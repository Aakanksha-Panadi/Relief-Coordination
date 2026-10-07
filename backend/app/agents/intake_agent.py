import vertexai
from vertexai.generative_models import GenerativeModel
import json
import re
import os
from dotenv import load_dotenv

load_dotenv()

class IntakeAgent:
    def __init__(self):
        vertexai.init(
            project=os.getenv("GOOGLE_CLOUD_PROJECT", "resourceworkflow"),
            location="us-central1"
        )
        self.model = GenerativeModel("gemini-2.0-flash")

    def process_message(self, raw_message: str) -> dict:
        prompt = f"""
You are a disaster relief intake agent processing emergency help requests.
Extract information and return ONLY valid JSON, no explanation, no markdown.

Message: "{raw_message}"

Return exactly this JSON structure:
{{
  "language": "detected language name",
  "translated_text": "english translation of message",
  "location_description": "location mentioned or unknown",
  "people_count": 0,
  "urgency": "CRITICAL or HIGH or MEDIUM or LOW",
  "vulnerable": true or false,
  "vulnerable_details": "elderly/children/medical details or empty string",
  "needs": ["evacuation", "medical", "food", "shelter"],
  "confidence": 0.95
}}

Rules:
- CRITICAL = life threatening, elderly/children, rising water
- HIGH = stuck, need evacuation soon
- MEDIUM = need help but stable
- LOW = information request
- vulnerable = true if elderly, children, disabled, or medical emergency
"""
        response = self.model.generate_content(prompt)
        
        # Clean response and parse JSON
        text = response.text.strip()
        text = re.sub(r'```json|```', '', text).strip()
        
        return json.loads(text)

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