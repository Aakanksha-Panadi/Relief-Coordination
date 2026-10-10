import json
import re

from app.config import (
    MOCK_MODE,
    NODE_HINT,
    NODE_IDS,
    URGENCY_LEVELS,
    gemini_model,
    normalize_urgency,
    resolve_node_from_text,
)


class IntakeExtractionError(RuntimeError):
    """Raised when a message could not be turned into a structured request.

    Carries the raw model output so a failed emergency can be inspected and
    replayed instead of vanishing into a stack trace.
    """

    def __init__(self, message: str, raw_output: str = ""):
        super().__init__(message)
        self.raw_output = raw_output


# Constrains Gemini at decode time: no markdown fences, no preamble, no
# urgency outside the canonical four, no invented node ids. "unknown" is an
# allowed node so the model can admit ignorance instead of guessing a ward.
EXTRACTION_SCHEMA = {
    "type": "OBJECT",
    "additionalProperties": False,
    "required": [
        "language",
        "translated_text",
        "location_description",
        "node_id",
        "people_count",
        "urgency",
        "vulnerable",
        "vulnerable_details",
        "needs",
        "confidence",
    ],
    "properties": {
        "language": {"type": "STRING"},
        "translated_text": {"type": "STRING"},
        "location_description": {"type": "STRING"},
        "node_id": {"type": "STRING", "enum": sorted(NODE_IDS) + ["unknown"]},
        "people_count": {"type": "INTEGER", "nullable": True},
        "urgency": {"type": "STRING", "enum": list(URGENCY_LEVELS)},
        "vulnerable": {"type": "BOOLEAN"},
        "vulnerable_details": {"type": "STRING"},
        "needs": {
            "type": "ARRAY",
            "items": {
                "type": "STRING",
                # Must match the `needs` list the prompt offers, including
                # "water", or the schema rejects a valid extraction.
                "enum": ["evacuation", "medical", "food", "shelter", "water"],
            },
        },
        "confidence": {"type": "NUMBER"},
    },
}


