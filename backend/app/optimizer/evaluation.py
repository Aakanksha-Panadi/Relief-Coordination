"""Evaluation harness: RescueIQ's plan vs a first-come-first-served baseline.

The baseline models what an overloaded control room actually does — take
requests in the order they arrived and hand each one the first resource
someone can reach on the phone. Every number reported here is computed from
real plans over the real road graph; nothing is hardcoded.
"""
from app.config import resource_node, request_node
from app.optimizer.constraints import ConstraintEngine

DISPATCHABLE = ("boat", "volunteer_team")


class EvaluationHarness:
    def __init__(self, router, resources: list):
        self.router = router
        self.resources = resources
        self.constraints = ConstraintEngine()

    # ---------- baseline ----------

    def baseline_plan(self, requests: list) -> dict:
        """First-come-first-served. No urgency sort, no distance scoring."""
        available = [
            r for r in self.resources
            if r.get("available", False) and r.get("type") in DISPATCHABLE
        ]
        assignments = []
        unassigned = []
        used = set()

        for req in requests:  # arrival order, deliberately unsorted
            req_node = request_node(req)
            picked = None
            for resource in available:
                if resource["resourceId"] in used:
                    continue
                route = self.router.shortest_path(
                    resource_node(resource), req_node
                )
                if route.get("blocked"):
                    continue
                picked = (resource, route)
                break  # first one that can get there at all

            if not picked:
                unassigned.append(req)
                continue

            resource, route = picked
            validation = self.constraints.validate_assignment(resource, req)
            assignments.append({
                "request": req,
                "resource": resource,
                "route": route,
                "violations": validation["violations"],
                "status": "BASELINE",
            })
            used.add(resource["resourceId"])

        return {
            "assignments": assignments,
            "unassigned": unassigned,
            "total_assigned": len(assignments),
            "total_unassigned": len(unassigned),
        }

    # ---------- scoring ----------

    def score_plan(self, plan: dict, total_requests: int) -> dict:
        assignments = plan.get("assignments", [])
        times = [
            a["route"]["minutes"] for a in assignments
            if a.get("route") and a["route"].get("minutes", -1) >= 0
        ]
        violations = sum(len(a.get("violations", [])) for a in assignments)

        avg_time = round(sum(times) / len(times), 1) if times else 0
        coverage = (
            round(len(assignments) / total_requests * 100)
            if total_requests else 0
        )

        return {
            "avg_response_time_min": avg_time,
            "max_response_time_min": max(times) if times else 0,
            "coverage_pct": coverage,
            "critical_first_pct": self._critical_first_pct(assignments),
            "constraint_violations": violations,
            "assigned": len(assignments),
            "unassigned": plan.get("total_unassigned", 0),
        }

    def _critical_first_pct(self, assignments: list) -> int:
        """% of CRITICAL requests dispatched before any non-critical one.

        This is the metric that separates triage from queueing: a plan that
        serves a life-critical case after three routine ones scores low even
        if it eventually serves everybody.
        """
        criticals = [
            i for i, a in enumerate(assignments)
            if a["request"].get("urgency") == "CRITICAL"
        ]
        if not criticals:
            return 100

        first_non_critical = next(
            (
                i for i, a in enumerate(assignments)
                if a["request"].get("urgency") != "CRITICAL"
            ),
            len(assignments),
        )
        served_first = sum(1 for i in criticals if i < first_non_critical)
        return round(served_first / len(criticals) * 100)

    # ---------- comparison ----------

    def compare(self, requests: list, rescueiq_plan: dict) -> dict:
        total = len(requests)
        baseline = self.score_plan(self.baseline_plan(requests), total)
        rescueiq = self.score_plan(rescueiq_plan, total)

        def delta_pct(before, after, lower_is_better=True):
            if before == 0:
                return 0
            change = (before - after) / before * 100
            return round(change if lower_is_better else -change)

        return {
            "total_requests": total,
            "baseline": baseline,
            "rescueiq": rescueiq,
            "improvement": {
                "response_time_pct_faster": delta_pct(
                    baseline["avg_response_time_min"],
                    rescueiq["avg_response_time_min"],
                ),
                "coverage_pct_points": (
                    rescueiq["coverage_pct"] - baseline["coverage_pct"]
                ),
                "critical_first_pct_points": (
                    rescueiq["critical_first_pct"]
                    - baseline["critical_first_pct"]
                ),
                "violations_avoided": (
                    baseline["constraint_violations"]
                    - rescueiq["constraint_violations"]
                ),
            },
        }
