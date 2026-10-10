from app.config import (
    meets_needs,
    normalize_urgency,
    resource_capacity,
    resource_load,
)


class ConstraintEngine:
    def __init__(self, rules: list = None):
        self.rules = rules or self._default_rules()

    def _default_rules(self) -> list:
        return [
            {
                "id": "R001",
                "type": "hard",
                "name": "No overload",
                "description": "Resource cannot exceed capacity"
            },
            {
                "id": "R002",
                "type": "hard",
                "name": "Availability check",
                "description": "Resource must be available"
            },
            {
                "id": "R003",
                "type": "soft",
                "name": "Critical first",
                "description": "CRITICAL requests get priority"
            },
            {
                "id": "R004",
                "type": "soft",
                "name": "Vulnerable priority",
                "description": "Vulnerable groups get priority"
            }
        ]

    def validate_assignment(self, resource: dict, request: dict) -> dict:
        violations = []
        warnings = []

        # Hard rule — availability
        if not resource.get("available", False):
            violations.append({
                "rule": "R002",
                "message": f"{resource['resourceId']} is not available"
            })

        # Hard rule — capacity. Read through the alias helpers: shelters are
        # seeded with totalCapacity/currentOccupancy and would otherwise
        # present as capacity 0 and fail every check.
        current = resource_load(resource)
        capacity = resource_capacity(resource)
        people = request.get("people_count")
        if not isinstance(people, int) or people <= 0:
            violations.append({"rule": "R006", "message": "People count is unknown; coordinator review required"})
            people = 0
        if current + people > capacity:
            violations.append({
                "rule": "R001",
                "message": f"Capacity exceeded: {current + people} > {capacity}"
            })

        # Soft rule — capability. Not disqualifying: a team without medical
        # training still beats nobody, but the score should prefer one that
        # has it.
        needs = request.get("needs", [])
        if needs and meets_needs(resource, needs) == 0:
            warnings.append({
                "rule": "R005",
                "message": (
                    f"{resource.get('resourceId')} is not equipped for "
                    f"{', '.join(str(n) for n in needs)}"
                )
            })

        return {
            "valid": len(violations) == 0,
            "violations": violations,
            "warnings": warnings
        }

    def score_assignment(self, resource: dict, request: dict, route: dict) -> float:
        score = 100.0

        # Urgency bonus
        urgency_bonus = {
            "CRITICAL": 40,
            "HIGH": 25,
            "MEDIUM": 10,
            "LOW": 0
        }
        # Normalized so "Critical" cannot miss the lookup and score as LOW.
        score += urgency_bonus.get(normalize_urgency(request.get("urgency")), 0)

        # Vulnerable bonus
        if request.get("vulnerable", False):
            score += 20

        # Capability bonus — 15 per need this resource is equipped for, so a
        # medically-trained team outranks a marginally closer one that is not.
        score += 15 * meets_needs(resource, request.get("needs", []))

        # Distance penalty
        minutes = route.get("minutes", 99)
        score -= minutes * 2

        # Availability bonus
        if resource.get("available", False):
            score += 10

        return round(score, 2)
