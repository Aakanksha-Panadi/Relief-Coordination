import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime
import json

#initialize
cred = credentials.Certificate("../backend/serviceAccountKey.json")
firebase_admin.initialize_app(cred)
db = firestore.client(database_id="rescource-graph")

print("Connected to Firestore. Seeding data...")

# resources
resources = [

    # BOATS
    {
        "resourceId": "BOAT_01",
        "type": "boat",
        "name": "Rescue Boat 1",
        "capacity": 8,
        "currentLoad": 0,
        "location": "Zone_A_Dock",
        "zone":"A",
        "available":True,
        "operatorId": "VOL_001",
        "fuelLevel": "medium"
    },
    {
        "resourceId": "BOAT_02",
        "type": "boat",
        "name": "Rescue Boat 2",
        "capacity":6,
        "currentLoad": 0,
        "location": "Zone_B_Dock",
        "zone":"B",
        "available":True,
        "operatorId": "VOL_002",
        "fuelLevel": "high"
    },
    {
        "resourceId": "BOAT_03",
        "type": "boat",
        "name": "Rescue Boat 3",
        "capacity":10,
        "currentLoad": 0,
        "location": "Zone_C_Dock",
        "zone":"C",
        "available":True,
        "operatorId": "VOL_003",
        "fuelLevel": "medium"
    },
    {
        "resourceId": "BOAT_04",
        "type": "boat",
        "name": "Rescue Boat 4",
        "capacity":5,
        "currentLoad": 0,
        "location": "Central_Depot",
        "zone":"Central",
        "available":True,
        "operatorId": "VOL_004",
        "fuelLevel": "high"
    },
    {
        "resourceId": "BOAT_05",
        "type": "boat",
        "name": "Rescue Boat 5",
        "capacity":8,
        "currentLoad": 0,
        "location": "Zone_A_Dock",
        "zone":"A",
        "available":True,
        "operatorId": "VOL_001",
        "fuelLevel": "low"
    },

    # VOLUNTEER TEAMS   
    {
        "resourceId": "TEAM_01",
        "type": "volunteer_team",
        "name": "Volunteer Team Alpha",
        "capacity": 15,
        "currentLoad": 0,
        "location": "Central_Depot",
        "zone": "Central",
        "available":True,
        "skills": ["first_aid", "evacuation", "search_rescue"],
        "memberCount": 6
    },
    {
        "resourceId": "TEAM_02",
        "type": "volunteer_team",
        "name": "Volunteer Team Beta",
        "capacity": 20,
        "currentLoad": 0,
        "location": "Zone_B_Community_Hall",
        "zone": "B",
        "available":True,
        "skills": ["evacuation", "food_distribution"],
        "memberCount": 8
    },
    {
        "resourceId": "TEAM_03",
        "type": "volunteer_team",
        "name": "Volunteer Team Gamma",
        "capacity": 10,
        "currentLoad": 0,
        "location": "Zone_C_School",
        "zone": "C",
        "available":True,
        "skills": ["first_aid", "medical_support"],
        "memberCount": 5
    },
    {
        "resourceId": "TEAM_04",
        "type": "volunteer_team",
        "name": "Volunteer Team Delta",
        "capacity": 20,
        "currentLoad": 0,
        "location": "Zone_A_Temple",
        "zone": "A",
        "available":True,
        "skills": ["crowd_management", "evacuation", "search_rescue"],
        "memberCount": 10
    },
    {
        "resourceId": "TEAM_05",
        "type": "volunteer_team",
        "name": "Volunteer Team Echo",
        "capacity": 12,
        "currentLoad": 0,
        "location": "Central_Depot",
        "zone": "Central",
        "available":True,
        "skills": ["food_distribution", "shelter_management"],
        "memberCount": 6
    },
    # SHELTERS
    {
        "resourceId": "SHELTER_01",
        "type": "shelter",
        "name": "Government School Zone A",
        "totalCapacity": 200,
        "currentOccupancy": 45,
        "location": "Zone_A_School",
        "zone":"A",
        "available": True,
        "facilities": ["food", "water", "medical", "toilets"],
        "contactPerson": "Principal Sharma"
    },
    {
        "resourceId": "SHELTER_02",
        "type": "shelter",
        "name": "Community Hall Zone B",
        "totalCapacity": 150,
        "currentOccupancy": 30,
        "location": "Zone_B_Community_Hall",
        "zone":"B",
        "available": True,
        "facilities": ["food", "water", "toilets"],
        "contactPerson": "Ward Officer Nair"
    },
    {
        "resourceId": "SHELTER_03",
        "type": "shelter",
        "name": "Sports Complex Central",
        "totalCapacity": 500,
        "currentOccupancy": 120,
        "location": "Central_Sports_Complex",
        "zone":"Central",
        "available": True,
        "facilities": ["food", "water", "medical", "toilets", "generator"],
        "contactPerson": "Manager Pillai"
    },
    {
        "resourceId": "SHELTER_04",
        "type": "shelter",
        "name": "Temple Hall Zone C",
        "totalCapacity": 100,
        "currentOccupancy": 10,
        "location": "Zone_C_Temple",
        "zone":"C",
        "available": True,
        "facilities": ["food", "water"],
        "contactPerson": "Trustee Menon"
    },

    #MEDICAL UNITS
    {
        "resourceId":"MED_01",
        "type":"medical_unit",
        "name": "Medical Unit 1",
        "capacity": 5,
        "currentLoad":0,
        "location":"Central_Depot",
        "zone":"Central",
        "available":True,
        "supplies": ["first_aid", "insulin", "bp_medication", "oxygen"],
        "doctorOnBoard": True
    },
    {
        "resourceId":"MED_02",
        "type":"medical_unit",
        "name": "Medical Unit 2",
        "capacity": 5,
        "currentLoad":0,
        "location":"Zone_B_Dock",
        "zone":"B",
        "available":True,
        "supplies": ["first_aid", "basic_medication"],
        "doctorOnBoard": False
    }
]

