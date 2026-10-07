import vertexai
from vertexai.generative_models import GenerativeModel

vertexai.init(
    project="resourceworkflow",
    location="us-central1"
)

model = GenerativeModel("gemini-2.0-flash")

print("TEST 1 — English")
response = model.generate_content("""
You are a disaster relief intake agent.
Extract information and return ONLY valid JSON.

Message: "Help!! 6 people stuck on roof near City Bank 
Market Road, 2 children please send boat urgent"

Return exactly this JSON:
{
  "language": "detected language",
  "translated_text": "english translation",
  "location_description": "location mentioned",
  "people_count": 0,
  "urgency": "CRITICAL or HIGH or MEDIUM or LOW",
  "vulnerable": true,
  "vulnerable_details": "details",
  "needs": ["evacuation"],
  "confidence": 0.95
}
""")
print(response.text)
print()

print("TEST 2 — Hindi")
response = model.generate_content("""
You are a disaster relief intake agent.
Extract information and return ONLY valid JSON.

Message: "मेरे घर में पानी भर गया है, 4 लोग हैं, 
बुजुर्ग माँ है चल नहीं सकती, Ward 7 मंदिर के पास"

Return exactly this JSON:
{
  "language": "detected language",
  "translated_text": "english translation",
  "location_description": "location mentioned",
  "people_count": 0,
  "urgency": "CRITICAL or HIGH or MEDIUM or LOW",
  "vulnerable": true,
  "vulnerable_details": "details",
  "needs": ["evacuation"],
  "confidence": 0.95
}
""")
print(response.text)
print()

print("TEST 3 — Mixed")
response = model.generate_content("""
You are a disaster relief intake agent.
Extract information and return ONLY valid JSON.

Message: "HELP ward7 paani bahut zyada 3 log please boat jaldi"

Return exactly this JSON:
{
  "language": "detected language",
  "translated_text": "english translation",
  "location_description": "location mentioned",
  "people_count": 0,
  "urgency": "CRITICAL or HIGH or MEDIUM or LOW",
  "vulnerable": false,
  "vulnerable_details": "",
  "needs": ["evacuation"],
  "confidence": 0.95
}
""")
print(response.text)