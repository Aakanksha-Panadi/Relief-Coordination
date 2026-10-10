import json
import os
import re
import threading
import uuid
from difflib import SequenceMatcher
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
        self._approval_lock = threading.RLock()
        self._intake_lock = threading.RLock()
        self._closure_lock = threading.RLock()
        self._closure_changes = set()
        self._closure_timer = None
        self._closure_deadline = None
        self._closure_debounce_seconds = 60
        self._plan_reminders = {}
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

    def process_intake(self, message: str, media_type: str = "text") -> dict:
        with self._intake_lock:
            return self._process_intake_locked(message, media_type)

    def _process_intake_locked(self, message: str, media_type: str = "text") -> dict:
        """Extract, persist, queue. Raises if the request could not be stored.

        Both failure modes propagate deliberately: an emergency that was not
        recorded must not look like a success to the caller.
        """
        if not self.current_requests:
            self.current_requests = self.resource_agent.get_active_requests()
        result = (self.intake.process_message(message) if media_type == "text" else
                  self.intake._unprocessed(message, f"Unsupported media type: {media_type}"))
        result["raw_text"] = message
        result.setdefault("processing_flags", [])
        result.setdefault("human_review_required", False)

        # Replies from a household update their open case. Exact repeats and
        # strong text matches link to an existing case instead of creating
        # additional demand. This is conservative and never merges on location alone.
        text = message.lower().strip()
        candidates = [r for r in self.current_requests if r.get("status") in ("PENDING", "ASSIGNED")]
        cancellation = bool(re.search(r"\b(rescued|safe now|we are safe|we're safe|cancel|no help needed)\b", text))
        count_match = re.search(r"\b(\d+)\s*(?:people|persons|person|of us|members)\b", text)
        followup = bool(re.search(r"\b(now|update|actually|only|more|less)\b", text)) or cancellation
        match = self._match_existing_report(message, result, candidates)
        if match is None and followup and len(candidates) == 1:
            match = candidates[0]
        if match and (followup or self._same_report(message, match)):
            if cancellation:
                previous_status = match.get("status")
                match["status"] = "CANCELLED" if previous_status == "PENDING" else "COMPLETED"
                match["cancel_reason"] = "caller reported safe/rescued"
                match["updatedAt"] = datetime.now(timezone.utc).isoformat()
                self.resource_agent.update_request(match.get("requestId"), {
                    "status": match["status"], "cancel_reason": match["cancel_reason"],
                    "updatedAt": match["updatedAt"],
                })
                self._audit({"type": "request_cancelled_by_followup", "requestId": match.get("requestId"),
                             "previous_status": previous_status, "message": message,
                             "timestamp": match["updatedAt"]})
                if previous_status == "ASSIGNED":
                    self._release_cancelled_assignment(match)
                else:
                    for assignment in (self.current_plan or {}).get("assignments", []):
                        if assignment.get("request", {}).get("requestId") == match.get("requestId"):
                            assignment["status"] = "CANCELLED"
                replacement = None
                if previous_status in ("ASSIGNED", "PENDING") and self.current_plan:
                    candidate_plan = self.generate_dispatch_plan()
                    if "error" not in candidate_plan:
                        replacement = candidate_plan
                return {**match, "linked_request_id": match.get("requestId"), "followup_action": "cancelled",
                        "replacement_plan": replacement}
            if count_match:
                match["people_count"] = int(count_match.group(1))
                match["updatedAt"] = datetime.now(timezone.utc).isoformat()
                was_assigned = match.get("status") == "ASSIGNED"
                had_plan = bool(self.current_plan)
                if had_plan:
                    self._release_cancelled_assignment(match)
                if was_assigned:
                    match["status"] = "PENDING"
                self.resource_agent.update_request(match.get("requestId"), {
                    "people_count": match["people_count"], "updatedAt": match["updatedAt"],
                    "status": match.get("status", "PENDING")})
                self._audit({"type": "request_updated_by_followup", "requestId": match.get("requestId"),
                             "people_count": match["people_count"], "timestamp": match["updatedAt"]})
                replacement = None
                if had_plan:
                    candidate_plan = self.generate_dispatch_plan()
                    if "error" not in candidate_plan:
                        replacement = candidate_plan
                return {**match, "linked_request_id": match.get("requestId"), "followup_action": "updated",
                        "replacement_plan": replacement}
            if self._same_report(message, match):
                match["report_count"] = int(match.get("report_count", 1)) + 1
                match["last_report_at"] = datetime.now(timezone.utc).isoformat()
                match["report_sources"] = list(match.get("report_sources", [])) + [message]
                self.resource_agent.update_request(match.get("requestId"), {
                    "report_count": match["report_count"], "last_report_at": match["last_report_at"],
                    "report_sources": match["report_sources"]})
                return {**match, "linked_request_id": match.get("requestId"), "followup_action": "linked_duplicate"}
        result["requestId"] = self._next_request_id()
        result["report_count"] = 1
        if result.get("status") != "UNPROCESSED":
            result["status"] = "PENDING"
        result["createdAt"] = datetime.now(timezone.utc).isoformat()
        # Stays PENDING even when the location is unresolved, so it surfaces
        # in the next dispatch plan's `unassigned` list with a reason rather
        # than disappearing into a status nobody queries.
        self.resource_agent.save_request(result)
        self.current_requests.append(result)
        return result

    @staticmethod
    def _same_report(message: str, request: dict) -> bool:
        norm = lambda value: " ".join("".join(char.lower() if char.isalnum() else " " for char in (value or "")).split())
        left, right = norm(message), norm(request.get("raw_text", ""))
        return bool(left and right and SequenceMatcher(None, left, right).ratio() >= 0.82)

    def _match_existing_report(self, message: str, extracted: dict, candidates: list) -> dict | None:
        if not candidates:
            return None
        same = [r for r in candidates if self._same_report(message, r)]
        if len(same) == 1:
            return same[0]
        if len(same) > 1:
            return None
        # Follow-up linkage requires a unique explicit ward/node match.
        node = extracted.get("node_id")
        matching = [r for r in candidates if node and r.get("node_id") == node]
        if len(matching) != 1:
            return None
        candidate = matching[0]
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(candidate.get("createdAt"))).total_seconds()
        except (TypeError, ValueError):
            return None
        same_needs = bool(set(extracted.get("needs", [])) & set(candidate.get("needs", [])))
        return candidate if age <= 1800 and same_needs else None

    def _release_cancelled_assignment(self, request: dict):
        released = set()
        for assignment in (self.current_plan or {}).get("assignments", []):
            if assignment.get("request", {}).get("requestId") != request.get("requestId"):
                continue
            resource = assignment.get("resource", {})
            rid = resource.get("resourceId")
            if rid and rid not in released:
                updates = {"available": True, "currentLoad": 0, "assignedTo": None, "destinationNode": None}
                self.resource_agent.update_resource(rid, updates)
                for item in self.resources:
                    if item.get("resourceId") == rid:
                        item.update(updates)
                released.add(rid)
            assignment["status"] = "CANCELLED" if request.get("status") in ("CANCELLED", "COMPLETED") else "SUPERSEDED"
        if released:
            self._audit({"type": "assignment_resources_released", "requestId": request.get("requestId"),
                         "resourceIds": sorted(released), "timestamp": datetime.now(timezone.utc).isoformat()})

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
        self.current_plan["graph_revision"] = self.router.revision
        self.current_plan["approval_status"] = "WAITING"
        self.current_plan["approval_wait_started_at"] = self.current_plan["generatedAt"]
        self.current_plan["metrics"] = self.evaluator.score_plan(
            self.current_plan, len(pending)
        )
        self.current_plan["baseline_metrics"] = baseline
        self.current_plan["request_count"] = len(pending)
        for request in self.current_plan.get("unassigned", []):
            if request.get("status") == "UNREACHABLE":
                self.resource_agent.update_request(request.get("requestId"), {
                    "status": "UNREACHABLE", "dispatch_status": "UNREACHABLE",
                    "escalation_required": True,
                })
        return self.current_plan

    def get_current_plan(self) -> dict:
        if not self.current_plan:
            return {"error": "No active dispatch plan"}
        generated = datetime.fromisoformat(self.current_plan["approval_wait_started_at"])
        now = datetime.now(timezone.utc)
        waiting = max(0, int((now - generated).total_seconds()))
        critical = any(
            config.normalize_urgency(item.get("request", item).get("urgency")) == "CRITICAL"
            for item in (self.current_plan.get("assignments", []) + self.current_plan.get("unassigned", []))
        )
        self.current_plan["approval_wait_seconds"] = waiting
        if (critical and self.current_plan.get("approval_status") != "APPROVED"
                and self.current_plan.get("status") not in ("APPROVED", "STALE", "REJECTED", "REPLAN_PENDING", "REPLAN_PENDING_APPROVAL")
                and waiting >= 300
                and now.timestamp() - self._plan_reminders.get(self.current_plan.get("planId"), 0) >= 300):
            self._plan_reminders[self.current_plan["planId"]] = now.timestamp()
            self.current_plan["last_approval_reminder_at"] = now.isoformat()
            self.current_plan["approval_reminder_count"] = int(self.current_plan.get("approval_reminder_count", 0)) + 1
            event = {"type": "critical_plan_approval_reminder", "planId": self.current_plan.get("planId"),
                     "waiting_seconds": waiting, "timestamp": now.isoformat(),
                     "message": "Critical plan is still awaiting coordinator approval. No dispatch was started."}
            self._audit(event)
        return self.current_plan

    def _audit(self, event: dict):
        self.events.append(event)
        try:
            event["audit_storage"] = self.resource_agent.save_event(event)
        except Exception as exc:
            event["audit_storage_error"] = str(exc)

    def review_plan(
        self, decision: str, coordinator: str, reason: str = "",
        exclude_resource_ids: list[str] | None = None,
        request_updates: dict | None = None,
    ) -> dict:
        with self._approval_lock:
            return self._review_plan_locked(decision, coordinator, reason, exclude_resource_ids, request_updates)

    def _review_plan_locked(
        self, decision: str, coordinator: str, reason: str = "",
        exclude_resource_ids: list[str] | None = None,
        request_updates: dict | None = None,
    ) -> dict:
        if not self.current_plan:
            return {"error": "No active dispatch plan to review"}
        if decision not in ("reject", "edit"):
            return {"error": "Decision must be 'reject' or 'edit'"}
        if self.current_plan.get("graph_revision") != self.router.revision:
            return {"error": "Plan is stale because roads changed; generate a fresh plan before editing it."}

        prior_plan_id = self.current_plan.get("planId")
        pending = []
        seen = set()
        plan_requests = [a.get("request", {}) for a in self.current_plan.get("assignments", [])]
        plan_requests.extend(self.current_plan.get("unassigned", []))
        for req in plan_requests:
            rid = req.get("requestId")
            if rid not in seen and req.get("status") in (None, "PENDING"):
                seen.add(rid)
                pending.append(req)
        excluded = set(exclude_resource_ids or [])
        if decision == "reject" and not excluded:
            excluded = {a.get("resource", {}).get("resourceId") for a in self.current_plan.get("assignments", [])}
        known_ids = {r.get("resourceId") for r in self.resources}
        if excluded - known_ids:
            return {"error": f"Unknown resource exclusion(s): {', '.join(sorted(excluded - known_ids))}"}

        allowed_edits = {"people_count", "urgency", "vulnerable", "vulnerable_details", "needs", "node_id", "location_description"}
        for rid, changes in (request_updates or {}).items():
            request = next((r for r in pending if r.get("requestId") == rid), None)
            if request is None:
                return {"error": f"Request '{rid}' is not in the pending plan"}
            if set(changes) - allowed_edits:
                return {"error": f"Unsupported request edit fields: {', '.join(sorted(set(changes) - allowed_edits))}"}
            if "people_count" in changes and (not isinstance(changes["people_count"], int) or changes["people_count"] <= 0):
                return {"error": "Edited people_count must be a positive integer"}
            if "urgency" in changes and config.normalize_urgency(changes["urgency"]) != str(changes["urgency"]).upper():
                return {"error": "Edited urgency must be CRITICAL, HIGH, MEDIUM, or LOW"}
            if "node_id" in changes and changes["node_id"] not in config.NODE_IDS:
                return {"error": "Edited node_id must be a known district node"}
            if "vulnerable" in changes and not isinstance(changes["vulnerable"], bool):
                return {"error": "Edited vulnerable must be a boolean"}
            if "needs" in changes and (not isinstance(changes["needs"], list) or
                                       any(item not in ("evacuation", "medical", "food", "shelter", "water") for item in changes["needs"])):
                return {"error": "Edited needs must be a list of supported services"}
            request.update(changes)
            self.resource_agent.update_request(rid, changes)

        self.current_plan["status"] = "REJECTED" if decision == "reject" else "EDITED"
        event = {"type": "plan_coordinator_decision", "decision": decision,
                 "planId": prior_plan_id, "coordinator": coordinator, "reason": reason,
                 "excluded_resources": sorted(excluded), "request_updates": request_updates or {},
                 "timestamp": datetime.now(timezone.utc).isoformat()}
        self._audit(event)

        replanned = self.dispatch.create_plan(pending, exclude_resource_ids=excluded)
        replanned["planId"] = f"PLAN_{uuid.uuid4().hex[:8]}"
        replanned["generatedAt"] = datetime.now(timezone.utc).isoformat()
        replanned["approval_wait_started_at"] = replanned["generatedAt"]
        replanned["approval_status"] = "WAITING"
        replanned["graph_revision"] = self.router.revision
        replanned["review_of"] = prior_plan_id
        replanned["review_decision"] = decision
        replanned["request_count"] = len(pending)
        replanned["metrics"] = self.evaluator.score_plan(replanned, len(pending))
        self.current_plan = replanned
        return {"decision": decision, "audit_event": event, "plan": replanned}

    def approve_plan(self, approved_by: str = "coordinator") -> dict:
        with self._approval_lock:
            return self._approve_plan_locked(approved_by)

    def _approve_plan_locked(self, approved_by: str) -> dict:
        """Coordinator approval, the only path that commits a plan.

        Writes assignments to Firestore, marks resources busy and requests
        assigned. Nothing is committed until this is called.
        """
        if not self.current_plan:
            return {"error": "No active dispatch plan to approve"}

        if self.current_plan.get("graph_revision") != self.router.revision:
            self.current_plan["status"] = "STALE"
            event = {"type": "plan_rejected_stale", "planId": self.current_plan.get("planId"),
                     "plannedGraphRevision": self.current_plan.get("graph_revision"),
                     "currentGraphRevision": self.router.revision,
                     "timestamp": datetime.now(timezone.utc).isoformat()}
            self._audit(event)
            return {"error": "Plan is stale because roads changed after planning; generate a new plan before approval."}

        reservation = self.resource_agent.reserve_plan_resources(
            self.current_plan.get("assignments", [])
        )
        if not reservation.get("ok"):
            event = {"type": "plan_rejected_unavailable_resource", "planId": self.current_plan.get("planId"),
                     "reason": reservation.get("reason"),
                     "timestamp": datetime.now(timezone.utc).isoformat()}
            self._audit(event)
            shelter_failure = re.search(r"Shelter ([A-Za-z0-9_-]+)", reservation.get("reason", ""), re.I)
            if shelter_failure:
                failed_shelter_id = shelter_failure.group(1)
                for shelter in self.resources:
                    if shelter.get("resourceId") == failed_shelter_id:
                        shelter["available"] = False
                        shelter["status"] = "UNAVAILABLE"
                requests = []
                seen = set()
                for assignment in self.current_plan.get("assignments", []):
                    req = assignment.get("request", {})
                    if req.get("status") in (None, "PENDING") and req.get("requestId") not in seen:
                        requests.append(req)
                        seen.add(req.get("requestId"))
                requests.extend(req for req in self.current_plan.get("unassigned", []) if req.get("requestId") not in seen)
                replacement = self.dispatch.create_plan(requests)
                replacement.update({"planId": f"PLAN_{uuid.uuid4().hex[:8]}",
                                    "generatedAt": datetime.now(timezone.utc).isoformat(),
                                    "approval_wait_started_at": datetime.now(timezone.utc).isoformat(),
                                    "approval_status": "WAITING", "graph_revision": self.router.revision,
                                    "review_of": self.current_plan.get("planId"),
                                    "request_count": len(requests)})
                replacement["metrics"] = self.evaluator.score_plan(replacement, len(requests))
                self.current_plan = replacement
                self._audit({"type": "shelter_replan_after_approval_conflict",
                             "failed_shelter": failed_shelter_id, "newPlanId": replacement["planId"],
                             "timestamp": datetime.now(timezone.utc).isoformat()})
                return {"replanned": True, "reason": reservation.get("reason"), "plan": replacement,
                        "message": "Shelter availability changed. Alternate plan awaits coordinator approval."}
            return {"error": reservation.get("reason", "A resource is no longer available")}
        for rid, updates in reservation.get("updates", {}).items():
            local = next((r for r in self.resources if r.get("resourceId") == rid), None)
            if local:
                local.update(updates)

        approved = []
        now = datetime.now(timezone.utc).isoformat()

        for assignment in self.current_plan.get("assignments", []):
            if assignment.get("status") not in ("PENDING_APPROVAL", "PARTIAL"):
                continue
            resource = assignment["resource"]
            request = assignment["request"]
            route = assignment.get("route", {})

            record = {
                "assignmentId": f"ASG_{uuid.uuid4().hex[:8]}",
                "requestId": request.get("requestId"),
                "resourceIds": [resource["resourceId"]] + ([assignment["shelter"]["resourceId"]] if assignment.get("shelter") else []),
                "shelterId": assignment.get("shelter", {}).get("resourceId"),
                "wave": assignment.get("wave"),
                "wave_count": assignment.get("wave_count"),
                "status": "APPROVED",
                "eta_minutes": route.get("minutes"),
                "route": route.get("path", []),
                "explanation": assignment.get("explanation", ""),
                "approvedBy": approved_by,
                "approvedAt": now,
            }
            self.resource_agent.save_assignment(record)
            self._session_assignment_ids.add(record["assignmentId"])

            updates = {"destinationNode": (route.get("path") or [None])[-1]}
            resource.update(updates)
            self.resource_agent.update_resource(resource["resourceId"], updates)

            request["status"] = "ASSIGNED"
            request["assignedResources"] = sorted(set(request.get("assignedResources", [])) | {resource["resourceId"]})
            # Write the status through. Keeping it in memory only meant that
            # after a restart this request reappeared as PENDING and was
            # dispatched a second time while the fleet was still busy.
            self.resource_agent.update_request(
                request.get("requestId"),
                {
                    "status": "ASSIGNED",
                    "assignedResources": request["assignedResources"],
                    "assignmentId": record["assignmentId"],
                    "assignedAt": now,
                },
            )
            assignment["approval_status"] = "APPROVED"
            if assignment.get("status") != "PARTIAL":
                assignment["status"] = "APPROVED"
            approved.append(record)

        self.current_plan["approval_status"] = "APPROVED"
        self.current_plan["status"] = "PARTIAL" if any(
            a.get("status") == "PARTIAL" and a.get("approval_status") == "APPROVED"
            for a in self.current_plan.get("assignments", [])
        ) else "APPROVED"
        self._audit({"type": "plan_approved", "count": len(approved),
                     "planId": self.current_plan.get("planId"),
                     "approvedBy": approved_by, "timestamp": now})
        return {
            "planId": self.current_plan.get("planId"),
            "plan_status": self.current_plan.get("status"),
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

        matches = [a for a in self.current_plan.get("assignments", [])
                   if a.get("request", {}).get("requestId") == request_id]
        if not matches:
            return {"error": f"No assignment found for request '{request_id}'"}
        if all(a.get("status") == "COMPLETED" for a in matches):
            return {"error": f"{request_id} is already completed"}
        if any(a.get("status") not in ("APPROVED", "PARTIAL") or a.get("approval_status") not in (None, "APPROVED") for a in matches):
            return {"error": f"{request_id} has not been approved yet"}
        now = datetime.now(timezone.utc).isoformat()
        released = []
        for match in matches:
            resource = match["resource"]
            route = match.get("route", {})
            arrived_at = (route.get("path") or [None])[-1]
            updates = {"available": True, "currentLoad": 0, "assignedTo": None, "currentNode": arrived_at}
            self.resource_agent.update_resource(resource["resourceId"], updates)
            for local in self.resources:
                if local.get("resourceId") == resource["resourceId"]:
                    local.update(updates)
                    break
            match["status"] = "COMPLETED"
            released.append({"resourceId": resource["resourceId"], "currentNode": arrived_at})
        matches[0]["request"]["status"] = "COMPLETED"
        self.resource_agent.update_request(
            request_id, {"status": "COMPLETED", "completedAt": now}
        )
        self.events.append(
            {"type": "assignment_completed", "requestId": request_id, "timestamp": now}
        )
        return {
            "requestId": request_id,
            "resourceId": released[0]["resourceId"],
            "released_resources": released,
            "released": True,
            "completedAt": now,
        }

    def mark_resource_unavailable(self, resource_id: str, reason: str) -> dict:
        """Remove a failed responder and prepare a human-approved replacement plan."""
        with self._approval_lock:
            resource = next((r for r in self.resources if r.get("resourceId") == resource_id), None)
            if not resource:
                return {"error": f"Unknown resource '{resource_id}'"}
            resource.update({"available": False, "status": "UNAVAILABLE", "unavailable_reason": reason})
            self.resource_agent.update_resource(resource_id, {
                "available": False, "status": "UNAVAILABLE", "unavailable_reason": reason,
                "currentLoad": 0, "assignedTo": None,
            })
            affected = []
            prior_assignments = (self.current_plan or {}).get("assignments", [])
            for assignment in prior_assignments:
                if assignment.get("resource", {}).get("resourceId") != resource_id:
                    continue
                if assignment.get("status") in ("COMPLETED", "CANCELLED", "UNAVAILABLE"):
                    continue
                assignment["status"] = "UNAVAILABLE"
                req = assignment.get("request", {})
                req["status"] = "PENDING"
                req.pop("assignedResources", None)
                self.resource_agent.update_request(req.get("requestId"), {"status": "PENDING", "assignedResources": []})
                if req not in affected:
                    affected.append(req)
            self._audit({"type": "resource_unavailable", "resourceId": resource_id,
                         "reason": reason, "affected_requests": [r.get("requestId") for r in affected],
                         "timestamp": datetime.now(timezone.utc).isoformat()})
            if not affected:
                return {"resourceId": resource_id, "status": "UNAVAILABLE", "replacement_plan": None}
            replanned = self.dispatch.create_plan(affected, exclude_resource_ids={resource_id})
            old_plan = self.current_plan or {}
            replanned["assignments"] = [a for a in prior_assignments if a.get("status") not in ("UNAVAILABLE", "CANCELLED")] + replanned.get("assignments", [])
            replanned["planId"] = f"PLAN_{uuid.uuid4().hex[:8]}"
            replanned["generatedAt"] = datetime.now(timezone.utc).isoformat()
            replanned["approval_wait_started_at"] = replanned["generatedAt"]
            replanned["approval_status"] = "WAITING"
            replanned["graph_revision"] = self.router.revision
            replanned["review_of"] = old_plan.get("planId")
            replanned["reason"] = "Resource failure; replacement requires coordinator approval"
            replanned["request_count"] = len(affected)
            replanned["total_assigned"] = sum(1 for a in replanned["assignments"] if a.get("status") in ("PENDING_APPROVAL", "PARTIAL", "APPROVED"))
            replanned["total_unassigned"] = len(replanned.get("unassigned", []))
            self.current_plan = replanned
            return {"resourceId": resource_id, "status": "UNAVAILABLE", "affected_requests": [r.get("requestId") for r in affected],
                    "replacement_plan": replanned, "message": "Replacement plan awaits coordinator approval; nothing was auto-dispatched."}

    # ---------- replanning ----------

    def simulate_road_closure(self, edge_id: str) -> dict:
        # Serialize graph mutations against approval's revision check and
        # resource reservation, so a closure cannot slip between those steps.
        with self._approval_lock:
            return self._simulate_road_closure_locked(edge_id)

    def _simulate_road_closure_locked(self, edge_id: str) -> dict:
        if edge_id not in self.graph_data.get("edges", {}):
            return {"error": f"Unknown edge '{edge_id}'"}
        changed = self.router.close_edge(edge_id)
        if not changed:
            return {"edge_id": edge_id, "closed": True, "status": "already_closed"}
        if not self.current_plan:
            self.events.append({"type": "road_closure", "affected": edge_id,
                               "timestamp": datetime.now(timezone.utc).isoformat(),
                               "triggeredReplan": False})
            return {"edge_id": edge_id, "closed": True, "status": "closed_no_active_plan"}
        return self._queue_network_replan(edge_id, closed=True)

    def _queue_network_replan(self, edge_id: str, closed: bool) -> dict:
        with self._closure_lock:
            self._closure_changes.add(edge_id)
            self.last_replan = None
            if self.current_plan:
                self.current_plan["status"] = "REPLAN_PENDING"
            if self._closure_timer:
                self._closure_timer.cancel()
            self._closure_deadline = datetime.now(timezone.utc).timestamp() + self._closure_debounce_seconds
            self._closure_timer = threading.Timer(self._closure_debounce_seconds, self._flush_network_replan)
            self._closure_timer.daemon = True
            self._closure_timer.start()
        event = {"type": "road_closed" if closed else "road_reopened", "affected": edge_id,
                 "timestamp": datetime.now(timezone.utc).isoformat(),
                 "graph_revision": self.router.revision, "replanScheduled": True}
        self.events.append(event)
        return {"edge_id": edge_id, "closed": closed, "status": "REPLAN_SCHEDULED",
                "pending_edges": sorted(self._closure_changes),
                "debounce_seconds": self._closure_debounce_seconds,
                "graph_revision": self.router.revision,
                "summary": f"Network change queued; routes will be recomputed once after {self._closure_debounce_seconds} seconds without another change."}

    def _flush_network_replan(self):
        with self._approval_lock:
            with self._closure_lock:
                changed_edges = sorted(self._closure_changes)
                self._closure_changes.clear()
                self._closure_timer = None
                self._closure_deadline = None
            if not changed_edges or not self.current_plan:
                return
            assignments = self.current_plan.get("assignments", [])
            try:
                result = self.replan.simulate_road_closure(
                    changed_edges, assignments, already_applied=True, replan_all=True
                )
                result["status"] = "READY"
                result["graph_revision"] = self.router.revision
                self.last_replan = result
                self.current_plan["status"] = "REPLAN_PENDING_APPROVAL"
                self.current_plan["replan_graph_revision"] = self.router.revision
                event = {"type": "network_replan_ready", "affected_edges": changed_edges,
                         "affected_count": result.get("affected_count", 0),
                         "alerts": result.get("alerts", []),
                         "timestamp": datetime.now(timezone.utc).isoformat()}
                self.events.append(event)
            except Exception as exc:
                self.events.append({"type": "network_replan_failed", "affected_edges": changed_edges,
                                    "error": str(exc), "timestamp": datetime.now(timezone.utc).isoformat()})

    def approve_replan(self, approved_by: str = "coordinator") -> dict:
        with self._approval_lock:
            return self._approve_replan_locked(approved_by)

    def _approve_replan_locked(self, approved_by: str) -> dict:
        """Promote the replanned routes into the current plan."""
        if not self.last_replan:
            if self._closure_changes:
                return {"error": "Network changes are being coalesced; wait for the replan to finish."}
            return {"error": "No replan to approve"}
        if self.last_replan.get("graph_revision") != self.router.revision:
            return {"error": "Replan is stale because roads changed again; wait for a fresh replan."}

        updated = 0
        for assignment in self.last_replan.get("assignments", []):
            if assignment.get("status") in ("REPLANNED", "RETURNING") and assignment.get("new_route"):
                assignment["route"] = assignment["new_route"]
                resource = assignment.get("resource", {})
                destination = (assignment["route"].get("path") or [None])[-1]
                resource["destinationNode"] = destination
                self.resource_agent.update_resource(resource.get("resourceId"), {"destinationNode": destination})
                updated += 1

        self.current_plan["assignments"] = self.last_replan["assignments"]
        self.current_plan["graph_revision"] = self.router.revision
        self.current_plan["status"] = "APPROVED" if all(a.get("status") not in ("BLOCKED", "RETURNING") for a in self.current_plan["assignments"]) else "PARTIAL"
        self._audit({"type": "replan_approved", "approvedBy": approved_by,
                     "updated_routes": updated, "graph_revision": self.router.revision,
                     "timestamp": datetime.now(timezone.utc).isoformat()})
        return {
            "updated_routes": updated,
            "approvedBy": approved_by,
            "approvedAt": datetime.now(timezone.utc).isoformat(),
        }

    def reopen_edge(self, edge_id: str) -> dict:
        with self._approval_lock:
            return self._reopen_edge_locked(edge_id)

    def _reopen_edge_locked(self, edge_id: str) -> dict:
        if edge_id not in self.graph_data.get("edges", {}):
            return {"error": f"Unknown edge '{edge_id}'"}
        changed = self.router.open_edge(edge_id)
        if not changed:
            return {"edge_id": edge_id, "closed": False, "status": "already_open"}
        if not self.current_plan:
            self.events.append({"type": "road_reopened", "affected": edge_id,
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "triggeredReplan": False})
            return {"edge_id": edge_id, "closed": False, "status": "reopened_no_active_plan"}
        return self._queue_network_replan(edge_id, closed=False)

    def get_replan_status(self) -> dict:
        if self.last_replan:
            return self.last_replan
        if self._closure_changes:
            return {"status": "REPLAN_SCHEDULED", "pending_edges": sorted(self._closure_changes),
                    "seconds_remaining": max(0, int((self._closure_deadline or 0) - datetime.now(timezone.utc).timestamp()))}
        return {"status": "IDLE"}

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
        with self._closure_lock:
            if self._closure_timer:
                self._closure_timer.cancel()
            self._closure_timer = None
            self._closure_deadline = None
            self._closure_changes.clear()
        removed_requests = self.resource_agent.delete_docs(
            "requests", [r.get("requestId") for r in self.current_requests]
        )
        removed_assignments = self.resource_agent.delete_docs(
            "assignments", list(self._session_assignment_ids)
        )

        self.current_requests = []
        self.current_plan = None
        self.last_replan = None
        self._plan_reminders.clear()
        self.events = []
        self._req_counter = self._req_id_base
        self._session_assignment_ids = set()
        reset_closed_edges = set(self.graph_data.get("closedEdges", []))
        if self.router.closed_edges != reset_closed_edges:
            self.router.revision += 1
            self.router.closed_edges = reset_closed_edges

        freed = 0
        for r in self.resources:
            if r.get("type") == "shelter":
                occupancy = config.resource_load(r)
                capacity = config.resource_capacity(r)
                powered = r.get("powerAvailable", r.get("power_available", True)) is not False
                r["available"] = occupancy < capacity and powered
                continue
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
            {"resourceId": "SHELTER_01", "type": "shelter", "name": "Government School",
             "totalCapacity": 200, "currentOccupancy": 45, "capacity": 200,
             "location": "Zone_A_School", "node_id": "N07", "available": True,
             "facilities": ["food", "water", "medical", "toilets"], "powerAvailable": True},
            {"resourceId": "SHELTER_02", "type": "shelter", "name": "Community Hall",
             "totalCapacity": 150, "currentOccupancy": 30, "capacity": 150,
             "location": "Zone_B_Community_Hall", "node_id": "N08", "available": True,
             "facilities": ["food", "water", "toilets"], "powerAvailable": True},
            {"resourceId": "SHELTER_03", "type": "shelter", "name": "Sports Complex",
             "totalCapacity": 500, "currentOccupancy": 120, "capacity": 500,
             "location": "Central_Sports_Complex", "node_id": "N01", "available": True,
             "facilities": ["food", "water", "medical", "toilets", "generator"], "powerAvailable": True},
        ]
