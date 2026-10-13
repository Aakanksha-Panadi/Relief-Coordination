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
        self._approved_plans = self.resource_agent.get_approved_plans()

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
        # Pending demo-clock check-ins, cancelled on reset so a finished
        # run cannot file arrivals against the next one.
        self._mission_timers = []
        self._closure_timer = None
        self._closure_deadline = None
        self._closure_debounce_seconds = config.REPLAN_DEBOUNCE_SECONDS
        self._plan_reminders = {}
        self._session_assignment_ids = set()
        self._session_assigned_request_ids = set()
        self.current_requests = self.resource_agent.get_active_requests()
        self._active_assignments = self._restore_active_assignments(
            self.resource_agent.get_active_assignments()
        )
        self._session_assignment_ids.update(
            assignment["assignmentId"]
            for assignment in self._active_assignments
            if assignment.get("assignmentId")
        )

    def _restore_active_assignments(self, records: list) -> list:
        requests = {item.get("requestId"): item for item in self.current_requests}
        resources = {item.get("resourceId"): item for item in self.resources}
        restored = []
        for record in records:
            resource_ids = record.get("resourceIds") or []
            resource_id = resource_ids[0] if resource_ids else None
            request_id = record.get("requestId")
            resource = resources.get(resource_id)
            request = requests.get(request_id)
            if not resource or not request:
                continue
            shelter_id = record.get("shelterId")
            shelter = resources.get(shelter_id) if shelter_id else None
            restored.append({
                "assignmentId": record.get("assignmentId"),
                "request": request,
                "resource": resource,
                "shelter": shelter,
                "route": record.get("route_details") or {
                    "path": record.get("route", []),
                    "edges": [],
                    "minutes": record.get("eta_minutes", 0),
                },
                "shelter_route": record.get("shelter_route_details"),
                "wave": record.get("wave"),
                "wave_people": record.get(
                    "shelter_people",
                    request.get("people_count", 0),
                ),
                "status": record.get("status", "APPROVED"),
                "coverage_status": record.get("coverage_status", "APPROVED"),
                "approval_status": "APPROVED",
                "mission_status": record.get("mission_status", "EN_ROUTE"),
            })
        return restored

    def _next_request_id(self) -> str:
        # IDs must remain unique across processes and service instances; a
        # startup scan plus a process-local counter cannot guarantee that.
        return f"REQ_{uuid.uuid4().hex.upper()}"

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
        self._collapse_duplicate_open_requests()
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
        if match and (followup or self._same_report(message, match, result.get("translated_text", ""))):
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
            if self._same_report(message, match, result.get("translated_text", "")):
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
    def _same_report(message: str, request: dict, translated_message: str = "") -> bool:
        def norm(value):
            normalized = " ".join("".join(char.lower() if char.isalnum() else " " for char in (value or "")).split())
            return re.sub(r"\bward\s*(\d+)\b", r"ward \1", normalized)

        incoming = [norm(message), norm(translated_message)]
        existing = [norm(request.get("raw_text", "")), norm(request.get("translated_text", ""))]
        stop_words = {"a", "an", "and", "are", "at", "be", "for", "from", "help", "i", "in", "is",
                      "me", "my", "of", "on", "please", "the", "there", "to", "we", "with", "us"}
        for left in incoming:
            if not left:
                continue
            left_tokens = set(left.split()) - stop_words
            for right in existing:
                if not right:
                    continue
                if SequenceMatcher(None, left, right).ratio() >= 0.80:
                    return True
                right_tokens = set(right.split()) - stop_words
                if not left_tokens or not right_tokens:
                    continue
                shared = left_tokens & right_tokens
                jaccard = len(shared) / len(left_tokens | right_tokens)
                shorter_coverage = len(shared) / min(len(left_tokens), len(right_tokens))
                # Word overlap catches same-language paraphrases with changed
                # word order, but requires both strong overlap and shared
                # coverage so a generic "help, flood" phrase won't merge cases.
                if jaccard >= 0.68 and shorter_coverage >= 0.82:
                    return True
        return False

    def _collapse_duplicate_open_requests(self):
        """Link multilingual duplicate cases and keep only one dispatchable record."""
        open_requests = [r for r in self.current_requests if r.get("status") == "PENDING"]
        for index, canonical in enumerate(open_requests):
            if canonical.get("status") != "PENDING":
                continue
            for duplicate in open_requests[index + 1:]:
                if duplicate.get("status") != "PENDING":
                    continue
                canonical_node = canonical.get("node_id")
                duplicate_node = duplicate.get("node_id")
                if canonical_node and duplicate_node and canonical_node != duplicate_node:
                    continue
                first_count, second_count = canonical.get("people_count"), duplicate.get("people_count")
                if first_count is not None and second_count is not None and first_count != second_count:
                    continue
                first_needs = set(canonical.get("needs") or [])
                second_needs = set(duplicate.get("needs") or [])
                if first_needs and second_needs and not first_needs.intersection(second_needs):
                    continue
                if not self._same_report(
                    canonical.get("raw_text", ""), duplicate,
                    canonical.get("translated_text", ""),
                ):
                    continue

                canonical["report_count"] = int(canonical.get("report_count", 1)) + int(duplicate.get("report_count", 1))
                canonical["report_sources"] = list(canonical.get("report_sources", [])) + [
                    duplicate.get("raw_text", ""), *duplicate.get("report_sources", [])
                ]
                duplicate["status"] = "DUPLICATE"
                duplicate["duplicate_of"] = canonical.get("requestId")
                duplicate["queue_hidden"] = True
                self.resource_agent.update_request(duplicate.get("requestId"), {
                    "status": "DUPLICATE", "duplicate_of": canonical.get("requestId"),
                    "queue_hidden": True,
                })
                self.resource_agent.update_request(canonical.get("requestId"), {
                    "report_count": canonical["report_count"],
                    "report_sources": canonical["report_sources"],
                })
                self._audit({"type": "duplicate_request_linked",
                             "requestId": duplicate.get("requestId"),
                             "canonicalRequestId": canonical.get("requestId"),
                             "report_count": canonical["report_count"],
                             "timestamp": datetime.now(timezone.utc).isoformat()})

    def _match_existing_report(self, message: str, extracted: dict, candidates: list) -> dict | None:
        if not candidates:
            return None
        same = []
        for request in candidates:
            extracted_node, existing_node = extracted.get("node_id"), request.get("node_id")
            if extracted_node and existing_node and extracted_node != existing_node:
                continue
            new_count, old_count = extracted.get("people_count"), request.get("people_count")
            if new_count is not None and old_count is not None and new_count != old_count:
                continue
            new_needs, old_needs = set(extracted.get("needs") or []), set(request.get("needs") or [])
            if new_needs and old_needs and not new_needs.intersection(old_needs):
                continue
            if self._same_report(message, request, extracted.get("translated_text", "")):
                same.append(request)
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
        for assignment in self._tracked_assignments():
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
            result = self._release_shelter_reservations([assignment])
            if not result.get("ok"):
                self._audit({"type": "shelter_reservation_release_failed",
                             "requestId": request.get("requestId"),
                             "reason": result.get("reason"),
                             "timestamp": datetime.now(timezone.utc).isoformat()})
            assignment["status"] = "CANCELLED" if request.get("status") in ("CANCELLED", "COMPLETED") else "SUPERSEDED"
            self.resource_agent.update_assignment(
                assignment.get("assignmentId"),
                {"status": assignment["status"]},
            )
        if released:
            self._audit({"type": "assignment_resources_released", "requestId": request.get("requestId"),
                         "resourceIds": sorted(released), "timestamp": datetime.now(timezone.utc).isoformat()})

    def _tracked_assignments(self) -> list:
        """Return active history and current draft assignments without duplicates."""
        assignments = list(self._active_assignments)
        known_ids = {
            assignment.get("assignmentId")
            for assignment in assignments
            if assignment.get("assignmentId")
        }
        known_objects = {id(assignment) for assignment in assignments}
        for assignment in (self.current_plan or {}).get("assignments", []):
            assignment_id = assignment.get("assignmentId")
            if (id(assignment) not in known_objects
                    and (not assignment_id or assignment_id not in known_ids)):
                assignments.append(assignment)
                known_objects.add(id(assignment))
                if assignment_id:
                    known_ids.add(assignment_id)
        return assignments

    def _release_shelter_reservations(self, assignments: list) -> dict:
        reservations = {}
        for assignment in assignments:
            if (assignment.get("approval_status") != "APPROVED"
                    or assignment.get("status") in (
                        "COMPLETED", "CANCELLED", "SUPERSEDED", "UNAVAILABLE"
                    )
                    or assignment.get("mission_status") == "DELIVERED"):
                continue
            shelter_id = (assignment.get("shelter") or {}).get("resourceId")
            if not shelter_id:
                continue
            people = assignment.get(
                "wave_people",
                assignment.get("request", {}).get("people_count", 0),
            ) or 0
            reservations[shelter_id] = reservations.get(shelter_id, 0) + people
        if not reservations:
            return {"ok": True, "updates": {}}

        result = self.resource_agent.release_shelter_reservations(reservations)
        if result.get("ok"):
            for resource_id, updates in result.get("updates", {}).items():
                local = next(
                    (item for item in self.resources
                     if item.get("resourceId") == resource_id),
                    None,
                )
                if local:
                    local.update(updates)
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
        if not self.current_requests:
            self.current_requests = self.resource_agent.get_active_requests()
        self._collapse_duplicate_open_requests()
        requests = self.current_requests
        # Assignment metadata is authoritative if a persisted status write was
        # delayed or failed; don't return an assigned case as queue-pending.
        for request in requests:
            if (request.get("requestId") in self._session_assigned_request_ids
                    or request.get("assignedResources") or request.get("assignmentId")):
                request["status"] = "ASSIGNED"
        if status:
            requests = [r for r in requests if r.get("status") == status]
        return requests

    def review_request(self, request_id: str, updates: dict,
                       reviewed_by: str = "coordinator") -> dict:
        with self._approval_lock:
            return self._review_request_locked(request_id, updates, reviewed_by)

    def _review_request_locked(self, request_id: str, updates: dict,
                               reviewed_by: str) -> dict:
        """Correct an uncertain extraction and explicitly return it to queue."""
        if not self.current_requests:
            self.current_requests = self.resource_agent.get_active_requests()
        request = next(
            (item for item in self.current_requests
             if item.get("requestId") == request_id),
            None,
        )
        if request is None:
            return {"error": f"Request '{request_id}' was not found"}
        if request.get("status") not in ("UNPROCESSED", "PENDING", "UNREACHABLE"):
            return {"error": f"Request '{request_id}' is not awaiting review"}

        allowed_fields = {
            "people_count", "urgency", "vulnerable", "vulnerable_details",
            "needs", "node_id", "location_description",
        }
        if not isinstance(updates, dict) or set(updates) - allowed_fields:
            return {"error": "Review contains unsupported request fields"}
        people_count = updates.get("people_count", request.get("people_count"))
        node_id = updates.get("node_id", request.get("node_id"))
        if not isinstance(people_count, int) or isinstance(people_count, bool) or people_count <= 0:
            return {"error": "A positive people_count is required before queueing"}
        if node_id not in config.NODE_IDS:
            return {"error": "A known node_id is required before queueing"}
        if ("urgency" in updates
                and config.normalize_urgency(updates["urgency"]) != str(updates["urgency"]).upper()):
            return {"error": "Urgency must be CRITICAL, HIGH, MEDIUM, or LOW"}
        if "vulnerable" in updates and not isinstance(updates["vulnerable"], bool):
            return {"error": "vulnerable must be a boolean"}
        if "needs" in updates and (
            not isinstance(updates["needs"], list)
            or any(item not in ("evacuation", "medical", "food", "shelter", "water")
                   for item in updates["needs"])
        ):
            return {"error": "needs must contain only supported services"}

        request.update(updates)
        request.update({
            "status": "PENDING",
            "human_review_required": False,
            "processing_flags": [],
            "reviewedBy": reviewed_by,
            "reviewedAt": datetime.now(timezone.utc).isoformat(),
        })
        self.resource_agent.update_request(request_id, {
            **updates,
            "status": "PENDING",
            "human_review_required": False,
            "processing_flags": [],
            "reviewedBy": request["reviewedBy"],
            "reviewedAt": request["reviewedAt"],
        })
        self._audit({"type": "request_reviewed", "requestId": request_id,
                     "reviewedBy": reviewed_by, "timestamp": request["reviewedAt"]})
        return request

    # ---------- dispatch ----------

    def generate_dispatch_plan(self, request_id: str | None = None) -> dict:
        if (self.current_plan
                and self.current_plan.get("approval_status") == "WAITING"
                and self.current_plan.get("status") not in ("STALE", "REJECTED", "EDITED")):
            return {"error": "Approve or reject the current plan before generating another plan."}
        if not self.current_requests:
            firestore_requests = self.resource_agent.get_pending_requests()
            if not firestore_requests:
                return {"error": "No requests to dispatch"}
            self.current_requests = firestore_requests
        self._collapse_duplicate_open_requests()

        pending = [
            r for r in self.current_requests
            if r.get("status") in (None, "PENDING")
        ]
        if not pending:
            return {"error": "All requests are already assigned"}

        # A named request is planned alone; otherwise the plan covers the
        # whole pending queue, ranked by the same urgency, vulnerability,
        # group-size, ETA and ID rules as dispatch. Planning one request at a
        # time made /metrics compare a single-request plan, where triage
        # cannot show up at all and critical_first_pct always read zero.
        if request_id:
            selected = next((r for r in pending if r.get("requestId") == request_id), None)
            if selected is None:
                return {"error": f"Request '{request_id}' is not pending and cannot be planned."}
            pending = [selected]
        else:
            pending = self.dispatch.prioritize_requests(pending)

        # First-come-first-served means arrival order, so the baseline gets
        # the queue sorted by when each call came in. Handing it the
        # already-prioritized list let the baseline inherit RescueIQ's triage,
        # which is why critical_first_pct came out identical for both and the
        # headline improvement always read zero.
        arrival_order = sorted(
            pending,
            key=lambda r: (r.get("createdAt") or "", r.get("requestId") or ""),
        )

        # Baseline is scored against the same fleet the real plan sees.
        # Computing it later, after /dispatch/approve has marked resources
        # busy, handed the baseline a depleted fleet and understated it.
        baseline = self.evaluator.score_plan(
            self.evaluator.baseline_plan(arrival_order), len(pending)
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
        self.current_plan["total_approved_assignments"] = len(self._session_assignment_ids)
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

        if self.current_plan.get("approval_status") == "APPROVED":
            return {"error": "This plan has already been approved. Generate a new plan for pending requests."}
        if self.current_plan.get("approval_status") != "WAITING":
            return {"error": "This plan is not awaiting approval. Generate a fresh plan before approving."}

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
            shelter = assignment.get("shelter") or {}

            record = {
                "assignmentId": f"ASG_{uuid.uuid4().hex[:8]}",
                "planId": self.current_plan.get("planId"),
                "requestId": request.get("requestId"),
                "resourceIds": [resource["resourceId"]] + ([shelter["resourceId"]] if shelter.get("resourceId") else []),
                "shelterId": shelter.get("resourceId"),
                "wave": assignment.get("wave"),
                "wave_count": assignment.get("wave_count"),
                "shelter_people": assignment.get(
                    "wave_people",
                    request.get("people_count", 0),
                ),
                "coverage_status": assignment.get("status", "APPROVED"),
                "status": "APPROVED",
                "mission_status": "EN_ROUTE",
                "eta_minutes": route.get("minutes"),
                "shelter_eta_minutes": assignment.get("shelter_eta_minutes"),
                "total_eta_minutes": assignment.get("total_eta_minutes"),
                "route": route.get("path", []),
                "route_details": route,
                "shelter_route_details": assignment.get("shelter_route"),
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
            if request.get("requestId"):
                self._session_assigned_request_ids.add(request["requestId"])
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
            assignment["assignmentId"] = record["assignmentId"]
            assignment["coverage_status"] = assignment.get("status", "APPROVED")
            if assignment.get("status") != "PARTIAL":
                assignment["status"] = "APPROVED"
            # Approval dispatches the vehicle; it is now travelling, not done.
            # Without a state here an assignment jumped straight from APPROVED
            # to COMPLETED, so nothing could tell a boat that had just left
            # from one about to arrive, and a replan could not know whether
            # the people were already aboard.
            assignment["mission_status"] = "EN_ROUTE"
            self._active_assignments.append(assignment)
            approved.append(record)

        self.current_plan["approval_status"] = "APPROVED"
        self.current_plan["status"] = "PARTIAL" if any(
            a.get("status") == "PARTIAL" and a.get("approval_status") == "APPROVED"
            for a in self.current_plan.get("assignments", [])
        ) else "APPROVED"
        self._audit({"type": "plan_approved", "count": len(approved),
                     "planId": self.current_plan.get("planId"),
                     "approvedBy": approved_by, "timestamp": now})
        self.current_plan["total_approved_assignments"] = len(self._session_assignment_ids)
        approved_plan = {
            "planId": self.current_plan.get("planId"),
            "approvedAt": now,
            "approvedBy": approved_by,
            "requestCount": len({assignment.get("requestId") for assignment in approved}),
            "assignmentCount": len(approved),
            "assignments": approved,
        }
        self.resource_agent.save_approved_plan(approved_plan)
        self._schedule_simulated_checkins()
        self._approved_plans = [p for p in self._approved_plans if p.get("planId") != approved_plan["planId"]]
        self._approved_plans.append(approved_plan)
        self._approved_plans.sort(key=lambda p: p.get("approvedAt", ""), reverse=True)
        return {
            "planId": self.current_plan.get("planId"),
            "plan_status": self.current_plan.get("status"),
            "approved_count": len(approved),
            "total_approved_assignments": len(self._session_assignment_ids),
            "assignments": approved,
            "approvedBy": approved_by,
            "approvedAt": now,
        }

    def get_approved_plans(self) -> dict:
        # Refresh from Firestore so approvals made by another coordinator show up.
        stored = self.resource_agent.get_approved_plans()
        known = {plan.get("planId"): plan for plan in self._approved_plans}
        known.update({plan.get("planId"): plan for plan in stored})
        plans = sorted(known.values(), key=lambda plan: plan.get("approvedAt", ""), reverse=True)
        self._approved_plans = plans
        return {"count": len(plans), "plans": plans}

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

    # ---------- mission progress ----------

    def report_progress(self, request_id: str, event: str,
                        reported_by: str = "coordinator",
                        simulated: bool = False) -> dict:
        """Log a check-in against a travelling assignment.

        Nothing can observe that a boat arrived, so arrival is always
        something a reporter tells us: a coordinator logging a radio call, a
        crew app, or a position feed. They all land here.
        """
        if event not in ("picked_up", "delivered"):
            return {"error": "event must be 'picked_up' or 'delivered'"}
        if event == "delivered":
            return self.complete_assignment(
                request_id, reported_by=reported_by, simulated=simulated
            )

        with self._approval_lock:
            matches = [
                a for a in self._tracked_assignments()
                if a.get("request", {}).get("requestId") == request_id
                and a.get("status") not in (
                    "COMPLETED", "CANCELLED", "SUPERSEDED", "UNAVAILABLE"
                )
            ]
            if not matches:
                return {"error": f"No assignment found for request '{request_id}'"}
            if any(a.get("approval_status") != "APPROVED" for a in matches):
                return {"error": f"{request_id} has not been approved yet"}
            if all(a.get("mission_status") in ("ABOARD", "DELIVERED") for a in matches):
                return {"error": f"{request_id} is already reported picked up"}

            now = datetime.now(timezone.utc).isoformat()
            aboard = []
            for match in matches:
                if match.get("mission_status") == "DELIVERED":
                    continue
                route = match.get("route", {})
                at_node = (route.get("path") or [None])[-1]
                # Reaching the caller is only half the job when a shelter is
                # involved; the vehicle is now carrying people, which is what
                # makes a later closure on the drop-off leg urgent.
                match["mission_status"] = "ABOARD" if match.get("shelter") else "AT_SCENE"
                match["picked_up_at"] = now
                self.resource_agent.update_assignment(
                    match.get("assignmentId"),
                    {"mission_status": match["mission_status"], "picked_up_at": now},
                )
                resource = match["resource"]
                resource["currentNode"] = at_node
                self.resource_agent.update_resource(
                    resource["resourceId"], {"currentNode": at_node}
                )
                aboard.append(resource["resourceId"])

            self._audit({"type": "mission_checkin", "event": "picked_up",
                         "requestId": request_id, "resourceIds": aboard,
                         "reportedBy": reported_by, "simulated": simulated,
                         "timestamp": now})
            return {"requestId": request_id, "event": "picked_up",
                    "mission_status": matches[0].get("mission_status"),
                    "resourceIds": aboard, "reportedBy": reported_by,
                    "simulated": simulated, "at": now}

    def _schedule_simulated_checkins(self):
        """Have a timer file the check-ins a crew would radio in.

        Only runs with DEMO_CLOCK on. It reports progress on assignments the
        coordinator has already approved and never approves anything itself,
        so the human-in-the-loop guarantee is unchanged.
        """
        if not config.DEMO_CLOCK or not self.current_plan:
            return
        rate = max(0.0, config.DEMO_CLOCK_SECONDS_PER_MINUTE)
        for assignment in self.current_plan.get("assignments", []):
            if assignment.get("approval_status") != "APPROVED":
                continue
            if assignment.get("mission_status") != "EN_ROUTE":
                continue
            request_id = assignment.get("request", {}).get("requestId")
            if not request_id:
                continue
            pickup = (assignment.get("route") or {}).get("minutes") or 0
            total = assignment.get("total_eta_minutes")
            if total is None:
                total = pickup
            for delay_minutes, event in ((pickup, "picked_up"), (total, "delivered")):
                timer = threading.Timer(
                    max(0.1, delay_minutes * rate),
                    self._simulated_checkin,
                    args=(request_id, event),
                )
                timer.daemon = True
                timer.start()
                self._mission_timers.append(timer)

    def _simulated_checkin(self, request_id: str, event: str):
        try:
            self.report_progress(
                request_id, event,
                reported_by="demo-clock (simulated)", simulated=True,
            )
        except Exception as exc:
            self.events.append({"type": "simulated_checkin_failed",
                                "requestId": request_id, "event": event,
                                "error": str(exc),
                                "timestamp": datetime.now(timezone.utc).isoformat()})

    def _cancel_simulated_checkins(self):
        for timer in self._mission_timers:
            timer.cancel()
        self._mission_timers = []

    def complete_assignment(self, request_id: str, reported_by: str = "coordinator",
                            simulated: bool = False) -> dict:
        """Mark a rescue finished: free the resource and move it to where it went.

        Without this there was no way back from busy except /reset, so a
        resource was effectively consumed by its first assignment and the
        fleet drained over a demo.
        """
        all_matches = [a for a in self._tracked_assignments()
                       if a.get("request", {}).get("requestId") == request_id]
        if not all_matches:
            return {"error": f"No assignment found for request '{request_id}'"}
        if all(a.get("status") == "COMPLETED" for a in all_matches):
            return {"error": f"{request_id} is already completed"}
        matches = [
            assignment for assignment in all_matches
            if assignment.get("status") not in (
                "COMPLETED", "CANCELLED", "SUPERSEDED", "UNAVAILABLE"
            )
        ]
        if not matches:
            return {"error": f"{request_id} has no active assignment"}
        if any(a.get("approval_status") != "APPROVED"
               or a.get("status") in ("BLOCKED", "RETURNING", "CANCELLED", "UNAVAILABLE")
               for a in matches):
            return {"error": f"{request_id} has not been approved yet"}
        now = datetime.now(timezone.utc).isoformat()
        released = []
        for match in matches:
            resource = match["resource"]
            route = match.get("route", {})
            # An evacuation ends at the shelter, not at the doorstep where the
            # people were picked up. Recording the pickup node left the vehicle
            # parked where it collected them while the people it carried were
            # already counted into a shelter a reroute away, so the next
            # assignment was planned from a place the vehicle had left.
            shelter_route = match.get("shelter_route") or {}
            arrived_at = (
                (shelter_route.get("path") or [None])[-1]
                if match.get("shelter") and shelter_route.get("path")
                else (route.get("path") or [None])[-1]
            )
            updates = {"available": True, "currentLoad": 0, "assignedTo": None,
                       "currentNode": arrived_at, "destinationNode": None}
            self.resource_agent.update_resource(resource["resourceId"], updates)
            for local in self.resources:
                if local.get("resourceId") == resource["resourceId"]:
                    local.update(updates)
                    break
            match["status"] = "COMPLETED"
            match["mission_status"] = "DELIVERED"
            match["delivered_at"] = now
            self.resource_agent.update_assignment(
                match.get("assignmentId"),
                {"status": "COMPLETED", "mission_status": "DELIVERED",
                 "delivered_at": now},
            )
            released.append({"resourceId": resource["resourceId"], "currentNode": arrived_at})
        matches[0]["request"]["status"] = "COMPLETED"
        self.resource_agent.update_request(
            request_id, {"status": "COMPLETED", "completedAt": now}
        )
        self._audit(
            {"type": "mission_checkin", "event": "delivered",
             "requestId": request_id, "reportedBy": reported_by,
             "simulated": simulated, "timestamp": now}
        )
        return {
            "requestId": request_id,
            "event": "delivered",
            "mission_status": "DELIVERED",
            "reportedBy": reported_by,
            "simulated": simulated,
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
            is_shelter = resource.get("type") == "shelter"
            affected = []
            prior_assignments = self._tracked_assignments()
            for assignment in prior_assignments:
                # A shelter is named in `shelter`, not `resource`. Matching
                # only on `resource` meant a flooded shelter was marked
                # unavailable while the people already approved for it stayed
                # routed there, with no replan and no alert.
                carrier_id = assignment.get("resource", {}).get("resourceId")
                shelter_id = (assignment.get("shelter") or {}).get("resourceId")
                if resource_id not in (carrier_id, shelter_id):
                    continue
                if assignment.get("status") in ("COMPLETED", "CANCELLED", "UNAVAILABLE"):
                    continue
                assignment["status"] = "UNAVAILABLE"
                self.resource_agent.update_assignment(
                    assignment.get("assignmentId"), {"status": "UNAVAILABLE"}
                )
                req = assignment.get("request", {})
                req["status"] = "PENDING"
                req.pop("assignedResources", None)
                self.resource_agent.update_request(req.get("requestId"), {"status": "PENDING", "assignedResources": []})
                if shelter_id == resource_id and carrier_id:
                    # The vehicle is fine, only its destination failed. Hand it
                    # back to the pool or the replan has nothing to drive with,
                    # and give the shelter back the beds nobody will occupy.
                    people = assignment.get("wave_people", req.get("people_count", 0)) or 0
                    freed = {"available": True, "currentLoad": 0, "assignedTo": None, "destinationNode": None}
                    self.resource_agent.update_resource(carrier_id, freed)
                    for item in self.resources:
                        if item.get("resourceId") == carrier_id:
                            item.update(freed)
                    remaining = max(0, config.resource_load(resource) - people)
                    resource["currentOccupancy"] = remaining
                    self.resource_agent.update_resource(resource_id, {"currentOccupancy": remaining})
                if req not in affected:
                    affected.append(req)
            self._audit({"type": "shelter_unavailable" if is_shelter else "resource_unavailable",
                         "resourceId": resource_id,
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
                "summary": f"Network change queued; routes will be recomputed once after {self._closure_debounce_seconds:g} seconds without another change."}

    def _flush_network_replan(self):
        with self._approval_lock:
            with self._closure_lock:
                changed_edges = sorted(self._closure_changes)
                self._closure_changes.clear()
                self._closure_timer = None
                self._closure_deadline = None
            if not changed_edges or not self.current_plan:
                return
            assignments = self._tracked_assignments()
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
            if (assignment.get("approval_status") == "APPROVED"
                    and assignment.get("status") in ("REPLANNED", "UNCHANGED")):
                assignment["status"] = assignment.get("coverage_status", "APPROVED")
            if assignment.get("approval_status") == "APPROVED":
                self.resource_agent.update_assignment(
                    assignment.get("assignmentId"),
                    {
                        "status": assignment.get("status"),
                        "route_details": assignment.get("route"),
                        "mission_status": assignment.get("mission_status"),
                    },
                )

        self.current_plan["assignments"] = self.last_replan["assignments"]
        replanned_by_id = {
            assignment.get("assignmentId"): assignment
            for assignment in self.last_replan["assignments"]
            if assignment.get("assignmentId")
        }
        for index, assignment in enumerate(self._active_assignments):
            updated_assignment = replanned_by_id.get(assignment.get("assignmentId"))
            if updated_assignment:
                self._active_assignments[index] = updated_assignment
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
        with self._approval_lock:
            return self._reset_locked()

    def _reset_locked(self) -> dict:
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
        self._cancel_simulated_checkins()
        # Clear the collection rather than the in-memory ids: requests stored
        # under an auto-id, and statuses that never enter the queue, both
        # survived the old by-requestId delete and reappeared next run.
        released_shelters = self._release_shelter_reservations(
            self._active_assignments
        )
        if not released_shelters.get("ok"):
            return {"error": "Could not release outstanding shelter reservations: "
                             + released_shelters.get("reason", "unknown error")}
        removed_requests = self.resource_agent.delete_all_requests()
        removed_assignments = self.resource_agent.delete_docs(
            "assignments", list(self._session_assignment_ids)
        )
        removed_plans = self.resource_agent.delete_approved_plans()
        self._approved_plans = []

        self.current_requests = []
        self.current_plan = None
        self._active_assignments = []
        self.last_replan = None
        self._plan_reminders.clear()
        self.events = []
        self._session_assignment_ids = set()
        self._session_assigned_request_ids = set()
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
                # An idle vehicle can still carry a destination from a run
                # that ended by another path. Without this it is never
                # rewritten, so the stale destination outlives every reset.
                or r.get("destinationNode")
                or r.get("assignedTo")
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
            "approved_plans_removed": removed_plans,
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
