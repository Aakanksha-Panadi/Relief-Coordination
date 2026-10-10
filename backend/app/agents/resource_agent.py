import os
from dotenv import load_dotenv
from google.cloud.firestore_v1.base_query import FieldFilter

from app.config import (
    DISPATCHABLE_TYPES,
    normalize_request,
    resource_capacity,
    resource_load,
)

load_dotenv()


class PersistenceError(RuntimeError):
    """A write that must not be lost silently did not reach Firestore."""


class ResourceAgent:
    def __init__(self, db_client=None, mock_resources: list = None):
        self.db = db_client
        self.mock_resources = mock_resources or []
        self.source = "unknown"

    def get_all_resources(self) -> list:
        """Live fleet from Firestore, or the hardcoded fallback.

        These two are not the same world — the fallback has 8 resources with
        different capacities and positions than the 16 that are seeded — so
        `self.source` records which one is live for /health to report.
        """
        if self.db:
            try:
                docs = self.db.collection("resources").stream()
                resources = [doc.to_dict() for doc in docs]
                if resources:
                    self.source = "firestore"
                    return resources
                print("Firestore 'resources' collection is empty; using fallback fleet")
            except Exception as e:
                print(f"Firestore resource read failed, using fallback fleet: {e}")
        self.source = "fallback"
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
                summary[rtype] = {
                    "total": 0,
                    "available": 0,
                    "capacity": 0,
                    "in_use": 0,
                    # Shelters are destinations, not responders. Flagged so
                    # the dashboard stops implying they can be dispatched.
                    "dispatchable": rtype in DISPATCHABLE_TYPES,
                }
            summary[rtype]["total"] += 1
            summary[rtype]["capacity"] += resource_capacity(r)
            summary[rtype]["in_use"] += resource_load(r)
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

    def delete_docs(self, collection: str, doc_ids: list) -> int:
        """Remove specific documents. Used by reset to undo a demo run."""
        if not self.db or not doc_ids:
            return 0
        removed = 0
        for doc_id in doc_ids:
            if not doc_id:
                continue
            try:
                self.db.collection(collection).document(doc_id).delete()
                removed += 1
            except Exception as e:
                print(f"Delete failed for {collection}/{doc_id}: {e}")
        return removed

    def get_pending_requests(self) -> list:
        """Pending requests, normalized to the canonical snake_case shape.

        Seeded documents use camelCase (`peopleCount`, `rawText`); live
        intake writes snake_case. Normalizing here means nothing downstream
        has to know which wrote the document.
        """
        if not self.db:
            return []
        try:
            docs = self.db.collection("requests").where(
                filter=FieldFilter("status", "==", "PENDING")
            ).stream()
            return [self._to_request(doc) for doc in docs]
        except Exception as e:
            print(f"Firestore pending-request read failed: {e}")
            return []

    @staticmethod
    def _to_request(doc) -> dict:
        """Normalize a document and guarantee it has an id.

        Documents written before ids were keyed on requestId carry an opaque
        auto-id and no requestId field; the document id is their identity.
        """
        data = normalize_request(doc.to_dict() or {})
        if not data.get("requestId"):
            data["requestId"] = doc.id
        return data

    def save_request(self, request_data: dict) -> str:
        """Persist a request. Raises PersistenceError if the write is lost.

        Swallowing this returned 200 OK to a caller whose emergency was never
        recorded, so failures must propagate.
        """
        if not self.db:
            return "memory_only"
        try:
            # Key on requestId so re-running a demo overwrites the same
            # document instead of piling up duplicates.
            doc_id = request_data.get("requestId")
            doc_ref = (
                self.db.collection("requests").document(doc_id)
                if doc_id
                else self.db.collection("requests").document()
            )
            doc_ref.set(request_data)
            return doc_ref.id
        except Exception as e:
            raise PersistenceError(f"Could not save request: {e}") from e

    def update_request(self, request_id: str, updates: dict):
        """Write request status changes through to Firestore.

        Without this, approving a plan only marked the in-memory copy
        ASSIGNED; after a restart the request reappeared as PENDING and was
        dispatched a second time while the fleet was still marked busy.
        """
        if not self.db or not request_id:
            return
        try:
            self.db.collection("requests").document(request_id).update(updates)
        except Exception as e:
            print(f"Firestore request update failed for {request_id}: {e}")

    def save_assignment(self, assignment: dict) -> str:
        if self.db:
            try:
                doc_id = assignment.get("assignmentId")
                doc_ref = (
                    self.db.collection("assignments").document(doc_id)
                    if doc_id
                    else self.db.collection("assignments").document()
                )
                doc_ref.set(assignment)
                return doc_ref.id
            except Exception as e:
                print(f"Save failed: {e}")
        return "mock_id"