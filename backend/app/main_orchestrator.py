import json
import os
from app.agents.intake_agent import IntakeAgent
from app.agents.dispatch_agent import DispatchAgent
from app.agents.replan_agent import ReplanAgent
from app.agents.resource_agent import ResourceAgent
from app.optimizer.router import DistrictRouter

class RescueIQOrchestrator:
    def __init__(self):
        # Connect to Firestore
        try:
            from google.cloud import firestore
            self.db = firestore.Client(
                project="resourceworkflow",
                database="rescource-graph"
            )
            print("Connected to Firestore: rescource-graph")
        except Exception as e:
            print(f"Firestore unavailable, using mock: {e}")
            self.db = None

        # Load road graph
        graph_path = os.path.join(
            os.path.dirname(__file__),
            "../../data/road_graph.json"
        )
        with open(graph_path) as f:
            self.graph_data = json.load(f)

        # Mock resources fallback
        self.mock_resources = self._load_mock_resources()

        # Initialize all components
        self.router = DistrictRouter(self.graph_data)
        self.intake = IntakeAgent()
        self.resource_agent = ResourceAgent(
            db_client=self.db,
            mock_resources=self.mock_resources
        )

        # Load resources for dispatch
        resources = self.resource_agent.get_all_resources()
        if not resources:
            resources = self.mock_resources

        self.dispatch = DispatchAgent(self.router, resources)
        self.replan = ReplanAgent(self.router, resources)

        self.current_plan = None
        self.current_requests = []

    def process_intake(self, message: str) -> dict:
        result = self.intake.process_message(message)
        result["raw_text"] = message
        result["status"] = "PENDING"

        # Save to Firestore if available
        self.resource_agent.save_request(result)
        self.current_requests.append(result)
        return result

    def generate_dispatch_plan(self) -> dict:
        if not self.current_requests:
            # Try loading from Firestore
            firestore_requests = self.resource_agent.get_pending_requests()
            if firestore_requests:
                self.current_requests = firestore_requests
            else:
                return {"error": "No requests to dispatch"}

        self.current_plan = self.dispatch.create_plan(self.current_requests)
        return self.current_plan

    def simulate_road_closure(self, edge_id: str) -> dict:
        if not self.current_plan:
            return {"error": "No active dispatch plan. Generate one first."}
        assignments = self.current_plan.get("assignments", [])
        return self.replan.simulate_road_closure(edge_id, assignments)

    def get_resource_summary(self) -> dict:
        return self.resource_agent.get_summary()

    def reset(self):
        self.current_requests = []
        self.current_plan = None

    def _load_mock_resources(self) -> list:
        return [
            {
                "resourceId": "BOAT_01",
                "type": "boat",
                "name": "Rescue Boat 1",
                "capacity": 8,
                "currentLoad": 0,
                "location": "Zone_A_Dock",
                "available": True
            },
            {
                "resourceId": "BOAT_02",
                "type": "boat",
                "name": "Rescue Boat 2",
                "capacity": 6,
                "currentLoad": 0,
                "location": "Zone_B_Dock",
                "available": True
            },
            {
                "resourceId": "BOAT_03",
                "type": "boat",
                "name": "Rescue Boat 3",
                "capacity": 10,
                "currentLoad": 0,
                "location": "Zone_B_Dock",
                "available": True
            },
            {
                "resourceId": "BOAT_04",
                "type": "boat",
                "name": "Rescue Boat 4",
                "capacity": 5,
                "currentLoad": 0,
                "location": "Central_Depot",
                "available": True
            },
            {
                "resourceId": "TEAM_01",
                "type": "volunteer_team",
                "name": "Volunteer Team Alpha",
                "capacity": 15,
                "currentLoad": 0,
                "location": "Central_Depot",
                "available": True
            },
            {
                "resourceId": "TEAM_02",
                "type": "volunteer_team",
                "name": "Volunteer Team Beta",
                "capacity": 20,
                "currentLoad": 0,
                "location": "Zone_B_Community_Hall",
                "available": True
            },
            {
                "resourceId": "TEAM_03",
                "type": "volunteer_team",
                "name": "Volunteer Team Gamma",
                "capacity": 10,
                "currentLoad": 0,
                "location": "Zone_A_Dock",
                "available": True
            },
            {
                "resourceId": "MED_01",
                "type": "medical_unit",
                "name": "Medical Unit 1",
                "capacity": 5,
                "currentLoad": 0,
                "location": "Central_Depot",
                "available": True
            },
        ]