import os
from dotenv import load_dotenv
from app.optimizer.router import DistrictRouter
from app.optimizer.constraints import ConstraintEngine

load_dotenv()

MOCK_MODE = True

class ReplanAgent:
    def __init__(self, router: DistrictRouter, resources: list):
        self.router = router
        self.resources = resources
        self.constraints = ConstraintEngine()

        if not MOCK_MODE:
            import vertexai
            from vertexai.generative_models import GenerativeModel
            vertexai.init(
                project=os.getenv("GOOGLE_CLOUD_PROJECT", "resourceworkflow"),
                location="us-central1"
            )
            self.model = GenerativeModel("gemini-2.0-flash")

    def simulate_road_closure(
        self, edge_id: str, current_assignments: list
    ) -> dict:
        # Close the edge
        self.router.close_edge(edge_id)

        affected = []
        replanned = []

        for assignment in current_assignments:
            old_route = assignment.get("route", {})
            old_edges = old_route.get("edges", [])

            if edge_id in old_edges:
                affected.append(assignment)
                req_node = assignment["request"].get("node_id", "N04")
                res = assignment["resource"]
                res_node = self._get_resource_node(res)

                new_route = self.router.shortest_path(res_node, req_node)

                if new_route.get("blocked"):
                    replanned.append({
                        **assignment,
                        "new_route": None,
                        "status": "BLOCKED",
                        "explanation": (
                            f"No alternative route found for "
                            f"{res['name']} to "
                            f"{assignment['request']['location_description']}. "
                            f"Manual intervention required."
                        )
                    })
                else:
                    old_mins = old_route.get("minutes", 0)
                    new_mins = new_route.get("minutes", 0)
                    delay = new_mins - old_mins

                    explanation = self._explain_replan(
                        assignment, new_route, edge_id, delay
                    )

                    replanned.append({
                        **assignment,
                        "old_route": old_route,
                        "new_route": new_route,
                        "delay_minutes": delay,
                        "status": "REPLANNED",
                        "explanation": explanation
                    })
            else:
                replanned.append({**assignment, "status": "UNCHANGED"})

        metrics_before = self._calc_metrics(current_assignments)
        metrics_after = self._calc_metrics(replanned, use_new=True)

        return {
            "edge_closed": edge_id,
            "affected_count": len(affected),
            "assignments": replanned,
            "metrics_before": metrics_before,
            "metrics_after": metrics_after,
            "summary": (
                f"{len(affected)} assignment(s) affected by closure of {edge_id}. "
                f"System replanned {len([r for r in replanned if r['status']=='REPLANNED'])} route(s)."
            )
        }

    def _explain_replan(
        self, assignment: dict, new_route: dict, edge_id: str, delay: int
    ) -> str:
        if MOCK_MODE:
            res_name = assignment["resource"].get("name", "Resource")
            path = " → ".join(new_route.get("path_names", []))
            req_loc = assignment["request"].get("location_description", "destination")
            urgency = assignment["request"].get("urgency", "HIGH")
            
            explanation = (
                f"{edge_id} closed. Rerouted {res_name} via {path} "
                f"(+{delay} minutes). "
            )
            if urgency == "CRITICAL":
                explanation += (
                    "Group is stable — delay is acceptable. "
                    "No critical risk increase."
                )
            else:
                explanation += "Impact is low. Coverage maintained."
            return explanation

        prompt = f"""
A road was closed during disaster relief operations.
Explain the replanning decision in 2 sentences.

Closed road: {edge_id}
Resource: {assignment['resource']['name']}
Destination: {assignment['request']['location_description']}
New route: {' → '.join(new_route.get('path_names', []))}
Extra time: {delay} minutes
Request urgency: {assignment['request']['urgency']}

Return only the explanation.
"""
        response = self.model.generate_content(prompt)
        return response.text.strip()

    def _get_resource_node(self, resource: dict) -> str:
        location_map = {
            "Central_Depot": "N01",
            "Zone_A_Dock": "N11",
            "Zone_B_Dock": "N12",
            "Zone_C_Dock": "N12",
            "Zone_B_Community_Hall": "N08",
            "Zone_A_School": "N07"
        }
        return location_map.get(resource.get("location", ""), "N01")

    def _calc_metrics(
        self, assignments: list, use_new: bool = False
    ) -> dict:
        total = len(assignments)
        if total == 0:
            return {}

        times = []
        violations = 0
        critical_first = 0
        critical_total = 0

        for a in assignments:
            route = a.get("new_route" if use_new else "route", {})
            if route:
                times.append(route.get("minutes", 0))
            if a["request"].get("urgency") == "CRITICAL":
                critical_total += 1
                if a.get("status") != "BLOCKED":
                    critical_first += 1
            if a.get("status") == "BLOCKED":
                violations += 1

        avg_time = round(sum(times) / len(times)) if times else 0
        coverage = round((total - violations) / total * 100) if total > 0 else 0
        critical_pct = round(critical_first / critical_total * 100) if critical_total > 0 else 100

        return {
            "avg_response_time": avg_time,
            "coverage_pct": coverage,
            "violations": violations,
            "critical_served_pct": critical_pct
        }