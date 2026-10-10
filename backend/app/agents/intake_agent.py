import json
import re

from app.config import (
    MOCK_MODE,
    NODE_HINT,
    NODE_IDS,
    gemini_model,
    resolve_node_from_text,
)


class IntakeAgent:
    def __init__(self):
        self.model = None if MOCK_MODE else gemini_model()

    def process_message(self, raw_message: str) -> dict:
        if MOCK_MODE:
            return self._mock_extract(raw_message)
        return self._gemini_extract(raw_message)

    def _gemini_extract(self, raw_message: str) -> dict:
        prompt = f"""
You are a disaster relief intake agent processing emergency help requests.
Extract information and return ONLY valid JSON, no explanation, no markdown.

These are the only locations in the district. Pick the node id whose name best
matches where the caller is. Match on ward numbers and landmarks:
{NODE_HINT}

Message: "{raw_message}"

Return exactly this JSON:
{{
  "language": "detected language name",
  "translated_text": "english translation",
  "location_description": "location mentioned or unknown",
  "node_id": "closest matching node id from the list above, e.g. N05",
  "people_count": 0,
  "urgency": "CRITICAL or HIGH or MEDIUM or LOW",
  "vulnerable": true or false,
  "vulnerable_details": "elderly/children/medical details or empty string",
  "needs": ["evacuation", "medical", "food", "shelter"],
  "confidence": 0.95
}}

Rules:
- CRITICAL = life threatening, elderly/children, rising water, medical emergency
- HIGH = stuck, need evacuation soon, children present
- MEDIUM = need help but stable
- vulnerable = true if elderly, children, disabled, or medical emergency
"""
        response = self.model.generate_content(prompt)
        text = response.text.strip()
        text = re.sub(r'```json|```', '', text).strip()
        result = json.loads(text)

        # Gemini still returns "unknown" when a message is vague; resolve from
        # the description so the request does not silently route to a default.
        if result.get("node_id") not in NODE_IDS:
            result["node_id"] = resolve_node_from_text(
                result.get("location_description", "")
            ) or resolve_node_from_text(result.get("translated_text", "")) or "unknown"
        return result

    def _mock_extract(self, raw_message: str) -> dict:
        msg = raw_message.lower()
        
        # Detect language
        if any(c in raw_message for c in 'अआइईउऊएऐओऔकखगघचछजझटठडढणतथदधनपफबभमयरलवशषसह'):
            language = "Hindi"
            translated = "Water has filled my house, 4 people, elderly mother cannot walk"
            location = "Ward 7, near temple"
            node_id = "N05"
            urgency = "CRITICAL"
            vulnerable = True
            vulnerable_details = "elderly woman, cannot walk"
            people_count = 4
            needs = ["evacuation", "medical"]
        elif any(c in raw_message for c in 'அஆஇஈஉஊகசடதநபமயரலவ'):
            language = "Tamil"
            translated = "Need help, we 5 people are trapped, children are present"
            location = "Ward 3, Temple Street"
            node_id = "N03"
            urgency = "HIGH"
            vulnerable = True
            vulnerable_details = "children present"
            people_count = 5
            needs = ["evacuation"]
        elif any(word in msg for word in ['paani', 'bahut', 'jaldi', 'zyada', 'madad', 'bachao']):
            language = "Mixed Hindi-English"
            translated = "HELP water is very high 3 people please boat quickly"
            location = "Ward 7"
            node_id = "N05"
            urgency = "HIGH"
            vulnerable = False
            vulnerable_details = ""
            people_count = 3
            needs = ["evacuation"]
        elif 'diabetic' in msg or 'insulin' in msg or 'medical' in msg:
            language = "English"
            translated = raw_message
            location = "Ward 9, Hospital Junction"
            node_id = "N06"
            urgency = "CRITICAL"
            vulnerable = True
            vulnerable_details = "diabetic patient, needs insulin"
            people_count = 1
            needs = ["medical", "evacuation"]
        else:
            language = "English"
            translated = raw_message
            location = "Market Road area"
            node_id = "N04"
            urgency = "HIGH"
            vulnerable = "children" in msg or "child" in msg
            vulnerable_details = "children present" if "children" in msg else ""
            people_count = 6
            needs = ["evacuation"]

        return {
            "language": language,
            "translated_text": translated,
            "location_description": location,
            "node_id": node_id,
            "people_count": people_count,
            "urgency": urgency,
            "vulnerable": vulnerable,
            "vulnerable_details": vulnerable_details,
            "needs": needs,
            "confidence": 0.94,
            "mock": True
        }

    def process_batch(self, messages: list) -> list:
        results = []
        for msg in messages:
            try:
                result = self.process_message(msg)
                result["raw_text"] = msg
                result["status"] = "PENDING"
                results.append(result)
            except Exception as e:
                results.append({
                    "raw_text": msg,
                    "error": str(e),
                    "status": "FAILED"
                })
        return results