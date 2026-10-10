from app.config import MOCK_MODE, gemini_model, resource_node, request_node
from app.optimizer.router import DistrictRouter
from app.optimizer.constraints import ConstraintEngine


class DispatchAgent:
    def __init__(self, router: DistrictRouter, resources: list):
        self.router = router
        self.resources = resources
        self.constraints = ConstraintEngine()
        self.model = None if MOCK_MODE else gemini_model()

    def create_plan(self, requests: list) -> dict:
        # Sort by urgency and vulnerability
        priority_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
        sorted_requests = sorted(
            requests,
            key=lambda r: (
                priority_order.get(r.get("urgency", "LOW"), 3),
                0 if r.get("vulnerable") else 1
            )
        )

        available_resources = [
            r for r in self.resources
            if r.get("available", False) and r.get("type") in ["boat", "volunteer_team"]
        ]

        assignments = []
        used_resources = set()
        unassigned = []

        for req in sorted_requests:
            req_node = request_node(req)
            best = None
            best_score = -999

            for resource in available_resources:
                if resource["resourceId"] in used_resources:
                    continue

                validation = self.constraints.validate_assignment(resource, req)
                if not validation["valid"]:
                    continue

                res_node = resource_node(resource)
                route = self.router.shortest_path(res_node, req_node)

                if route.get("blocked"):
                    continue

                score = self.constraints.score_assignment(resource, req, route)

                if score > best_score:
                    best_score = score
                    best = {
                        "resource": resource,
                        "route": route,
                        "score": score
                    }

            if best:
                explanation = self._explain_assignment(
                    best["resource"], req, best["route"]
                )
                assignments.append({
                    "request": req,
                    "resource": best["resource"],
                    "route": best["route"],
                    "score": best["score"],
                    "explanation": explanation,
                    "status": "PENDING_APPROVAL"
                })
                used_resources.add(best["resource"]["resourceId"])
            else:
                unassigned.append(req)

        tradeoff = self._explain_tradeoff(assignments)

        return {
            "assignments": assignments,
            "unassigned": unassigned,
            "tradeoff": tradeoff,
            "total_assigned": len(assignments),
            "total_unassigned": len(unassigned)
        }

    def _explain_assignment(self, resource: dict, request: dict, route: dict) -> str:
        if MOCK_MODE:
            urgency = request.get("urgency", "HIGH")
            vulnerable = request.get("vulnerable", False)
            res_name = resource.get("name", resource["resourceId"])
            minutes = route.get("minutes", "?")
            path_names = route.get("path_names", [])
            route_str = " → ".join(path_names) if path_names else "Direct route"

            reason = f"{res_name} assigned. ETA {minutes} minutes via {route_str}."
            if urgency == "CRITICAL":
                reason += " Life-critical priority."
            if vulnerable:
                reason += f" Vulnerable group: {request.get('vulnerable_details', '')}."
            reason += " No constraint violations."
            return reason

        prompt = f"""
You are a disaster relief coordinator. Explain this assignment in 2 sentences.
Be specific about why this resource was chosen.

Resource: {resource['name']} at {resource['location']}
Request: {request['people_count']} people at {request['location_description']}
Urgency: {request['urgency']}
Vulnerable: {request.get('vulnerable_details', 'None')}
Route: {' → '.join(route.get('path_names', []))}
ETA: {route['minutes']} minutes

Return only the explanation, no JSON.
"""
        response = self.model.generate_content(prompt)
        return response.text.strip()

    def _explain_tradeoff(self, assignments: list) -> str:
        if len(assignments) < 2:
            return ""
        
        first = assignments[0]
        second = assignments[1] if len(assignments) > 1 else None
        
        if second and first["request"].get("urgency") == "CRITICAL":
            delay = second["route"].get("minutes", 0) - first["route"].get("minutes", 0)
            if delay > 0:
                return (
                    f"Serving {first['request']['location_description']} first "
                    f"delays {second['request']['location_description']} by ~{delay} minutes. "
                    f"Recommended because first group is CRITICAL "
                    f"{'with vulnerable members' if first['request'].get('vulnerable') else ''}."
                )
        return "All assignments optimized for minimum response time with no critical conflicts."