import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from app.agents.intake_agent import IntakeAgent
from app.agents.resource_agent import ResourceAgent
from app.main_orchestrator import RescueIQOrchestrator


class RegressionTests(unittest.TestCase):
    def test_mock_hindi_and_tamil_requests_remain_dispatchable(self):
        samples = (
            ("मेरे घर में पानी भर गया है, 4 लोग हैं, बुजुर्ग माँ है चल नहीं सकती, Ward 7 मंदिर के पास",
             4, "N05"),
            ("உதவி! நாங்கள் 5 பேர் சிக்கிக்கொண்டோம், குழந்தைகள் உள்ளனர், Ward 3 கோயில் தெரு",
             5, "N03"),
        )

        for message, count, node in samples:
            with self.subTest(message=message):
                extracted = IntakeAgent._mock_extract(None, message)
                request = IntakeAgent._validate_extraction(message, extracted)
                self.assertEqual(request["status"], "PENDING")
                self.assertEqual(request["people_count"], count)
                self.assertEqual(request["node_id"], node)

    def test_request_ids_are_unique_without_startup_state(self):
        orchestrator = RescueIQOrchestrator.__new__(RescueIQOrchestrator)

        request_ids = {orchestrator._next_request_id() for _ in range(1000)}

        self.assertEqual(len(request_ids), 1000)

    def test_reset_clears_all_assignment_documents(self):
        deleted = []
        documents = [
            SimpleNamespace(reference=SimpleNamespace(
                delete=lambda index=index: deleted.append(index),
            ))
            for index in range(3)
        ]
        collection = SimpleNamespace(stream=lambda: documents)
        resource_agent = ResourceAgent(None)
        resource_agent.db = SimpleNamespace(
            collection=Mock(return_value=collection),
        )

        removed = resource_agent.delete_all_assignments()

        self.assertEqual(removed, 3)
        self.assertEqual(deleted, [0, 1, 2])

    def test_approving_one_request_leaves_other_requests_waiting(self):
        resource = {"resourceId": "BOAT_01", "name": "Rescue Boat 1"}
        assignments = [
            {
                "request": {"requestId": request_id, "people_count": 2},
                "resource": resource.copy(),
                "route": {"path": ["N01", "N02"], "minutes": 5},
                "status": "PENDING_APPROVAL",
            }
            for request_id in ("REQ_A", "REQ_B")
        ]
        orchestrator = RescueIQOrchestrator.__new__(RescueIQOrchestrator)
        orchestrator.current_plan = {
            "planId": "PLAN_TEST",
            "assignments": assignments,
            "approval_status": "WAITING",
            "graph_revision": 1,
        }
        orchestrator.router = SimpleNamespace(revision=1)
        orchestrator._approval_lock = threading.RLock()
        orchestrator._session_assignment_ids = set()
        orchestrator._session_assigned_request_ids = set()
        orchestrator._active_assignments = []
        orchestrator._approved_plans = []
        orchestrator.resources = [resource]
        orchestrator.resource_agent = SimpleNamespace(
            reserve_plan_resources=Mock(return_value={"ok": True, "updates": {}}),
            save_assignment=Mock(),
            update_resource=Mock(),
            update_request=Mock(),
            save_approved_plan=Mock(),
        )
        orchestrator._audit = Mock()
        orchestrator._schedule_simulated_checkins = Mock()

        result = orchestrator.approve_plan(request_id="REQ_A")

        self.assertEqual(result["approved_count"], 1)
        self.assertEqual(result["approval_status"], "WAITING")
        self.assertEqual(assignments[0]["approval_status"], "APPROVED")
        self.assertIsNone(assignments[1].get("approval_status"))
        reserved = orchestrator.resource_agent.reserve_plan_resources.call_args.args[0]
        self.assertEqual([a["request"]["requestId"] for a in reserved], ["REQ_A"])

    def test_network_replan_only_updates_unapproved_assignments(self):
        approved = {
            "assignmentId": "ASG_APPROVED",
            "request": {"requestId": "REQ_APPROVED"},
            "resource": {"resourceId": "BOAT_01"},
            "route": {"path": ["N01", "N02"], "edges": ["OLD_EDGE"]},
            "approval_status": "APPROVED",
            "status": "APPROVED",
        }
        pending = {
            "request": {"requestId": "REQ_PENDING"},
            "resource": {"resourceId": "BOAT_02"},
            "route": {"path": ["N03", "N04"], "edges": ["CLOSED_EDGE"]},
            "status": "PENDING_APPROVAL",
        }
        rerouted = {
            **pending,
            "old_route": pending["route"],
            "new_route": {
                "path": ["N03", "N05", "N04"],
                "edges": ["ALT_1", "ALT_2"],
                "minutes": 8,
            },
            "status": "REPLANNED",
        }
        orchestrator = RescueIQOrchestrator.__new__(RescueIQOrchestrator)
        orchestrator.current_plan = {
            "assignments": [approved, pending],
            "approval_status": "WAITING",
            "status": "REPLAN_PENDING",
        }
        orchestrator._active_assignments = [approved]
        orchestrator._approval_lock = threading.RLock()
        orchestrator._closure_lock = threading.Lock()
        orchestrator._closure_changes = {"CLOSED_EDGE"}
        orchestrator._closure_timer = None
        orchestrator._closure_deadline = None
        orchestrator.router = SimpleNamespace(revision=2)
        orchestrator.replan = SimpleNamespace(
            simulate_road_closure=Mock(return_value={
                "assignments": [rerouted],
                "affected_count": 1,
            })
        )
        orchestrator.events = []

        orchestrator._flush_network_replan()

        replan_call = orchestrator.replan.simulate_road_closure.call_args
        self.assertEqual(replan_call.args[1], [pending])
        self.assertTrue(replan_call.kwargs["replan_all"])
        self.assertEqual(
            [item["request"]["requestId"]
             for item in orchestrator.last_replan["assignments"]],
            ["REQ_PENDING"],
        )

        orchestrator.resource_agent = SimpleNamespace(
            update_resource=Mock(),
            update_assignment=Mock(),
        )
        orchestrator._audit = Mock()
        result = orchestrator._approve_replan_locked("coordinator")

        self.assertEqual(result["updated_routes"], 1)
        self.assertEqual(len(orchestrator.current_plan["assignments"]), 2)
        current_approved, current_pending = orchestrator.current_plan["assignments"]
        self.assertEqual(current_approved["route"]["edges"], ["OLD_EDGE"])
        self.assertEqual(current_pending["route"]["edges"], ["ALT_1", "ALT_2"])
        self.assertEqual(current_pending["status"], "PENDING_APPROVAL")
        self.assertEqual(current_pending.get("approval_status"), None)
        self.assertEqual(orchestrator.current_plan["approval_status"], "WAITING")

    def test_mission_progress_is_saved_in_approved_plan_history(self):
        history = {
            "planId": "PLAN_TEST",
            "assignments": [{
                "assignmentId": "ASG_TEST",
                "requestId": "REQ_TEST",
                "status": "APPROVED",
                "mission_status": "EN_ROUTE",
            }],
        }
        orchestrator = RescueIQOrchestrator.__new__(RescueIQOrchestrator)
        orchestrator._approved_plans = [history]
        orchestrator.resource_agent = SimpleNamespace(
            save_approved_plan=Mock(),
        )

        orchestrator._sync_approved_plan_assignment({
            "assignmentId": "ASG_TEST",
            "status": "COMPLETED",
            "mission_status": "DELIVERED",
            "delivered_at": "2026-10-15T12:00:00+00:00",
        })

        saved = history["assignments"][0]
        self.assertEqual(saved["status"], "COMPLETED")
        self.assertEqual(saved["mission_status"], "DELIVERED")
        orchestrator.resource_agent.save_approved_plan.assert_called_once_with(history)

    def test_completed_current_plan_is_archived_before_generating_another(self):
        history = {
            "planId": "PLAN_OLD",
            "approvedAt": "2026-10-15T12:00:00+00:00",
            "assignments": [{
                "assignmentId": "ASG_OLD",
                "requestId": "REQ_OLD",
                "status": "APPROVED",
                "mission_status": "EN_ROUTE",
            }],
        }
        completed = {
            "assignmentId": "ASG_OLD",
            "requestId": "REQ_OLD",
            "status": "COMPLETED",
            "mission_status": "DELIVERED",
        }
        orchestrator = RescueIQOrchestrator.__new__(RescueIQOrchestrator)
        orchestrator.current_plan = {
            "planId": "PLAN_OLD",
            "assignments": [completed],
        }
        orchestrator._approved_plans = [history]
        orchestrator.resource_agent = SimpleNamespace(save_approved_plan=Mock())

        orchestrator._archive_completed_current_plan()

        saved_assignment = history["assignments"][0]
        self.assertEqual(saved_assignment["status"], "COMPLETED")
        self.assertEqual(saved_assignment["mission_status"], "DELIVERED")
        orchestrator.resource_agent.save_approved_plan.assert_called_once_with(history)

    def test_completed_prior_assignment_is_archived_when_current_plan_is_newer(self):
        history = {
            "planId": "PLAN_OLD",
            "assignments": [{
                "assignmentId": "ASG_OLD",
                "requestId": "REQ_OLD",
                "status": "APPROVED",
                "mission_status": "EN_ROUTE",
            }],
        }
        completed = {
            "assignmentId": "ASG_OLD",
            "requestId": "REQ_OLD",
            "status": "COMPLETED",
            "mission_status": "DELIVERED",
        }
        orchestrator = RescueIQOrchestrator.__new__(RescueIQOrchestrator)
        orchestrator.current_plan = {
            "planId": "PLAN_NEW",
            "assignments": [{"request": {"requestId": "REQ_NEW"}}],
        }
        orchestrator._active_assignments = [completed]
        orchestrator._approved_plans = [history]
        orchestrator.resource_agent = SimpleNamespace(save_approved_plan=Mock())

        orchestrator._archive_completed_current_plan()

        saved_assignment = history["assignments"][0]
        self.assertEqual(saved_assignment["status"], "COMPLETED")
        self.assertEqual(saved_assignment["mission_status"], "DELIVERED")
        orchestrator.resource_agent.save_approved_plan.assert_called_once_with(history)

    def test_approved_history_refresh_preserves_local_completed_status(self):
        persisted = {
            "planId": "PLAN_OLD",
            "assignments": [{
                "assignmentId": "ASG_OLD",
                "requestId": "REQ_OLD",
                "status": "APPROVED",
                "mission_status": "EN_ROUTE",
            }],
        }
        local = {
            "planId": "PLAN_OLD",
            "assignments": [{
                "assignmentId": "ASG_OLD",
                "requestId": "REQ_OLD",
                "status": "COMPLETED",
                "mission_status": "DELIVERED",
            }],
        }
        orchestrator = RescueIQOrchestrator.__new__(RescueIQOrchestrator)
        orchestrator.current_plan = None
        orchestrator._active_assignments = []
        orchestrator._approved_plans = [local]
        orchestrator.resource_agent = SimpleNamespace(
            get_approved_plans=Mock(return_value=[persisted]),
            save_approved_plan=Mock(),
        )

        result = orchestrator.get_approved_plans()

        saved_assignment = result["plans"][0]["assignments"][0]
        self.assertEqual(saved_assignment["status"], "COMPLETED")
        self.assertEqual(saved_assignment["mission_status"], "DELIVERED")

    def test_approved_history_refresh_reads_completed_assignment_record(self):
        persisted = {
            "planId": "PLAN_OLD",
            "assignments": [{
                "assignmentId": "ASG_OLD",
                "requestId": "REQ_OLD",
                "status": "APPROVED",
                "mission_status": "EN_ROUTE",
            }],
        }
        completed = {
            "assignmentId": "ASG_OLD",
            "planId": "PLAN_OLD",
            "requestId": "REQ_OLD",
            "status": "COMPLETED",
            "mission_status": "DELIVERED",
        }
        orchestrator = RescueIQOrchestrator.__new__(RescueIQOrchestrator)
        orchestrator.current_plan = None
        orchestrator._active_assignments = []
        orchestrator._approved_plans = []
        orchestrator.resource_agent = SimpleNamespace(
            get_approved_plans=Mock(return_value=[persisted]),
            get_completed_assignments=Mock(return_value=[completed]),
            save_approved_plan=Mock(),
        )

        result = orchestrator.get_approved_plans()

        saved_assignment = result["plans"][0]["assignments"][0]
        self.assertEqual(saved_assignment["status"], "COMPLETED")
        self.assertEqual(saved_assignment["mission_status"], "DELIVERED")

    def test_dismiss_request_removes_from_active_queue_and_plan_but_keeps_record(self):
        request = {
            "requestId": "REQ_INFEASIBLE",
            "status": "PENDING",
            "people_count": 50,
        }
        orchestrator = RescueIQOrchestrator.__new__(RescueIQOrchestrator)
        orchestrator.current_requests = [request]
        orchestrator.current_plan = {
            "unassigned": [request],
            "total_unassigned": 1,
            "metrics": {"unassigned": 1},
        }
        orchestrator._active_assignments = []
        orchestrator._approval_lock = threading.RLock()
        orchestrator.resource_agent = SimpleNamespace(save_request=Mock())
        events = []
        orchestrator._audit = events.append

        result = orchestrator.dismiss_request("REQ_INFEASIBLE")

        self.assertEqual(result["status"], "DISMISSED")
        self.assertEqual(request["status"], "DISMISSED")
        orchestrator.resource_agent.save_request.assert_called_once_with(request)
        self.assertEqual(orchestrator.get_requests(), [])
        self.assertEqual(orchestrator.current_plan["unassigned"], [])
        self.assertEqual(orchestrator.current_plan["total_unassigned"], 0)
        self.assertEqual(events[0]["type"], "request_dismissed")

    def test_gemini_quota_error_is_safely_reported_to_coordinator(self):
        agent = IntakeAgent.__new__(IntakeAgent)
        agent.model = SimpleNamespace(
            generate_content=Mock(
                side_effect=RuntimeError(
                    "429 RESOURCE_EXHAUSTED: internal provider details"
                )
            )
        )

        result = agent.process_message(
            "Help, five people are stranded near Ward 3 bridge"
        )

        self.assertEqual(result["status"], "UNPROCESSED")
        self.assertIn("temporarily rate-limited or at capacity",
                      result["processing_flags"][0])
        self.assertNotIn("RESOURCE_EXHAUSTED", result["processing_flags"][0])
        self.assertNotIn("internal provider details", result["processing_flags"][0])

    def test_active_assignment_progress_survives_new_draft_plan(self):
        assignment = {
            "assignmentId": "ASG_OLD",
            "request": {"requestId": "REQ_OLD"},
            "resource": {"resourceId": "BOAT_01"},
            "route": {"path": ["N01", "N02"]},
            "approval_status": "APPROVED",
            "status": "APPROVED",
            "mission_status": "EN_ROUTE",
            "shelter": None,
        }
        orchestrator = RescueIQOrchestrator.__new__(RescueIQOrchestrator)
        orchestrator._active_assignments = [assignment]
        orchestrator.current_plan = {
            "assignments": [{
                "request": {"requestId": "REQ_NEW"},
                "approval_status": None,
                "status": "PENDING_APPROVAL",
            }]
        }
        orchestrator._approval_lock = threading.RLock()
        orchestrator.resource_agent = SimpleNamespace(
            update_assignment=Mock(),
            update_resource=Mock(),
        )
        events = []
        orchestrator._audit = events.append

        result = orchestrator.report_progress("REQ_OLD", "picked_up")

        self.assertEqual(result["requestId"], "REQ_OLD")
        self.assertEqual(assignment["mission_status"], "AT_SCENE")

    def test_active_assignment_completion_survives_new_draft_plan(self):
        request = {"requestId": "REQ_OLD", "status": "ASSIGNED"}
        resource = {"resourceId": "BOAT_01", "currentLoad": 2}
        assignment = {
            "assignmentId": "ASG_OLD",
            "request": request,
            "resource": resource,
            "route": {"path": ["N01", "N02"]},
            "approval_status": "APPROVED",
            "status": "APPROVED",
            "mission_status": "EN_ROUTE",
            "shelter": None,
        }
        orchestrator = RescueIQOrchestrator.__new__(RescueIQOrchestrator)
        orchestrator._active_assignments = [assignment]
        orchestrator.current_plan = {
            "assignments": [{
                "request": {"requestId": "REQ_NEW"},
                "approval_status": None,
                "status": "PENDING_APPROVAL",
            }]
        }
        orchestrator.resources = [resource]
        orchestrator.resource_agent = SimpleNamespace(
            update_assignment=Mock(),
            update_request=Mock(),
            update_resource=Mock(),
        )
        orchestrator._audit = Mock()

        result = orchestrator.complete_assignment("REQ_OLD")

        self.assertTrue(result["released"])
        self.assertEqual(assignment["status"], "COMPLETED")
        self.assertEqual(request["status"], "COMPLETED")

    def test_review_promotes_uncertain_request_after_correction(self):
        request = {
            "requestId": "REQ_REVIEW",
            "status": "UNPROCESSED",
            "people_count": None,
            "node_id": None,
        }
        orchestrator = RescueIQOrchestrator.__new__(RescueIQOrchestrator)
        orchestrator.current_requests = [request]
        orchestrator._approval_lock = threading.RLock()
        orchestrator.resource_agent = SimpleNamespace(update_request=Mock())
        events = []
        orchestrator._audit = events.append

        result = orchestrator.review_request(
            "REQ_REVIEW",
            {"people_count": 3, "node_id": "N01", "urgency": "HIGH"},
        )

        self.assertEqual(result["status"], "PENDING")
        self.assertFalse(result["human_review_required"])
        self.assertEqual(result["node_id"], "N01")

    def test_reset_releases_only_undelivered_shelter_reservations(self):
        resources = [{
            "resourceId": "SHELTER_TEST",
            "type": "shelter",
            "capacity": 100,
            "totalCapacity": 100,
            "currentOccupancy": 55,
            "available": True,
            "powerAvailable": True,
        }]
        orchestrator = RescueIQOrchestrator.__new__(RescueIQOrchestrator)
        orchestrator.resources = resources
        orchestrator.resource_agent = ResourceAgent(None, resources)
        assignments = [
            {
                "approval_status": "APPROVED",
                "status": "APPROVED",
                "mission_status": "EN_ROUTE",
                "shelter": {"resourceId": "SHELTER_TEST"},
                "wave_people": 10,
            },
            {
                "approval_status": "APPROVED",
                "status": "COMPLETED",
                "mission_status": "DELIVERED",
                "shelter": {"resourceId": "SHELTER_TEST"},
                "wave_people": 5,
            },
        ]

        result = orchestrator._release_shelter_reservations(assignments)

        self.assertTrue(result["ok"])
        self.assertEqual(resources[0]["currentOccupancy"], 45)

    def test_stale_shelter_reservation_does_not_block_reset_cleanup(self):
        resources = [{
            "resourceId": "SHELTER_TEST",
            "type": "shelter",
            "capacity": 100,
            "totalCapacity": 100,
            "currentOccupancy": 0,
            "available": True,
        }]
        resource_agent = ResourceAgent(None, resources)

        result = resource_agent.release_shelter_reservations(
            {"SHELTER_TEST": 43}
        )

        self.assertTrue(result["ok"])
        self.assertEqual(resources[0]["currentOccupancy"], 0)
        self.assertEqual(
            result["warnings"],
            [
                "Shelter SHELTER_TEST has 0 occupants, but 43 seats were marked "
                "reserved; occupancy was left unchanged"
            ],
        )


if __name__ == "__main__":
    unittest.main()
