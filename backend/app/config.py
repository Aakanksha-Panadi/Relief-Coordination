"""Central configuration for RescueIQ.

Everything that used to be a hardcoded constant scattered across the agents
lives here, so the whole system can be flipped between mock and live Gemini
with a single environment variable.
"""
import json
import os
from dotenv import load_dotenv

load_dotenv()


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


# MOCK_MODE=true  -> deterministic canned extraction/explanations, no network.
# MOCK_MODE=false -> real Gemini calls through Vertex AI.
MOCK_MODE = _flag("MOCK_MODE", "true")

GCP_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "resourceworkflow")
VERTEX_LOCATION = os.getenv("VERTEX_LOCATION", "us-central1")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# Extraction is a determinism task: the same emergency message must always
# yield the same urgency and people_count, or re-running a scenario silently
# produces a different plan.
GEMINI_TEMPERATURE = float(os.getenv("GEMINI_TEMPERATURE", "0"))

FIRESTORE_DATABASE = os.getenv("FIRESTORE_DATABASE", "rescource-graph")

PORT = int(os.getenv("PORT", "8080"))
CORS_ORIGINS = [
    o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()
]

# Where the road graph lives, relative to this file.
ROAD_GRAPH_PATH = os.getenv(
    "ROAD_GRAPH_PATH",
    os.path.join(os.path.dirname(__file__), "..", "..", "data", "road_graph.json"),
)

# Resources are seeded with human-readable depot names; the router works on
# graph node ids. This is the single source of truth for that translation.
LOCATION_TO_NODE = {
    "Central_Depot": "N01",
    "Central_Sports_Complex": "N01",
    "Zone_A_Temple": "N03",
    "Zone_C_Temple": "N03",
    "Zone_A_School": "N07",
    "Zone_C_School": "N07",
    "Zone_B_Community_Hall": "N08",
    "Zone_A_Dock": "N11",
    "Zone_B_Dock": "N12",
    "Zone_C_Dock": "N12",
    # Request-side locations, in case a request arrives pre-geocoded.
    "Ward_1": "N02",
    "Ward_3": "N03",
    "Market_Road": "N04",
    "Ward_5": "N04",
    "Ward_7": "N05",
    "Ward_9": "N06",
}

DEFAULT_NODE = "N01"


def _load_nodes() -> dict:
    try:
        with open(os.path.abspath(ROAD_GRAPH_PATH), encoding="utf-8") as f:
            return json.load(f).get("nodes", {})
    except (OSError, ValueError):
        return {}


NODES = _load_nodes()
NODE_IDS = set(NODES)

# Handed to Gemini so it picks a real node instead of guessing "unknown".
NODE_HINT = "\n".join(
    f"  {nid} = {node.get('name', nid)}" for nid, node in sorted(NODES.items())
)


def resource_node(resource: dict) -> str:
    """Graph node a resource currently sits on.

    `currentNode` wins because it is updated when a trip completes; `node_id`
    and `location` only ever describe where the resource originally started.
    """
    if resource.get("currentNode") in NODE_IDS:
        return resource["currentNode"]
    if resource.get("node_id"):
        return resource["node_id"]
    return LOCATION_TO_NODE.get(resource.get("location", ""), DEFAULT_NODE)


# Shelters are seeded with totalCapacity/currentOccupancy while boats and
# teams use capacity/currentLoad. Read both so a shelter does not silently
# present as capacity 0 and fail every constraint check.
def resource_capacity(resource: dict) -> int:
    for key in ("capacity", "totalCapacity"):
        value = resource.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    return 0


def resource_load(resource: dict) -> int:
    for key in ("currentLoad", "currentOccupancy"):
        value = resource.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    return 0


# Resource types that can be sent to a caller. Shelters are destinations
# people are brought to, not responders, so they are deliberately excluded.
DISPATCHABLE_TYPES = ("boat", "volunteer_team", "medical_unit")

# Which capabilities satisfy a stated need. Volunteer teams carry `skills`
# in Firestore; medical units are treated as inherently medical-capable.
NEED_SKILLS = {
    "medical": ("first_aid", "medical_support", "medical"),
    "evacuation": ("evacuation", "search_rescue"),
    "food": ("food_distribution",),
    "shelter": ("shelter_management", "crowd_management"),
}


def meets_needs(resource: dict, needs) -> int:
    """How many of a request's needs this resource is equipped for."""
    if not needs:
        return 0
    skills = {str(s).lower() for s in resource.get("skills", [])}
    if resource.get("type") == "medical_unit":
        skills.add("medical")
    if resource.get("type") == "boat":
        skills.add("evacuation")
    met = 0
    for need in needs:
        wanted = NEED_SKILLS.get(str(need).lower(), ())
        if skills.intersection(wanted):
            met += 1
    return met


URGENCY_LEVELS = ("CRITICAL", "HIGH", "MEDIUM", "LOW")


def normalize_urgency(value) -> str:
    """Fold model/seed casing onto the canonical set.

    Without this, "Critical" misses every dict lookup keyed on "CRITICAL" and
    a life-threatening request is sorted and scored as LOW.
    """
    if isinstance(value, str):
        upper = value.strip().upper().replace(" ", "_")
        if upper in URGENCY_LEVELS:
            return upper
    return "MEDIUM"


