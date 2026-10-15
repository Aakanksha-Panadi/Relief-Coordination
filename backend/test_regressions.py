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


if __name__ == "__main__":
    unittest.main()
