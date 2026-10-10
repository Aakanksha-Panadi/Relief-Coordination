from app.config import (
    DISPATCHABLE_TYPES,
    MOCK_MODE,
    gemini_model,
    normalize_urgency,
    request_node,
    resource_node,
)
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
                priority_order.get(normalize_urgency(r.get("urgency")), 3),
                0 if r.get("vulnerable") else 1
            )
        )

        available_resources = [
            r for r in self.resources
            if r.get("available", False) and r.get("type") in DISPATCHABLE_TYPES
        ]

        assignments = []
        used_resources = set()
        unassigned = []

        for req in sorted_requests:
            req_node = request_node(req)
            if not req_node:
                # Location never resolved. Surfacing this is the whole point:
                # defaulting to a node produced a confident plan that sent a
                # boat to the wrong ward.
                req["blockedReason"] = (
                    "Location could not be resolved to a known node. "
                    "Needs manual triage."
                )
                unassigned.append(req)
                continue

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
                        "score": score,
                        "warnings": validation["warnings"],
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
                    # Always empty by construction: invalid candidates are
                    # filtered above. Recorded explicitly so the metrics
                    # comparison reports a measured value, not a missing key.
                    "violations": [],
                    "warnings": best["warnings"],
                    "explanation": explanation,
                    "status": "PENDING_APPROVAL"
                })
                used_resources.add(best["resource"]["resourceId"])
            else:
                req["blockedReason"] = (
                    "No available resource has the capacity and a clear route "
                    "to this location."
                )
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

        # Every field uses .get(): requests reaching here may have come from a
        # seeded Firestore document, and a missing key used to raise KeyError
        # and turn the whole dispatch call into a 500.
        prompt = f"""
You are a disaster relief coordinator. Explain this assignment in 2 sentences.
Be specific about why this resource was chosen.

Resource: {resource.get('name', resource.get('resourceId', 'Unknown'))} at {resource.get('location', 'unknown location')}
Capabilities: {', '.join(resource.get('skills', [])) or 'general'}
Request: {request.get('people_count', 0)} people at {request.get('location_description', 'unknown location')}
Needs: {', '.join(str(n) for n in request.get('needs', [])) or 'unspecified'}
Urgency: {request.get('urgency', 'HIGH')}
Vulnerable: {request.get('vulnerable_details') or 'None'}
Route: {' → '.join(route.get('path_names', []))}
ETA: {route.get('minutes', '?')} minutes

Return only the explanation, no JSON.
"""
        try:
            response = self.model.generate_content(prompt)
            return (response.text or "").strip() or self._fallback_explanation(
                resource, request, route
            )
        except Exception as e:
            # An explanation is narration, not a decision. Losing it must not
            # take the dispatch plan down with it.
            print(f"Explanation generation failed: {e}")
            return self._fallback_explanation(resource, request, route)

    def _fallback_explanation(
        self, resource: dict, request: dict, route: dict
    ) -> str:
        res_name = resource.get("name", resource.get("resourceId", "Resource"))
        path_names = route.get("path_names", [])
        route_str = " → ".join(path_names) if path_names else "direct route"
        return (
            f"{res_name} assigned. ETA {route.get('minutes', '?')} minutes "
            f"via {route_str}. Selected on urgency, capacity and travel time."
        )

    def _explain_tradeoff(self, assignments: list) -> str:
        if len(assignments) < 2:
            return ""
        
        first = assignments[0]
        second = assignments[1] if len(assignments) > 1 else None
        
        if second and normalize_urgency(first["request"].get("urgency")) == "CRITICAL":
            delay = second["route"].get("minutes", 0) - first["route"].get("minutes", 0)
            if delay > 0:
                return (
                    f"Serving {first['request'].get('location_description', 'the first location')} first "
                    f"delays {second['request'].get('location_description', 'the second location')} by ~{delay} minutes. "
                    f"Recommended because first group is CRITICAL "
                    f"{'with vulnerable members' if first['request'].get('vulnerable') else ''}."
                )
        return "All assignments optimized for minimum response time with no critical conflicts."