# The seed script writes camelCase; the intake agent emits snake_case. Both
# end up in the same `requests` collection, so everything downstream has to
# read one shape. This is that shape.
_REQUEST_ALIASES = {
    "rawText": "raw_text",
    "peopleCount": "people_count",
    "vulnerableDetails": "vulnerable_details",
    "locationDescription": "location_description",
    "translatedText": "translated_text",
    "nodeId": "node_id",
    "assignedResources": "assigned_resources",
}


def normalize_request(request: dict) -> dict:
    """Return a copy with canonical snake_case keys and safe defaults.

    Downstream code formats these into prompts with direct subscripts, so a
    missing key is a 500 rather than a degraded explanation.
    """
    out = dict(request)
    for alias, canonical in _REQUEST_ALIASES.items():
        if alias in out and canonical not in out:
            out[canonical] = out.pop(alias)

    out.setdefault("raw_text", "")
    out.setdefault("translated_text", out.get("raw_text", ""))
    out.setdefault("vulnerable_details", "")
    out.setdefault("needs", [])
    out.setdefault("vulnerable", False)
    out["urgency"] = normalize_urgency(out.get("urgency"))

    people = out.get("people_count")
    out["people_count"] = int(people) if isinstance(people, (int, float)) else 0

    if not out.get("location_description"):
        # Seeded requests carry a structured `location` ("Ward_7") instead.
        out["location_description"] = str(
            out.get("location") or "unknown"
        ).replace("_", " ")

    if out.get("node_id") not in NODE_IDS:
        resolved = request_node(out)
        out["node_id"] = resolved if resolved else None
    return out


# Gemini reliably describes *where* a caller is ("near Ward 7 temple") but has
# no way to know the graph's node ids, so it often returns "unknown". These
# patterns recover the node from the free-text description. Ward numbers are
# checked before landmarks because "Ward 7 temple" contains both.
_TEXT_TO_NODE = [
    ("ward 1", "N02"), ("ward1", "N02"),
    ("ward 3", "N03"), ("ward3", "N03"),
    ("ward 5", "N04"), ("ward5", "N04"),
    ("ward 7", "N05"), ("ward7", "N05"),
    ("ward 9", "N06"), ("ward9", "N06"),
    ("old town", "N05"),
    ("market", "N04"),
    ("hospital", "N06"),
    ("temple street", "N03"),
    ("riverside", "N02"),
    ("community hall", "N08"),
    ("school", "N07"),
    ("bridge", "N09"),
    ("jetty", "N11"),
    ("control room", "N01"),
    ("depot", "N01"),
    ("temple", "N03"),
]


def resolve_node_from_text(text: str) -> str | None:
    """Best-effort node lookup from a free-text location description."""
    if not text:
        return None
    lowered = text.lower()
    for pattern, node in _TEXT_TO_NODE:
        if pattern in lowered:
            return node
    return None


def request_node(request: dict) -> str | None:
    """Graph node a request is calling from, or None if it cannot be resolved.

    Order matters: an explicit node id wins, then the structured location
    field, then whatever the model wrote in free text.

    Returns None rather than a default node on failure. An unlocatable caller
    must surface as unassigned — defaulting to Market Road produced a plan
    that looked complete while sending a boat to the wrong ward.
    """
    node_id = request.get("node_id")
    if node_id and node_id in NODE_IDS:
        return node_id

    mapped = LOCATION_TO_NODE.get(request.get("location", ""))
    if mapped:
        return mapped

    return resolve_node_from_text(
        request.get("location_description", "")
    ) or resolve_node_from_text(request.get("translated_text", ""))


class _GeminiClient:
    """Adapter over the Google Gen AI SDK.

    The agents call `model.generate_content(prompt).text`, which was the old
    vertexai.generative_models shape. Keeping that surface here means the
    SDK migration touched one file instead of every agent.
    """

    def __init__(self, client, model_name: str):
        self._client = client
        self._model = model_name

    def generate_content(self, prompt: str, schema: dict | None = None):
        """Generate text, or structured JSON when `schema` is supplied.

        With a schema the model is constrained at decode time, so it cannot
        emit markdown fences, a preamble, or a value outside an enum. That
        removes the whole class of "parse whatever came back" failures.
        """
        gen_config: dict = {"temperature": GEMINI_TEMPERATURE}
        if schema is not None:
            gen_config["response_mime_type"] = "application/json"
            gen_config["response_schema"] = schema
        return self._client.models.generate_content(
            model=self._model, contents=prompt, config=gen_config
        )


def gemini_model():
    """Build a Gemini client against Vertex AI. Only called when MOCK_MODE is off.

    Uses google-genai; vertexai.generative_models is deprecated and scheduled
    for removal from google-cloud-aiplatform.
    """
    try:
        from google import genai
    except ImportError as e:
        raise RuntimeError(
            "MOCK_MODE=false needs the Google Gen AI SDK. Either run "
            "'pip install google-genai' or set MOCK_MODE=true in "
            "backend/.env to use the offline mock."
        ) from e

    client = genai.Client(
        vertexai=True, project=GCP_PROJECT, location=VERTEX_LOCATION
    )
    return _GeminiClient(client, GEMINI_MODEL)
