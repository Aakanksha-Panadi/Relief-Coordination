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

        # Hard rule — capacity
        current = resource.get("currentLoad", 0)
        capacity = resource.get("capacity", 0)
        people = request.get("people_count", 0)
        if current + people > capacity:
            violations.append({
                "rule": "R001",
                "message": f"Capacity exceeded: {current + people} > {capacity}"
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
        score += urgency_bonus.get(request.get("urgency", "LOW"), 0)

        # Vulnerable bonus
        if request.get("vulnerable", False):
            score += 20

        # Distance penalty
        minutes = route.get("minutes", 99)
        score -= minutes * 2

        # Availability bonus
        if resource.get("available", False):
            score += 10

        return round(score, 2)