#Load the graph from file
with open('road_graph.json', 'r') as f:
    road_graph = json.load(f)

#Add metadata fields Firestore needs
road_graph["lastUpdated"] = datetime.now().isoformat()
road_graph["closedEdges"] = []  # Initialize with no closed edges

#Save to firestore
print("Seeding road graph...")
db.collection('road_graph').document(road_graph['graph_id']).set(road_graph)
print("Road graph added")

#sample help requests
requests = [
    {
        "requestId": "REQ_001",
        "rawText": "Help!! 6 people stuck on roof near City Bank Market Road, 2 children please send boat urgent",
        "language": "English",
        "status": "PENDING",
        "urgency": "HIGH",
        "peopleCount": 6,
        "vulnerable": True,
        "vulnerableDetails": "2 children",
        "location": "Market_Road",
        "zone": "B",
        "needs": ["evacuation"],
        "timestamp": datetime.now().isoformat()
    },
    {
        "requestId": "REQ_002",
        "rawText": "मेरे घर में पानी भर गया है, 4 लोग हैं, बुजुर्ग माँ है चल नहीं सकती, Ward 7 मंदिर के पास",
        "language": "Hindi",
        "status": "PENDING",
        "urgency": "CRITICAL",
        "peopleCount": 4,
        "vulnerable": True,
        "vulnerableDetails": "elderly woman, cannot walk",
        "location": "Ward_7",
        "zone": "B",
        "needs": ["evacuation", "medical"],
        "timestamp": datetime.now().isoformat()
    },
    {
        "requestId": "REQ_003",
        "rawText": "உதவி தேவை, நாங்கள் 5 பேர் மாட்டிக்கொண்டோம், குழந்தைகள் இருக்கிறார்கள், Ward 3",
        "language": "Tamil",
        "status": "PENDING",
        "urgency": "HIGH",
        "peopleCount": 5,
        "vulnerable": True,
        "vulnerableDetails": "children present",
        "location": "Ward_3",
        "zone": "A",
        "needs": ["evacuation"],
        "timestamp": datetime.now().isoformat()
    },
    {
        "requestId": "REQ_004",
        "rawText": "HELP ward7 paani bahut zyada 3 log please boat jaldi",
        "language": "Mixed",
        "status": "PENDING",
        "urgency": "HIGH",
        "peopleCount": 3,
        "vulnerable": False,
        "vulnerableDetails": "",
        "location": "Ward_7",
        "zone": "B",
        "needs": ["evacuation"],
        "timestamp": datetime.now().isoformat()
    },
    {
        "requestId": "REQ_005",
        "rawText": "Diabetic patient needs insulin emergency Ward 9 ground floor flooding fast",
        "language": "English",
        "status": "PENDING",
        "urgency": "CRITICAL",
        "peopleCount": 1,
        "vulnerable": True,
        "vulnerableDetails": "diabetic patient, needs insulin",
        "location": "Ward_9",
        "zone": "B",
        "needs": ["medical", "evacuation"],
        "timestamp": datetime.now().isoformat()
    },
]

#write to firestore
print("Seeding resources...")
for resource in resources:
    db.collection("resources").document(
        resource["resourceId"]
    ).set(resource)
print(f"{len(resources)} resources added")

print("Seeding road graph...")
db.collection("road_graph").document(
    road_graph["graph_id"]
).set(road_graph)
print(f"{len(road_graph['nodes'])} nodes and {len(road_graph['edges'])} edges added")

print("Seeding sample requests...")
for req in requests:
    db.collection("requests").document(
        req["requestId"]
    ).set(req)
print(f"{len(requests)} requests added")

print("\nFirestore seeding complete!")
print(f"Resources: {len(resources)}")
print(f"Requests: {len(requests)}")
print(f"Road graph: 1 district")