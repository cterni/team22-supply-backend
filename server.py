import json
import sys
import time
import jwt

from bson.objectid import ObjectId
from urllib import parse
from http.server import HTTPServer
from http.server import BaseHTTPRequestHandler
from utils.mongoutils import initMongoFromCloud
from fleetmanager import FleetManager
from dispatch import Dispatch
from fleet import Fleet
from os import getenv
from dotenv import load_dotenv
from queue import PriorityQueue

load_dotenv()

dispatch_queue = PriorityQueue()

class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    version = '0.2.0'

    # Reads the POST data from the HTTP header
    def extract_POST_Body(self):
        postBodyLength = int(self.headers['content-length'])
        postBodyString = self.rfile.read(postBodyLength)
        postBodyDict = json.loads(postBodyString)
        return postBodyDict

    # handle post requests
    def do_POST(self):
        status = 404  # HTTP Request: Not found
        postData = self.extract_POST_Body()  # store POST data into a dictionary
        path = self.path
        cloud = 'supply'
        client = initMongoFromCloud(cloud)
        db = client['team22_' + cloud]

        responseBody = {
            'status': 'failed',
            'message': 'Request not found'
        }

        if '/vehicleHeartbeat' in path:
            status = 401
            responseBody = {
                'status': 'failed',
                'message': 'Heartbeat Failed'
            }
            # Vehicle heartbeating / top update in DB
            vehicleId = postData.pop("vehicleId", None)
            location = postData.pop("location", None)
            vehicleStatus = postData.pop("status", None)
            lastHeartbeat = time.time()


            # Update document in DB
            vehicleStatusUpdate = db.Vehicle.update_one({"_id" : vehicleId}, {'$set' : {"status" : vehicleStatus}})
            vehicleLocationUpdate = db.Vehicle.update_one({"_id" : vehicleId}, {'$set' : {"location" : location}})
            lastHearbeatUpdate = db.Vehicle.update_one({"_id" : vehicleId}, {'$set' : {"lastHeartbeat" : lastHeartbeat}})


            statusUpdated = False
            locationUpdated = False
            lastHearbeatUpdated = False

            if vehicleStatusUpdate.matched_count == 1 and vehicleStatusUpdate.modified_count == 1:
                statusUpdated = True

            if vehicleLocationUpdate.matched_count == 1 and vehicleLocationUpdate.modified_count == 1:
                locationUpdated = True

            if lastHearbeatUpdate.matched_count == 1 and lastHearbeatUpdate.modified_count == 1:
                lastHearbeatUpdated = True

            if statusUpdated or locationUpdated or lastHearbeatUpdated:
                responseBody = {
                    'Heartbeat': 'Received'
                }
                # DatabaseUpdated
                # Find a dispatch document from DB where vehicleId = vehicleId from postData that is not complete
                dispatch_data = db.Dispatch.find_one({"vehicleId": vehicleId, "status": {'$ne': "complete"}})

                if dispatch_data is None and not dispatch_queue.empty():
                    '''if vehicle is not assigned to a dispatch that says either 'processing' or 'in progress', 
                    then check if dispatch queue can assign the vehicle to a new dispatch
                    '''
                    vehicle_data = db.Vehicle.find_one({ "_id": vehicleId })
                    dispatch_dict = dispatch_queue.get()
                    if vehicle_data != None and vehicleStatus == 'ready' and vehicle_data["vehicleType"] == dispatch_dict["vehicleType"]:
                        dispatch_dict = dispatch_queue.get()
                        dispatch_data = db.Dispatch.find_one({ "_id": dispatch_dict["dispatchId"] })
                    else:
                        dispatch_queue.put((1, dispatch_dict))
                # dispatch status is processing responseBody -> heartbeat received, send coordinates
                # dispatch status is in progress responseBody -> heartbeat received, send coordinates
                # dispatch status is complete responseBody -> heartbeat received
                if dispatch_data is not None:
                    dispatch = Dispatch(dispatch_data)
                    directions_response = dispatch.requestDirections(db)
                    coordinates = Dispatch.getRouteCoordinates(directions_response)
                    dispatch.status= "in progress" # Change dispatch status -> in progress
                    db.Dispatch.update_one({"_id": dispatch.id}, {'$set': {"status": dispatch.status, "vehicleId": vehicleId }})
                    responseBody = {
                        'Heartbeat': 'Received',
                        'coordinates': coordinates,  # [ [90.560,45.503], [90.560,45.523] ]
                        'duration': directions_response["routes"][0]["legs"][0]["duration"]
                    }
                    last_coordinate = coordinates[len(coordinates)-1]
                    last_coordinate_string = f"{last_coordinate[0]},{last_coordinate[1]}"
                    # check if vehicle coordinate == order location
                    if location == last_coordinate_string:
                        dispatch.status = "complete"
                        db.Dispatch.update_one({"_id": dispatch.id}, {'$set': {"status": dispatch.status}})                    

                status = 200 # DatabaseUpdated 

        elif '/fleet' in path:
            status = 401
            # Get token so we can get the fleet manager
            fleetManager = self.get_fleet_manager_from_token(db)

            # add fleet to fleet manager and Fleet collection
            if fleetManager is not None:
                status = 200
                fleetManager.addFleet(db, postData)
                responseBody = {
                    "fleetManager": fleetManager.id,
                    "fleetIds": fleetManager.fleetIds
                }


        elif '/vehicle' in path:
            status = 401
            # Get token so we can get the fleet manager
            fleetManager = self.get_fleet_manager_from_token(db)
            #get correct fleet and add vehicle to it
            if fleetManager is not None:
                status = 200
                fleet = fleetManager.accessFleet(db, postData['vType'])
                fleet.addVehicle(db, postData)
                responseBody = {
                    "totalVehicles": fleet.totalVehicles
                }

        elif '/dispatch' in path:
            status = 401
            dispatch_data = {
                "orderId": postData["orderId"],
                "vehicleId": "0",
                "orderDestination": postData["orderDestination"],
                "status": "processing"
            }

            dispatch = Dispatch(dispatch_data)
            vehicleType = postData["vehicleType"]

            cursor = db.Fleet.find({ "vType": vehicleType })

            selected_fleet_data = None
            vehicle_id = ""
            for fleet_data in cursor:
                fleet = Fleet(fleet_data)
                vehicle_data = fleet.findAvailableVehicle(db)
                if vehicle_data != {}:
                    # once it finds a vehicle then save the id
                    vehicle_id = vehicle_data["vehicleId"]
                    break

            dispatch.vehicleId = vehicle_id
            db.Dispatch.insert_one({
                "_id": dispatch.id,
                "orderId": dispatch.orderId,
                "vehicleId": dispatch.vehicleId,
                "status": dispatch.status,
                "orderDestination": dispatch.orderDestination
            })

            if vehicle_id == "":
                # add dispatch to queue because there was no available vehicles for it
                dispatch_queue.append({ "dispatchId": dispatch.id, "vehicleType": vehicleType })
            status = 201 # request is created
            responseBody = {
                'dispatch_status': dispatch.status,
                'vehicleId': dispatch.vehicleId
            }
            # There was no vehicle available for the specific fleet, add to a queue

        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        responseString = json.dumps(responseBody).encode('utf-8')
        self.wfile.write(responseString)
        client.close()

    # handle get requests
    def do_GET(self):
        path = self.path
        status = 400
        cloud = 'supply'
        client = initMongoFromCloud(cloud)
        db = client['team22_' + cloud]
        response = {}
        # Get token
        fleetManager = self.get_fleet_manager_from_token(db)

        #front end request for tables
        if '/returnVehicles' in path:
            status = 403 # Not Authorized
            response = {
                "message": "Not authorized"
            }

            if fleetManager is not None:
                # Validate
                fleetIds = fleetManager.fleetIds
                fleetArray = []
                vehicles = []
                for fleetId in fleetIds:
                    cursor = db.Vehicle.find({"fleetId": fleetId},
                                             {
                                                 "fleetId": 0,
                                                 "dock": 0,
                                             })
                    for vehicle in cursor:
                        vehicles.append(vehicle)

                    fleetArray.append(vehicles)
                response = fleetArray
                status = 200

        # vehicle request
        elif '/getAllVehicles' in path:
            vehicles = []
            try:
                cursor = db.Vehicle.find({})
                for vehicle in cursor:
                    vehicles.append(vehicle)
                status = 200
                response = vehicles
                
            except:
                response = {'request': 'failed'}

        # demand request
        elif '/status' in path:
            parse.urlsplit(path)
            parse.parse_qs(parse.urlsplit(path).query)
            parameters = dict(parse.parse_qsl(parse.urlsplit(path).query))
            orderid_dict = {'orderId': parameters.get('orderId')}
            cursor = db.Dispatch.find(orderid_dict)
            dispatch_data = {}
            for dis in cursor:
                dispatch_data = dis
            if dispatch_data is not None:
                dispatch = Dispatch(dispatch_data)
                # Get directions API and geocde API responses stored in variables
                directions_response = dispatch.requestDirections(db)
                geocode_response = dispatch.requestForwardGeocoding()

                vehicle_starting_coordinate = dispatch.getVehicleLocation(db)
                destination_coordinate = Dispatch.getCoordinateFromGeocodeResponse(geocode_response)
                geometry = Dispatch.getGeometry(directions_response)
                status = 200
                response = {
                    'order_status': dispatch.status,
                    'vehicle_starting_coordinate': vehicle_starting_coordinate,
                    'destination_coordinate': destination_coordinate,
                    'geometry': geometry
                }
        elif '/getVehicleLocation' in path:
            parse.urlsplit(path)
            parse.parse_qs(parse.urlsplit(path).query)
            parameters = dict(parse.parse_qsl(parse.urlsplit(path).query))
            vehicleid = parameters.get('vehicleId')
            vehicle_data = db.Vehicle.find_one({'_id': vehicleid})
            if vehicle_data is not None:
                response = {
                    'location': vehicle_data['location']
                }
                status = 200
        else:
            status = 400
            response = {'received': 'nope'}

        self.send_response(status)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        responseString = json.dumps(response).encode('utf-8')
        self.wfile.write(responseString)
        client.close()

    def get_fleet_manager_from_token(self, db):
        try:
            tokenStr = self.headers["Cookie"]
            if tokenStr is not None:
                token = tokenStr.split('token=')[1].split(";")[0]
                if token != "":
                    token_secret = getenv("TOKEN_SECRET")
                    token_decoded = jwt.decode(token, token_secret, algorithms="HS256")
                    user_data = db.FleetManager.find_one({ "_id": token_decoded["user_id"]})
                    return FleetManager(user_data)
        except:
            pass
        return None

def main():
    port = 4001  # Port 4001 reserved for demand backend
    server = HTTPServer(('', port), SimpleHTTPRequestHandler)
    print('Server is starting... Use <Ctrl+C> to cancel. Running on Port {}'.format(port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped server due to user interrupt")
    print("Server stopped")


if __name__ == "__main__":
    main()
