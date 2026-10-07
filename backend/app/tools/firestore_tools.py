from google.cloud import firestore
from datetime import datetime
import os

class FirestoreTools:
    def __init__(self):
        self.db = firestore.Client(
            project=os.getenv("GOOGLE_CLOUD_PROJECT", "resourceworkflow")
        )

    def save_request(self, request_data: dict) -> str:
        request_data["timestamp"] = datetime.now().isoformat()
        request_data["status"] = "PENDING"
        
        doc_ref = self.db.collection("requests").document()
        doc_ref.set(request_data)
        return doc_ref.id

    def get_available_resources(self, resource_type: str = None) -> list:
        query = self.db.collection("resources").where("available", "==", True)
        if resource_type:
            query = query.where("type", "==", resource_type)
        return [doc.to_dict() for doc in query.stream()]

    def get_pending_requests(self) -> list:
        query = self.db.collection("requests").where(
            "status", "==", "PENDING"
        )
        return [doc.to_dict() for doc in query.stream()]

    def update_resource(self, resource_id: str, updates: dict):
        self.db.collection("resources").document(resource_id).update(updates)

    def update_request(self, request_id: str, updates: dict):
        self.db.collection("requests").document(request_id).update(updates)

    def save_assignment(self, assignment: dict) -> str:
        assignment["timestamp"] = datetime.now().isoformat()
        doc_ref = self.db.collection("assignments").document()
        doc_ref.set(assignment)
        return doc_ref.id