import os
import threading
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
    def __init__(self, db_client=None, mock_resources: list | None = None):
        self.db = db_client
        self.mock_resources = mock_resources or []
        self.source = "unknown"
        self._reservation_lock = threading.RLock()

    def reserve_plan_resources(self, assignments: list) -> dict:
        """Atomically claim every resource in a plan before writing assignments."""
        loads = {}
        owners = {}
        shelter_loads = {}
        for assignment in assignments:
            if assignment.get("status") not in ("PENDING_APPROVAL", "PARTIAL"):
                continue
            resource = assignment.get("resource") or {}
            resource_id = resource.get("resourceId")
            if not resource_id:
                continue
            people = assignment.get("wave_people", assignment.get("request", {}).get("people_count", 0)) or 0
            loads[resource_id] = max(loads.get(resource_id, 0), people)
            owners.setdefault(resource_id, assignment.get("request", {}).get("requestId"))
            shelter = assignment.get("shelter") or {}
            shelter_id = shelter.get("resourceId")
            if shelter_id:
                shelter_loads[shelter_id] = shelter_loads.get(shelter_id, 0) + people

        with self._reservation_lock:
            db = self.db
            if db is not None:
                try:
                    from google.cloud import firestore

                    transaction = db.transaction()

                    @firestore.transactional
                    def reserve(txn):
                        refs = {rid: db.collection("resources").document(rid) for rid in set(loads) | set(shelter_loads)}
                        snapshots = {rid: txn.get(ref) for rid, ref in refs.items()}
                        pending_updates = {}
                        for rid, snapshot in snapshots.items():
                            data = snapshot.to_dict() if snapshot.exists else None
                            if not data:
                                raise ValueError(f"Resource {rid} no longer available")
                            if rid in shelter_loads:
                                if data.get("available", True) is False or data.get("powerAvailable", data.get("power_available", True)) is False:
                                    raise ValueError(f"Shelter {rid} is unavailable or has no power")
                                new_load = resource_load(data) + shelter_loads[rid]
                                if new_load > resource_capacity(data):
                                    raise ValueError(f"Shelter {rid} filled before approval")
                                pending_updates[rid] = {"currentOccupancy": new_load,
                                                        "available": new_load < resource_capacity(data)}
                                continue
                            if not data.get("available", False):
                                raise ValueError(f"Resource {rid} no longer available")
                            new_load = resource_load(data) + loads[rid]
                            if new_load > resource_capacity(data):
                                raise ValueError(f"Resource {rid} capacity changed; no longer available")
                            pending_updates[rid] = {
                                "available": False,
                                "currentLoad": new_load,
                                "assignedTo": owners[rid],
                            }
                        for rid, updates in pending_updates.items():
                            txn.update(refs[rid], updates)
                        return pending_updates

                    updates = reserve(transaction)
                except Exception as exc:
                    return {"ok": False, "reason": str(exc)}
            else:
                by_id = {r.get("resourceId"): r for r in self.mock_resources}
                updates = {}
                for rid, people in loads.items():
                    resource = by_id.get(rid)
                    if not resource or not resource.get("available", False):
                        return {"ok": False, "reason": f"Resource {rid} no longer available"}
                    new_load = resource_load(resource) + people
                    if new_load > resource_capacity(resource):
                        return {"ok": False, "reason": f"Resource {rid} capacity changed; no longer available"}
                    updates[rid] = {"available": False, "currentLoad": new_load, "assignedTo": owners[rid]}
                for rid, people in shelter_loads.items():
                    shelter = by_id.get(rid)
                    if not shelter or shelter.get("available", True) is False or shelter.get("powerAvailable", shelter.get("power_available", True)) is False:
                        return {"ok": False, "reason": f"Shelter {rid} is unavailable or has no power"}
                    new_load = resource_load(shelter) + people
                    if new_load > resource_capacity(shelter):
                        return {"ok": False, "reason": f"Shelter {rid} filled before approval"}
                    updates[rid] = {"currentOccupancy": new_load,
                                    "available": new_load < resource_capacity(shelter)}
                for rid, fields in updates.items():
                    by_id[rid].update(fields)
        return {"ok": True, "updates": updates}

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
                    for resource in resources:
                        low_fuel = str(resource.get("fuelLevel", "")).lower() == "low"
                        operator_lost = resource.get("operatorReachable", True) is False
                        if (low_fuel or operator_lost) and resource.get("available", True):
                            reason = "low fuel" if low_fuel else "operator unreachable"
                            resource.update({"available": False, "status": "UNAVAILABLE", "unavailable_reason": reason})
                            self.update_resource(resource.get("resourceId"), {
                                "available": False, "status": "UNAVAILABLE", "unavailable_reason": reason,
                            })
                    return resources
                print("Firestore 'resources' collection is empty; using fallback fleet")
            except Exception as e:
                print(f"Firestore resource read failed, using fallback fleet: {e}")
        self.source = "fallback"
        return self.mock_resources

    def get_available(self, resource_type: str | None = None) -> list:
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

    def get_active_requests(self) -> list:
        """Open cases used for repeat-report matching across service restarts."""
        if not self.db:
            return []
        try:
            docs = self.db.collection("requests").stream()
            return [self._to_request(doc) for doc in docs
                    if (doc.to_dict() or {}).get("status") in ("PENDING", "ASSIGNED")]
        except Exception as e:
            print(f"Firestore active-request read failed: {e}")
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

    def save_event(self, event: dict) -> str:
        """Persist coordinator decisions/reminders as an append-only audit item."""
        if not self.db:
            return "memory_only"
        try:
            ref = self.db.collection("audit_events").document()
            ref.set(event)
            return ref.id
        except Exception as e:
            raise PersistenceError(f"Could not write audit event: {e}") from e
