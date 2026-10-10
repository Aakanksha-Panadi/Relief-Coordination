import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone

from app import config
from app.agents.intake_agent import IntakeAgent
from app.agents.dispatch_agent import DispatchAgent
from app.agents.replan_agent import ReplanAgent
from app.agents.resource_agent import ResourceAgent
from app.optimizer.router import DistrictRouter
from app.optimizer.evaluation import EvaluationHarness


class RescueIQOrchestrator:
    """Root agent. Routes coordinator intent to the right sub-agent and owns
    the live state: current requests, current plan, road closures."""

    def __init__(self):
        self.db = self._connect_firestore()

        with open(os.path.abspath(config.ROAD_GRAPH_PATH), encoding="utf-8") as f:
            self.graph_data = json.load(f)

        self.mock_resources = self._load_mock_resources()
        self.router = DistrictRouter(self.graph_data)
        self.intake = IntakeAgent()
        self.resource_agent = ResourceAgent(
            db_client=self.db, mock_resources=self.mock_resources
        )

        # One canonical resource list, mutated in place on approval so that
        # dispatch and replan always see current availability.
        self.resources = self.resource_agent.get_all_resources() or list(
            self.mock_resources
        )

        self.dispatch = DispatchAgent(self.router, self.resources)
        self.replan = ReplanAgent(self.router, self.resources)
        self.evaluator = EvaluationHarness(self.router, self.resources)

        self.current_plan = None
        self.current_requests = []
        self.last_replan = None
        self.events = []
        self._session_assignment_ids = set()
        # Request ids are handed out under a lock: `+=` is not atomic, and
        # FastAPI runs sync endpoints on a threadpool, so two concurrent
        # intake calls could mint the same id and .set() would overwrite one
        # live emergency with the other.
        self._id_lock = threading.Lock()
        self._id_scan_failed = False
        # Seeded demo requests already occupy REQ_001.. in Firestore. Start
        # above them so a live run never overwrites (and reset never deletes)
        # the fixtures the seed script created.
        self._req_id_base = self._highest_seeded_request_number()
        self._req_counter = self._req_id_base

    def _highest_seeded_request_number(self) -> int:
        if not self.db:
            return 0
        try:
            highest = 0
            for doc in self.db.collection("requests").stream():
                match = re.fullmatch(r"REQ_(\d+)", doc.id)
                if match:
                    highest = max(highest, int(match.group(1)))
            return highest
        except Exception as e:
            # Returning 0 here would restart numbering at REQ_001 and
            # overwrite existing documents. Switch to opaque ids instead.
            print(f"Could not scan existing request ids ({e}); using opaque ids")
            self._id_scan_failed = True
            return 0

    def _next_request_id(self) -> str:
        if self._id_scan_failed:
            return f"REQ_{uuid.uuid4().hex[:8].upper()}"
        with self._id_lock:
            self._req_counter += 1
            return f"REQ_{self._req_counter:03d}"

    def _connect_firestore(self):
        try:
            from google.cloud import firestore

            db = firestore.Client(
                project=config.GCP_PROJECT, database=config.FIRESTORE_DATABASE
            )
            print(f"Connected to Firestore: {config.FIRESTORE_DATABASE}")
            return db
        except Exception as e:
            print(f"Firestore unavailable, using in-memory mock: {e}")
            return None

    # ---------- intake ----------

    def process_intake(self, message: str) -> dict:
        """Extract, persist, queue. Raises if the request could not be stored.

        Both failure modes propagate deliberately: an emergency that was not
        recorded must not look like a success to the caller.
        """
        result = self.intake.process_message(message)
        result["requestId"] = self._next_request_id()
        result["raw_text"] = message
        result["status"] = "PENDING"
        result["createdAt"] = datetime.now(timezone.utc).isoformat()
        # Stays PENDING even when the location is unresolved, so it surfaces
        # in the next dispatch plan's `unassigned` list with a reason rather
        # than disappearing into a status nobody queries.
        self.resource_agent.save_request(result)
        self.current_requests.append(result)
        return result

    def process_intake_batch(self, messages: list) -> list:
        """Per-message isolation: one bad message must not drop the rest."""
        results = []
        for message in messages:
            try:
                results.append(self.process_intake(message))
            except Exception as e:
                results.append({
                    "raw_text": message,
                    "status": "FAILED",
                    "error": str(e),
                })
        return results

    def get_requests(self, status: str = None) -> list:
        requests = self.current_requests
        if not requests:
            requests = self.resource_agent.get_pending_requests()
        if status:
            requests = [r for r in requests if r.get("status") == status]
        return requests

    # ---------- dispatch ----------

    def generate_dispatch_plan(self) -> dict:
        if not self.current_requests:
            firestore_requests = self.resource_agent.get_pending_requests()
            if not firestore_requests:
                return {"error": "No requests to dispatch"}
            self.current_requests = firestore_requests

        pending = [
            r for r in self.current_requests
            if r.get("status") in (None, "PENDING")
        ]
        if not pending:
            return {"error": "All requests are already assigned"}

        # Baseline is scored against the same fleet the real plan sees.
        # Computing it later, after /dispatch/approve has marked resources
        # busy, handed the baseline a depleted fleet and understated it.
        baseline = self.evaluator.score_plan(
            self.evaluator.baseline_plan(pending), len(pending)
        )

        self.current_plan = self.dispatch.create_plan(pending)
        self.current_plan["planId"] = f"PLAN_{uuid.uuid4().hex[:8]}"
        self.current_plan["generatedAt"] = datetime.now(timezone.utc).isoformat()
        self.current_plan["metrics"] = self.evaluator.score_plan(
            self.current_plan, len(pending)
        )
        self.current_plan["baseline_metrics"] = baseline
        self.current_plan["request_count"] = len(pending)
        return self.current_plan

    def approve_plan(self, approved_by: str = "coordinator") -> dict:
        """Coordinator approval, the only path that commits a plan.

        Writes assignments to Firestore, marks resources busy and requests
        assigned. Nothing is committed until this is called.
        """
        if not self.current_plan:
            return {"error": "No active dispatch plan to approve"}

        approved = []
        now = datetime.now(timezone.utc).isoformat()

        for assignment in self.current_plan.get("assignments", []):
            resource = assignment["resource"]
            request = assignment["request"]
            route = assignment.get("route", {})

            record = {
                "assignmentId": f"ASG_{uuid.uuid4().hex[:8]}",
                "requestId": request.get("requestId"),
                "resourceIds": [resource["resourceId"]],
                "status": "APPROVED",
                "eta_minutes": route.get("minutes"),
                "route": route.get("path", []),
                "explanation": assignment.get("explanation", ""),
                "approvedBy": approved_by,
                "approvedAt": now,
            }
            self.resource_agent.save_assignment(record)
            self._session_assignment_ids.add(record["assignmentId"])

            self.resource_agent.update_resource(
                resource["resourceId"],
                {
                    "available": False,
                    "currentLoad": config.resource_load(resource)
                    + request.get("people_count", 0),
                    "assignedTo": request.get("requestId"),
                    # Where it is headed, so completing the trip can update
                    # its position instead of leaving it at its old depot.
                    "destinationNode": (route.get("path") or [None])[-1],
                },
            )
            self._apply_resource_update(resource["resourceId"], request, route)

            request["status"] = "ASSIGNED"
            request["assignedResources"] = [resource["resourceId"]]
            # Write the status through. Keeping it in memory only meant that
            # after a restart this request reappeared as PENDING and was
            # dispatched a second time while the fleet was still busy.
            self.resource_agent.update_request(
                request.get("requestId"),
                {
                    "status": "ASSIGNED",
                    "assignedResources": [resource["resourceId"]],
                    "assignmentId": record["assignmentId"],
                    "assignedAt": now,
                },
            )
            assignment["status"] = "APPROVED"
            approved.append(record)

        self.current_plan["status"] = "APPROVED"
        self.events.append(
            {"type": "plan_approved", "count": len(approved), "timestamp": now}
        )
        return {
            "planId": self.current_plan.get("planId"),
            "approved_count": len(approved),
            "assignments": approved,
            "approvedBy": approved_by,
            "approvedAt": now,
        }

    def _apply_resource_update(
        self, resource_id: str, request: dict, route: dict = None
    ):
        for r in self.resources:
            if r.get("resourceId") == resource_id:
                r["available"] = False
                r["currentLoad"] = config.resource_load(r) + request.get(
                    "people_count", 0
                )
                r["assignedTo"] = request.get("requestId")
                if route:
                    r["destinationNode"] = (route.get("path") or [None])[-1]
                break

    def complete_assignment(self, request_id: str) -> dict:
        """Mark a rescue finished: free the resource and move it to where it went.

        Without this there was no way back from busy except /reset, so a
        resource was effectively consumed by its first assignment and the
        fleet drained over a demo.
        """
        if not self.current_plan:
            return {"error": "No active dispatch plan"}

        match = next(
            (
                a for a in self.current_plan.get("assignments", [])
                if a.get("request", {}).get("requestId") == request_id
            ),
            None,
        )
        if not match:
            return {"error": f"No assignment found for request '{request_id}'"}
        if match.get("status") == "COMPLETED":
            return {"error": f"{request_id} is already completed"}
        if match.get("status") != "APPROVED":
            return {"error": f"{request_id} has not been approved yet"}

        resource = match["resource"]
        route = match.get("route", {})
        arrived_at = (route.get("path") or [None])[-1]
        now = datetime.now(timezone.utc).isoformat()

        updates = {
            "available": True,
            "currentLoad": 0,
            "assignedTo": None,
            # The vehicle is now where the rescue was, not at its depot.
            "currentNode": arrived_at,
        }
        self.resource_agent.update_resource(resource["resourceId"], updates)
        for r in self.resources:
            if r.get("resourceId") == resource["resourceId"]:
                r.update(updates)
                break

        match["status"] = "COMPLETED"
        match["request"]["status"] = "COMPLETED"
        self.resource_agent.update_request(
            request_id, {"status": "COMPLETED", "completedAt": now}
        )
        self.events.append(
            {"type": "assignment_completed", "requestId": request_id, "timestamp": now}
        )
        return {
            "requestId": request_id,
            "resourceId": resource["resourceId"],
            "released": True,
            "currentNode": arrived_at,
            "completedAt": now,
        }

    # ---------- replanning ----------

    def simulate_road_closure(self, edge_id: str) -> dict:
        if edge_id not in self.graph_data.get("edges", {}):
            return {"error": f"Unknown edge '{edge_id}'"}
        if not self.current_plan:
            return {"error": "No active dispatch plan. Generate one first."}

        assignments = self.current_plan.get("assignments", [])
        result = self.replan.simulate_road_closure(edge_id, assignments)
        result["edge_name"] = self.graph_data["edges"][edge_id].get("name", edge_id)
        self.last_replan = result
        self.events.append(
            {
                "type": "road_closure",
                "affected": edge_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "triggeredReplan": True,
            }
        )
        return result

    def approve_replan(self, approved_by: str = "coordinator") -> dict:
        """Promote the replanned routes into the current plan."""
        if not self.last_replan:
            return {"error": "No replan to approve"}

        updated = 0
        for assignment in self.last_replan.get("assignments", []):
            if assignment.get("status") == "REPLANNED" and assignment.get("new_route"):
                assignment["route"] = assignment["new_route"]
                updated += 1

        self.current_plan["assignments"] = self.last_replan["assignments"]
        return {
            "updated_routes": updated,
            "approvedBy": approved_by,
            "approvedAt": datetime.now(timezone.utc).isoformat(),
        }

    def reopen_edge(self, edge_id: str) -> dict:
        self.router.open_edge(edge_id)
        return {"edge_id": edge_id, "closed": False}

    # ---------- read models ----------

    def get_resource_summary(self) -> dict:
        return {
            "by_type": self.resource_agent.get_summary(),
            # Firestore and the hardcoded fallback are different fleets with
            # different capacities and positions; say which one is live.
            "source": self.resource_agent.source,
        }

    def get_metrics(self) -> dict:
        """Compare against the baseline snapshot taken when the plan was made."""
        if not self.current_plan:
            return {"error": "No dispatch plan yet. Generate one first."}
        return self.evaluator.compare(
            self.current_requests,
            self.current_plan,
            baseline=self.current_plan.get("baseline_metrics"),
        )

    def get_graph(self) -> dict:
        return {
            "nodes": self.graph_data.get("nodes", {}),
            "edges": self.graph_data.get("edges", {}),
            "closed_edges": sorted(self.router.closed_edges),
        }

    # ---------- lifecycle ----------

    def reset(self) -> dict:
        """Back to a clean demo state: no requests, no plan, roads reopened.

        Must write through to Firestore. Restoring only the in-memory copy
        leaves resources marked busy in the database, so the next demo run
        starts with half the fleet unavailable.
        """
        removed_requests = self.resource_agent.delete_docs(
            "requests", [r.get("requestId") for r in self.current_requests]
        )
        removed_assignments = self.resource_agent.delete_docs(
            "assignments", list(self._session_assignment_ids)
        )

        self.current_requests = []
        self.current_plan = None
        self.last_replan = None
        self.events = []
        self._req_counter = self._req_id_base
        self._session_assignment_ids = set()
        self.router.closed_edges = set(self.graph_data.get("closedEdges", []))

        freed = 0
        for r in self.resources:
            was_busy = (
                not r.get("available", True)
                or config.resource_load(r)
                or r.get("currentNode")
            )
            r["available"] = True
            r["currentLoad"] = 0
            r.pop("assignedTo", None)
            # Positions must reset too, or the next run starts every vehicle
            # at wherever the previous run's last rescue happened to end.
            r.pop("currentNode", None)
            r.pop("destinationNode", None)
            if was_busy:
                self.resource_agent.update_resource(
                    r["resourceId"],
                    {
                        "available": True,
                        "currentLoad": 0,
                        "assignedTo": None,
                        "currentNode": None,
                        "destinationNode": None,
                    },
                )
                freed += 1

        return {
            "status": "reset",
            "resources_freed": freed,
            "requests_removed": removed_requests,
            "assignments_removed": removed_assignments,
        }

    def release_all_resources(self) -> dict:
        """Force every resource back to available, in memory and in Firestore.

        Recovery hatch for when a previous run left the fleet marked busy.
        """
        freed = 0
        for r in self.resource_agent.get_all_resources():
            if not r.get("available", True) or config.resource_load(r):
                self.resource_agent.update_resource(
                    r["resourceId"],
                    {"available": True, "currentLoad": 0, "assignedTo": None},
                )
                freed += 1
        for r in self.resources:
            r["available"] = True
            r["currentLoad"] = 0
            r.pop("assignedTo", None)
        return {"resources_freed": freed}

    def _load_mock_resources(self) -> list:
        """Offline fallback fleet.

        Deliberately a strict subset of what data/seed_firestore.py writes —
        same ids, capacities, positions and skills — so losing Firestore
        shrinks the fleet rather than silently swapping in a different world.
        """
        return [
            {"resourceId": "BOAT_01", "type": "boat", "name": "Rescue Boat 1",
             "capacity": 8, "currentLoad": 0, "location": "Zone_A_Dock",
             "node_id": "N11", "available": True},
            {"resourceId": "BOAT_02", "type": "boat", "name": "Rescue Boat 2",
             "capacity": 6, "currentLoad": 0, "location": "Zone_B_Dock",
             "node_id": "N12", "available": True},
            {"resourceId": "BOAT_03", "type": "boat", "name": "Rescue Boat 3",
             "capacity": 10, "currentLoad": 0, "location": "Zone_C_Dock",
             "node_id": "N12", "available": True},
            {"resourceId": "BOAT_04", "type": "boat", "name": "Rescue Boat 4",
             "capacity": 5, "currentLoad": 0, "location": "Central_Depot",
             "node_id": "N01", "available": True},
            {"resourceId": "TEAM_01", "type": "volunteer_team",
             "name": "Volunteer Team Alpha", "capacity": 15, "currentLoad": 0,
             "location": "Central_Depot", "node_id": "N01", "available": True,
             "skills": ["first_aid", "evacuation", "search_rescue"]},
            {"resourceId": "TEAM_02", "type": "volunteer_team",
             "name": "Volunteer Team Beta", "capacity": 20, "currentLoad": 0,
             "location": "Zone_B_Community_Hall", "node_id": "N08",
             "available": True,
             "skills": ["evacuation", "food_distribution"]},
            {"resourceId": "TEAM_03", "type": "volunteer_team",
             "name": "Volunteer Team Gamma", "capacity": 10, "currentLoad": 0,
             "location": "Zone_C_School", "node_id": "N07", "available": True,
             "skills": ["first_aid", "medical_support"]},
            {"resourceId": "MED_01", "type": "medical_unit",
             "name": "Medical Unit 1", "capacity": 5, "currentLoad": 0,
             "location": "Central_Depot", "node_id": "N01", "available": True,
             "supplies": ["first_aid", "insulin", "bp_medication", "oxygen"]},
        ]
