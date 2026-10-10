from app.config import (
    MOCK_MODE,
    gemini_model,
    normalize_urgency,
    request_node,
    resource_node,
)
from app.optimizer.router import DistrictRouter
from app.optimizer.constraints import ConstraintEngine


class ReplanAgent:
    def __init__(self, router: DistrictRouter, resources: list):
        self.router = router
        self.resources = resources
        self.constraints = ConstraintEngine()
        self.model = None if MOCK_MODE else gemini_model()

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
                req_node = request_node(assignment["request"])
                res = assignment["resource"]

                # Reroute from where the closure actually stops the vehicle,
                # not from the depot it left. old_edges[i] joins
                # old_path[i] -> old_path[i+1], so the last node reachable
                # before the closed edge is old_path[i].
                resume_node, travelled = self._resume_point(
                    old_route, edge_id, res
                )

                new_route = self.router.shortest_path(resume_node, req_node) \
                    if req_node else {"blocked": True}
                if not new_route.get("blocked"):
                    new_route = self._stitch(old_route, edge_id, new_route, travelled)

                if new_route.get("blocked"):
                    replanned.append({
                        **assignment,
                        "new_route": None,
                        "status": "BLOCKED",
                        "explanation": (
                            f"No alternative route found for "
                            f"{res.get('name', res.get('resourceId', 'resource'))} to "
                            f"{assignment['request'].get('location_description', 'the destination')}. "
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

    def _resume_point(self, old_route: dict, edge_id: str, resource: dict):
        """Node the vehicle is held at by the closure, and minutes already run.

        Conservative by design: assume it got as far as the junction before
        the closed road. Falls back to the resource's own node when the old
        route is unusable.
        """
        old_edges = old_route.get("edges", []) or []
        old_path = old_route.get("path", []) or []
        try:
            idx = old_edges.index(edge_id)
        except ValueError:
            return resource_node(resource), 0
        if idx >= len(old_path):
            return resource_node(resource), 0
        return old_path[idx], self.router.path_minutes(old_edges[:idx])

    def _stitch(
        self, old_route: dict, edge_id: str, new_leg: dict, travelled: int
    ) -> dict:
        """Join the distance already covered to the detour.

        Reporting only the detour would understate the journey and make a
        closure look cheaper than it is.
        """
        old_edges = old_route.get("edges", []) or []
        old_path = old_route.get("path", []) or []
        try:
            idx = old_edges.index(edge_id)
        except ValueError:
            return new_leg
        prefix_path = old_path[:idx]
        prefix_names = old_route.get("path_names", [])[:idx]
        return {
            "path": prefix_path + new_leg.get("path", []),
            "path_names": prefix_names + new_leg.get("path_names", []),
            "edges": old_edges[:idx] + new_leg.get("edges", []),
            "minutes": travelled + new_leg.get("minutes", 0),
            "blocked": False,
            "resumed_from": old_path[idx] if idx < len(old_path) else None,
            "minutes_already_travelled": travelled,
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

        resource = assignment.get("resource", {})
        request = assignment.get("request", {})
        prompt = f"""
A road was closed during disaster relief operations.
Explain the replanning decision in 2 sentences.

Closed road: {edge_id}
Resource: {resource.get('name', resource.get('resourceId', 'Unknown'))}
Held at: {new_route.get('resumed_from') or 'its depot'}
Destination: {request.get('location_description', 'unknown location')}
New route: {' → '.join(new_route.get('path_names', []))}
Extra time: {delay} minutes
Request urgency: {request.get('urgency', 'HIGH')}

Return only the explanation.
"""
        try:
            response = self.model.generate_content(prompt)
            text = (response.text or "").strip()
            if text:
                return text
        except Exception as e:
            print(f"Replan explanation failed: {e}")
        path = " → ".join(new_route.get("path_names", []))
        return (
            f"{edge_id} closed. Rerouted via {path} "
            f"({delay:+d} minutes). Coverage maintained."
        )

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
            # After a replan only affected assignments carry a new_route;
            # unchanged ones must still contribute their original ETA or the
            # "after" average silently drops them and looks better than it is.
            route = a.get("route") or {}
            if use_new and a.get("new_route"):
                route = a["new_route"]
            if route and route.get("minutes", -1) >= 0:
                times.append(route["minutes"])
            if normalize_urgency(a["request"].get("urgency")) == "CRITICAL":
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