import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime
import json
from pathlib import Path
import argparse

parser = argparse.ArgumentParser(description="Seed RescueIQ Firestore data")
parser.add_argument(
    "--remove-demo-requests-only",
    action="store_true",
    help="delete the five legacy sample requests without reseeding resources or roads",
)
args = parser.parse_args()

# Resolve files relative to this script, regardless of the caller's cwd.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SERVICE_ACCOUNT_FILE = PROJECT_ROOT / "backend" / "serviceAccountKey.json"
ROAD_GRAPH_FILE = Path(__file__).resolve().parent / "road_graph.json"

if not SERVICE_ACCOUNT_FILE.is_file():
    raise FileNotFoundError(
        f"Firestore service account key not found: {SERVICE_ACCOUNT_FILE}. "
        "Place your Firebase service account JSON at backend/serviceAccountKey.json."
    )

cred = credentials.Certificate(str(SERVICE_ACCOUNT_FILE))
firebase_admin.initialize_app(cred)
db = firestore.client(database_id="rescource-graph")

demo_request_ids = [f"REQ_{number:03d}" for number in range(1, 6)]
if args.remove_demo_requests_only:
    for request_id in demo_request_ids:
        db.collection("requests").document(request_id).delete()
    print("Removed legacy demo requests REQ_001 through REQ_005")
    raise SystemExit(0)

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
        "node_id": "N11",
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
        "node_id": "N12",
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
        "node_id": "N12",
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
        "node_id": "N01",
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
        "node_id": "N11",
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
        "node_id": "N01",
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
        "node_id": "N08",
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
        "node_id": "N07",
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
        "node_id": "N03",
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
        "node_id": "N01",
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
        "node_id": "N07",
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
        "node_id": "N08",
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
        "node_id": "N01",
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
        "node_id": "N03",
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
        "node_id": "N01",
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
        "node_id": "N12",
        "zone":"B",
        "available":True,
        "supplies": ["first_aid", "basic_medication"],
        "doctorOnBoard": False
    }
]

#Load the graph from file
with ROAD_GRAPH_FILE.open('r', encoding='utf-8') as f:
    road_graph = json.load(f)

#Add metadata fields Firestore needs
road_graph["lastUpdated"] = datetime.now().isoformat()
road_graph["closedEdges"] = []  # Initialize with no closed edges

# Sample requests are intentionally not seeded; the queue should contain only actual intake.
requests = []

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

print("Removing legacy sample requests...")
for request_id in demo_request_ids:
    db.collection("requests").document(request_id).delete()
print(f"Removed {len(demo_request_ids)} legacy demo requests; no sample requests were added")

print("\nFirestore seeding complete!")
print(f"Resources: {len(resources)}")
print(f"Requests: {len(requests)}")
print(f"Road graph: 1 district")
