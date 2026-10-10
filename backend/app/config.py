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
    """Graph node a resource currently sits on."""
    if resource.get("node_id"):
        return resource["node_id"]
    return LOCATION_TO_NODE.get(resource.get("location", ""), DEFAULT_NODE)


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


def request_node(request: dict) -> str:
    """Graph node a request is calling from.

    Order matters: an explicit node id wins, then the structured location
    field, then whatever the model wrote in free text. Falling straight to
    the default would silently route every live request to Market Road.
    """
    node_id = request.get("node_id")
    if node_id and node_id in NODE_IDS:
        return node_id

    mapped = LOCATION_TO_NODE.get(request.get("location", ""))
    if mapped:
        return mapped

    from_text = resolve_node_from_text(
        request.get("location_description", "")
    ) or resolve_node_from_text(request.get("translated_text", ""))
    return from_text or "N04"


class _GeminiClient:
    """Adapter over the Google Gen AI SDK.

    The agents call `model.generate_content(prompt).text`, which was the old
    vertexai.generative_models shape. Keeping that surface here means the
    SDK migration touched one file instead of every agent.
    """

    def __init__(self, client, model_name: str):
        self._client = client
        self._model = model_name

    def generate_content(self, prompt: str):
        return self._client.models.generate_content(
            model=self._model, contents=prompt
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
