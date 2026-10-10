from app.config import (
    DISPATCHABLE_TYPES,
    MOCK_MODE,
    gemini_model,
    normalize_urgency,
    resource_capacity,
    resource_load,
    resource_node,
    request_node,
)
from app.optimizer.router import DistrictRouter
from app.optimizer.constraints import ConstraintEngine


class DispatchAgent:
    def __init__(self, router: DistrictRouter, resources: list):
        self.router = router
        self.resources = resources
        self.constraints = ConstraintEngine()
        self.model = None if MOCK_MODE else gemini_model()

    def create_plan(self, requests: list, exclude_resource_ids: set | None = None) -> dict:
        # Sort by urgency and vulnerability
        priority_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
        def minimum_eta(request):
            node = request_node(request)
            etas = [self.router.shortest_path(resource_node(r), node).get("minutes", 10**9)
                    for r in self.resources if r.get("available", False)
                    and r.get("type") in DISPATCHABLE_TYPES and node
                    and str(r.get("fuelLevel", "")).lower() != "low"
                    and r.get("operatorReachable", True) is not False
                    and r.get("status", "").upper() not in ("UNAVAILABLE", "BROKEN", "OFFLINE")]
            return min(etas, default=10**9)
        sorted_requests = sorted(
            requests,
            key=lambda r: (
                priority_order.get(normalize_urgency(r.get("urgency")), 3),
                0 if r.get("vulnerable") else 1,
                -(r.get("people_count") or 0),
                minimum_eta(r),
                r.get("requestId", ""),
            )
        )

        available_resources = [
            r for r in self.resources
            if r.get("available", False) and r.get("type") in DISPATCHABLE_TYPES
            and str(r.get("fuelLevel", "")).lower() != "low"
            and r.get("operatorReachable", True) is not False
            and r.get("status", "").upper() not in ("UNAVAILABLE", "BROKEN", "OFFLINE")
            and r.get("resourceId") not in (exclude_resource_ids or set())
        ]

        assignments = []
        used_resources = set()
        coverage_alerts = []
        shelter_occupancy = {r.get("resourceId"): resource_load(r) for r in self.resources if r.get("type") == "shelter"}
        unassigned = []

        for req in sorted_requests:
            req_node = request_node(req)
            if "multiple_requests_possible" in (req.get("processing_flags") or []):
                req["blockedReason"] = "Several households may be included; coordinator must split or confirm this request before dispatch."
                unassigned.append(req)
                continue
            if req.get("people_count") is None:
                req["blockedReason"] = "People count is missing. Coordinator review required before dispatch."
                unassigned.append(req)
                continue
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
                if "medical" in (req.get("needs") or []) and resource.get("type") != "medical_unit":
                    skills = {str(skill).lower() for skill in resource.get("skills", [])}
                    if not skills.intersection({"first_aid", "medical_support", "medical"}):
                        continue

                res_node = resource_node(resource)
                route = self.router.shortest_path(res_node, req_node)

                if route.get("blocked"):
                    continue

                score = self.constraints.score_assignment(resource, req, route)

                route_minutes = route.get("minutes", 0)
                best_minutes = best["route"].get("minutes", 0) if best else float("inf")
                resource_id = str(resource.get("resourceId", ""))
                best_resource_id = str(best["resource"].get("resourceId", "")) if best else ""
                if (best is None or score > best_score or
                        (score == best_score and (route_minutes < best_minutes or
                         (route_minutes == best_minutes and resource_id < best_resource_id)))):
                    best_score = score
                    best = {
                        "resource": resource,
                        "route": route,
                        "score": score,
                        "warnings": validation["warnings"],
                    }

            if best:
                shelter = None
                if "evacuation" in (req.get("needs") or []) or "shelter" in (req.get("needs") or []):
                    shelters = [s for s in self.resources if s.get("type") == "shelter"
                                and s.get("available", True)
                                and s.get("powerAvailable", s.get("power_available", True)) is not False
                                and resource_capacity(s) - shelter_occupancy.get(s.get("resourceId"), resource_load(s)) >= req["people_count"]]
                    shelter_routes = [(self.router.shortest_path(req_node, resource_node(s)), s)
                                      for s in shelters]
                    shelter_routes = [(route, s) for route, s in shelter_routes if not route.get("blocked")]
                    if not shelter_routes:
                        req["blockedReason"] = "No reachable shelter has enough capacity and power; hold and alert coordinator."
                        req["partial"] = True
                        coverage_alerts.append(f"{req.get('requestId')}: no reachable shelter with capacity and power; hold and alert.")
                        unassigned.append(req)
                        continue
                    shelter_route, shelter = min(shelter_routes, key=lambda pair: (pair[0].get("minutes", 10**9), pair[1].get("resourceId", "")))
                    # Keep occupancy in this generated plan so two proposed
                    # rescues cannot both consume the same final shelter beds.
                    planned_occupancy = shelter_occupancy.get(shelter.get("resourceId"), resource_load(shelter)) + req["people_count"]
                    if planned_occupancy > resource_capacity(shelter):
                        req["blockedReason"] = "No reachable shelter has enough remaining capacity; hold and alert coordinator."
                        req["partial"] = True
                        unassigned.append(req)
                        continue
                    shelter_occupancy[shelter.get("resourceId")] = planned_occupancy
                explanation = self._explain_assignment(
                    best["resource"], req, best["route"]
                )
                assignments.append({
                    "request": req,
                    "resource": best["resource"],
                    "shelter": shelter,
                    "shelter_route": shelter_route if shelter else None,
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
                assignment = assignments[-1]
                if "medical" in (req.get("needs") or []) and best["resource"].get("type") != "medical_unit":
                    assignment["status"] = "PARTIAL"
                    assignment["warnings"].append({"message": "No medical unit free; first-aid team assigned as fallback."})
                    coverage_alerts.append(f"{req.get('requestId')}: first-aid fallback; medical coverage is partial.")
                if "food" in (req.get("needs") or []):
                    stock = best["resource"].get("foodStock", best["resource"].get("food_stock"))
                    if stock is None or stock < req["people_count"]:
                        assignment["status"] = "PARTIAL"
                        assignment["warnings"].append({"message": "Food stock is insufficient or unreported; partial coverage."})
                        coverage_alerts.append(f"{req.get('requestId')}: food stock insufficient or unreported; alert coordinator.")
                used_resources.add(best["resource"]["resourceId"])
            else:
                reachable_any = any(
                    not self.router.shortest_path(resource_node(r), req_node).get("blocked")
                    for r in available_resources
                )
                # An oversized evacuation can still be served in capacity
                # limited waves. Each assigned boat gets an explicit wave,
                # load, and cumulative ETA; approval remains coordinator-only.
                boats = []
                if req.get("needs") and "evacuation" in req["needs"]:
                    for resource in available_resources:
                        if resource.get("resourceId") in used_resources:
                            continue
                        if resource.get("type") != "boat":
                            continue
                        capacity = resource_capacity(resource) - resource_load(resource)
                        route = self.router.shortest_path(resource_node(resource), req_node)
                        if capacity > 0 and not route.get("blocked"):
                            boats.append((route.get("minutes", 0), resource.get("resourceId", ""), resource, route, capacity))
                if boats and req["people_count"] > max(item[4] for item in boats):
                    boats.sort(key=lambda item: (item[0], item[1]))
                    shelter = None
                    shelter_routes = []
                    if "shelter" in req["needs"] or "evacuation" in req["needs"]:
                        shelter_routes = [(self.router.shortest_path(req_node, resource_node(s)), s)
                                          for s in self.resources if s.get("type") == "shelter"
                                          and s.get("available", True)
                                          and s.get("powerAvailable", s.get("power_available", True)) is not False
                                          and resource_capacity(s) - shelter_occupancy.get(s.get("resourceId"), resource_load(s)) >= req["people_count"]]
                        shelter_routes = [(rt, sh) for rt, sh in shelter_routes if not rt.get("blocked")]
                        if not shelter_routes:
                            req["blockedReason"] = "No reachable shelter has capacity for the group; hold and alert coordinator."
                            req["partial"] = True
                            coverage_alerts.append(f"{req.get('requestId')}: no reachable shelter for all waves; hold and alert.")
                            unassigned.append(req)
                            continue
                        _shelter_route, shelter = min(shelter_routes, key=lambda pair: (pair[0].get("minutes", 10**9), pair[1].get("resourceId", "")))
                        shelter_occupancy[shelter["resourceId"]] = shelter_occupancy.get(shelter["resourceId"], resource_load(shelter)) + req["people_count"]
                    remaining = req["people_count"]
                    trip_by_resource = {}
                    wave = 0
                    while remaining > 0:
                        wave += 1
                        for eta, rid, resource, route, capacity in boats:
                            if remaining <= 0:
                                break
                            people = min(capacity, remaining)
                            trip_by_resource[rid] = trip_by_resource.get(rid, 0) + 1
                            trip = trip_by_resource[rid]
                            round_trip = 2 * eta
                            wave_route = dict(route)
                            wave_route["minutes"] = eta + (trip - 1) * round_trip
                            assignments.append({"request": req, "resource": resource, "route": wave_route,
                                                "shelter": shelter, "wave": wave, "wave_count": None,
                                                "wave_people": people, "status": "PENDING_APPROVAL",
                                                "score": 0, "violations": [], "warnings": [],
                                                "explanation": f"Wave {wave}: {people} people on {resource.get('name', rid)}; cumulative ETA {wave_route['minutes']} minutes."})
                            remaining -= people
                            used_resources.add(rid)
                    for assignment in assignments:
                        if assignment.get("request") is req and assignment.get("wave"):
                            assignment["wave_count"] = wave
                    continue
                if available_resources and not reachable_any:
                    req["blockedReason"] = "Unreachable with current resources; escalation required."
                    req["dispatch_status"] = "UNREACHABLE"
                    req["escalation_required"] = True
                    req["status"] = "UNREACHABLE"
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
            "total_unassigned": len(unassigned),
            "alerts": coverage_alerts,
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
        model = self.model
        if model is None:
            return self._fallback_explanation(resource, request, route)

        try:
            response = model.generate_content(prompt)
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
        if any(normalize_urgency(a["request"].get("urgency")) == "CRITICAL" for a in assignments):
            return "Critical requests are prioritized by vulnerability, then larger group size, then ETA; request ID breaks remaining ties. Coordinator approval is required."
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