class IntakeAgent:
    def __init__(self):
        self.model = None if MOCK_MODE else gemini_model()

    def process_message(self, raw_message: str) -> dict:
        if not raw_message or not raw_message.strip():
            return self._unprocessed(raw_message or "", "Empty message")
        if len(raw_message.strip().split()) < 3:
            return self._unprocessed(raw_message, "Message is clipped or too short to extract safely")
        compact = re.sub(r"\s+", "", raw_message)
        if len(compact) > 8 and len(set(compact.lower())) <= 3:
            return self._unprocessed(raw_message, "Message appears noisy or unintelligible")
        if MOCK_MODE:
            result = self._mock_extract(raw_message)
        else:
            try:
                result = self._gemini_extract(raw_message)
            except IntakeExtractionError as exc:
                return self._unprocessed(raw_message, str(exc))
        return self._validate_extraction(raw_message, result)

    @staticmethod
    def _unprocessed(raw_message: str, reason: str) -> dict:
        return {"raw_text": raw_message, "translated_text": "", "language": "unknown",
                "location_description": "unknown", "node_id": None, "people_count": None,
                "urgency": "MEDIUM", "vulnerable": False, "vulnerable_details": "",
                "needs": [], "confidence": 0.05, "status": "UNPROCESSED",
                "processing_flags": [reason], "human_review_required": True}

    @staticmethod
    def _validate_extraction(raw_message: str, result: dict) -> dict:
        if not isinstance(result, dict):
            return IntakeAgent._unprocessed(raw_message, "Invalid extraction shape")
        translated = str(result.get("translated_text") or "")
        location = str(result.get("location_description") or "unknown")
        # Resolve against the caller's location, not arbitrary words in the
        # rest of the message. A claimed node unsupported by the location is discarded.
        supported_node = resolve_node_from_text(location)
        if result.get("mock"):
            # The offline demo extractor contains legacy sample branches. Do
            # not trust their canned details unless the caller actually said them.
            supported_node = resolve_node_from_text(raw_message)
            source_ward = re.search(r"\bward\s*(1|3|5|7|9)\b", raw_message, re.I)
            if source_ward:
                location = source_ward.group(0)
            elif supported_node:
                location = raw_message
            else:
                location = "unknown"
        explicit_node = re.search(r"\bN\d{2}\b", location, re.I)
        if (explicit_node and explicit_node.group(0).upper() in NODE_IDS
                and (not result.get("mock") or explicit_node.group(0).upper() in raw_message.upper())):
            supported_node = explicit_node.group(0).upper()
        count = result.get("people_count")
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0 or count > 10000:
            count = None
        # Don't accept a model-invented number when no people count appears in
        # either the source or its translation.
        count_pattern = r"\b\d+\s*(?:people|persons|person|of us|members|famil(?:y|ies)|passengers|ppl|log|lok|per)\b"
        count_evidence = re.search(count_pattern, raw_message if result.get("mock") else raw_message + " " + translated, re.I)
        if not count_evidence:
            count = None
        flags = list(result.get("processing_flags") or [])
        if count is None:
            flags.append("people_count_missing")
        if supported_node is None:
            flags.append("location_unknown_or_ambiguous")
        language = str(result.get("language") or "unknown").lower()
        if result.get("mock") and any(ord(char) > 127 for char in raw_message):
            has_hindi = any("\u0900" <= char <= "\u097f" for char in raw_message)
            has_tamil = any("\u0b80" <= char <= "\u0bff" for char in raw_message)
            language = "hindi" if has_hindi else "tamil" if has_tamil else "unknown"
            result["confidence"] = min(result.get("confidence", 0.0), 0.1)
            if language == "unknown":
                flags.append("noisy_or_unsupported_text")
        if not any(name in language for name in ("english", "hindi", "tamil", "mixed", "hinglish")):
            flags.append("unsupported_language_or_language_uncertain")
        text = f"{raw_message} {translated}".lower()
        # Vulnerability evidence overrides reassuring language. Escalate
        # conservatively: an explicit mobility/medical danger is critical.
        vulnerable_terms = ("cannot walk", "can't walk", "unable to walk", "bedridden",
                            "infant", "baby", "disabled", "elderly", "pregnant",
                            "child", "children", "wheelchair")
        source_text = raw_message.lower()
        vulnerable_evidence = any(t in text for t in vulnerable_terms)
        if result.get("mock"):
            vulnerable_evidence = any(t in source_text for t in vulnerable_terms)
        vulnerable = (bool(result.get("vulnerable")) and vulnerable_evidence) or vulnerable_evidence
        urgency = normalize_urgency(result.get("urgency"))
        if result.get("mock") and not any(term in source_text for term in ("urgent", "trapped", "drowning", "can't walk", "cannot walk", "insulin")):
            urgency = "HIGH" if any(term in source_text for term in ("help", "rescue", "flood", "bachao")) else "MEDIUM"
        if vulnerable and any(t in text for t in ("cannot walk", "can't walk", "unable to walk", "bedridden")):
            urgency = "CRITICAL"
        elif vulnerable and urgency in ("LOW", "MEDIUM"):
            urgency = "HIGH"
        # A message with clearly independent groups is retained for review;
        # don't flatten several households into one demand record.
        multi = bool(re.search(r"\b(and|also) (the )?(neighbou?rs|other family|next door)\b", text))
        if multi:
            flags.append("multiple_requests_possible")
        confidence = result.get("confidence")
        confidence = max(0.0, min(1.0, float(confidence))) if isinstance(confidence, (int, float)) else 0.0
        if "unsupported_language_or_language_uncertain" in flags or "noisy_or_unsupported_text" in flags:
            confidence = min(confidence, 0.19)
        human_review = bool(flags) or confidence < 0.5
        return {**result, "raw_text": raw_message, "translated_text": translated,
                "location_description": location, "node_id": supported_node,
                "location_resolved": supported_node is not None, "people_count": count,
                "urgency": urgency, "vulnerable": vulnerable,
                "vulnerable_details": result.get("vulnerable_details") or ("vulnerability mentioned" if vulnerable else ""),
                "needs": [n for n in (result.get("needs") or []) if n in ("evacuation", "medical", "food", "shelter", "water")],
                "confidence": confidence, "processing_flags": list(dict.fromkeys(flags)),
                "human_review_required": human_review,
                "status": "UNPROCESSED" if confidence < 0.2 or "unsupported_language_or_language_uncertain" in flags else "PENDING"}

    def _gemini_extract(self, raw_message: str) -> dict:
        if self.model is None:
            return self._mock_extract(raw_message)

        prompt = f"""
You are RescueIQ, an AI disaster-relief intake agent.

The incoming emergency message may be written in English,
Hindi, Tamil, Hinglish, or another language.

Your task is to:
1. Detect the original language.
2. Translate the COMPLETE message accurately into English.
3. Extract the location mentioned in the original message.
4. Match the location to the best-supported node from the
   district node list provided below.
5. Extract the number of people needing assistance.
6. Assess urgency based on the actual message.
7. Identify vulnerable people and their needs.
8. Identify the relief services requested.

Available district locations and node mappings:
{NODE_HINT}

Return ONLY a valid JSON object with exactly these fields:
{{
  "language": "original language name",
  "translated_text": "complete English translation",
  "location_description": "location mentioned or unknown",
  "node_id": "matching node ID or unknown",
  "people_count": null,
  "urgency": "CRITICAL or HIGH or MEDIUM or LOW",
  "vulnerable": false,
  "vulnerable_details": "",
  "needs": [],
  "confidence": 0.0
}}

Rules:
- Translate the actual input. Never use a predefined translation.
- Preserve the meaning, numbers, locations, and requests.
- Do not invent missing information.
- Match node_id only when the location supports the match.
  Otherwise return "unknown".
- Use null if the number of people is not stated. Never infer a count.
- needs may contain: evacuation, medical, food, shelter, water.
- CRITICAL: immediate threat to life or serious medical danger.
- HIGH: urgent rescue or evacuation is needed.
- MEDIUM: assistance is needed, but no immediate danger is stated.
- LOW: non-urgent assistance.
- vulnerable is true if children, elderly people, disabled people,
  or people with serious medical needs are mentioned.
- confidence must be a number between 0 and 1.
- Return JSON only, without Markdown fences.

Emergency message:
{json.dumps(raw_message, ensure_ascii=False)}
"""

        try:
            response = self.model.generate_content(
                prompt, schema=EXTRACTION_SCHEMA
            )
            text = (response.text or "").strip()
        except Exception as e:
            # Network error, permission denied, or a safety filter leaving the
            # candidate empty. Either way there is no extraction to return.
            raise IntakeExtractionError(f"Gemini call failed: {e}") from e

        if not text:
            raise IntakeExtractionError(
                "Gemini returned an empty response (possibly safety-filtered)"
            )

        # The schema should make fences impossible; strip them anyway so a
        # config regression degrades instead of failing outright.
        cleaned = re.sub(r"```(?:json)?|```", "", text).strip()
        try:
            result = json.loads(cleaned)
        except ValueError as e:
            raise IntakeExtractionError(
                f"Gemini did not return valid JSON: {e}", raw_output=text
            ) from e

        if not isinstance(result, dict):
            raise IntakeExtractionError(
                "Gemini returned JSON that is not an object", raw_output=text
            )

        result["urgency"] = normalize_urgency(result.get("urgency"))

        # The model may legitimately answer "unknown" for a vague message.
        # Recover from the free-text description before giving up. Leaving
        # node_id unset is deliberate: request_node() then returns None and
        # the request surfaces as unassigned instead of silently routing to
        # a default ward.
        if result.get("node_id") not in NODE_IDS:
            resolved = resolve_node_from_text(
                result.get("location_description", "")
            ) or resolve_node_from_text(result.get("translated_text", ""))
            result["node_id"] = resolved
            result["location_resolved"] = resolved is not None
        else:
            result["location_resolved"] = True
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
            "location_resolved": True,
            "mock": True
        }

    def process_batch(self, messages: list) -> list:
        results = []
        for msg in messages:
            try:
                result = self.process_message(msg)
                result["raw_text"] = msg
                results.append(result)
            except Exception as e:
                results.append({
                    "raw_text": msg,
                    "error": str(e),
                    "status": "FAILED"
                })
        return results
