import os
from dotenv import load_dotenv

load_dotenv()

class ResourceAgent:
    def __init__(self, db_client=None, mock_resources: list = None):
        self.db = db_client
        self.mock_resources = mock_resources or []

    def get_all_resources(self) -> list:
        if self.db:
            try:
                docs = self.db.collection("resources").stream()
                return [doc.to_dict() for doc in docs]
            except Exception:
                return self.mock_resources
        return self.mock_resources

    def get_available(self, resource_type: str = None) -> list:
        resources = self.get_all_resources()
        available = [r for r in resources if r.get("available", False)]
        if resource_type:
            available = [r for r in available if r.get("type") == resource_type]
        return available

    def get_summary(self) -> dict:
        all_res = self.get_all_resources()
        summary = {}
        for r in all_res:
            rtype = r.get("type", "unknown")
            if rtype not in summary:
                summary[rtype] = {"total": 0, "available": 0}
            summary[rtype]["total"] += 1
            if r.get("available"):
                summary[rtype]["available"] += 1
        return summary

    def update_resource(self, resource_id: str, updates: dict):
        if self.db:
            try:
                self.db.collection("resources").document(
                    resource_id
                ).update(updates)
            except Exception as e:
                print(f"Firestore update failed: {e}")
        for r in self.mock_resources:
            if r["resourceId"] == resource_id:
                r.update(updates)
                break

    def get_pending_requests(self) -> list:
        if self.db:
            try:
                docs = self.db.collection("requests").where(
                    "status", "==", "PENDING"
                ).stream()
                return [doc.to_dict() for doc in docs]
            except Exception:
                return []
        return []

    def save_request(self, request_data: dict) -> str:
        if self.db:
            try:
                doc_ref = self.db.collection("requests").document()
                doc_ref.set(request_data)
                return doc_ref.id
            except Exception as e:
                print(f"Save failed: {e}")
        return "mock_id"

    def save_assignment(self, assignment: dict) -> str:
        if self.db:
            try:
                doc_ref = self.db.collection("assignments").document()
                doc_ref.set(assignment)
                return doc_ref.id
            except Exception as e:
                print(f"Save failed: {e}")
        return "mock_id"