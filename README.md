# RescueIQ — Relief Coordination Agent

Multi-agent disaster relief coordination. Messy multilingual help requests go
in; a constraint-checked, routed, human-approvable dispatch plan comes out —
and when a road closes, the system replans and explains why.

**The rule the architecture follows:** Gemini understands. Python calculates.
Humans approve.

---

## Quick start

```powershell
cd backend

# 1. Create the virtual environment (once)
python -m venv venv

# 2. Install dependencies
.\venv\Scripts\python.exe -m pip install -r requirements.txt

# 3. Configure
copy .env.example .env

# 4. Run the API
.\venv\Scripts\python.exe -m uvicorn main:app --reload --port 8080
```

Then open **http://localhost:8080/docs** for interactive API documentation —
every endpoint can be called from that page, no frontend required.

### Verify it works

With the server running, in a second terminal:

```powershell
cd backend
.\venv\Scripts\python.exe test_graph.py   # road graph sanity checks
.\venv\Scripts\python.exe test_api.py     # full flow over HTTP
```

`test_api.py` walks the entire coordinator journey — intake in four languages,
dispatch planning, approval, a road closure, replanning, metrics and reset —
and exits non-zero if anything regresses.

---

## Mock mode

`MOCK_MODE` in `backend/.env` is the single switch for the whole system.

| | `MOCK_MODE=true` (default) | `MOCK_MODE=false` |
|---|---|---|
| Language extraction | Deterministic canned responses | Real Gemini via Vertex AI |
| Explanations | Templated | Gemini-written |
| Network needed | No | Yes (Vertex AI + Firestore) |

Everything else — routing, scoring, constraint checking, replanning — is real
Python in both modes. Mock mode exists so the system runs on a locked-down
network; it does not fake the parts being evaluated.

Firestore degrades the same way: if credentials are missing, the orchestrator
falls back to an in-memory resource list and logs that it did. The API stays
fully functional.

### Model choice

Verified working: **`gemini-2.5-flash`** in `us-central1` on project
`resourceworkflow`, via the `google-genai` SDK.

`gemini-2.0-flash` is retired and returns `404 NOT_FOUND` on this project.
`gemini-3.x` models appear in `models.list()` but are not enabled for it. If
you see a 404 naming a publisher model, the model is the problem, not your
credentials -- an auth failure would be 401/403.

The agents talk to Gemini through `config.gemini_model()`, which wraps the
Gen AI SDK in the `generate_content(prompt).text` shape the agents expect.
Changing SDK or model is a one-file change.

---

## API

Base URL `http://localhost:8080`. Full schema at `/docs`.

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/health` | Liveness, plus current mock/Firestore status |
| GET | `/graph` | Road graph nodes, edges and closed edges (for the map) |
| POST | `/intake/process` | Raw message in any language → structured request |
| POST | `/intake/batch` | Several messages at once |
| GET | `/requests` | All requests |
| GET | `/requests/pending` | Requests not yet assigned |
| GET | `/resources` | Resource list, filterable by `type` / `available_only` |
| GET | `/resources/summary` | Totals and availability per type (dashboard tiles) |
| POST | `/resources/release-all` | Force every resource back to available (recovery) |
| POST | `/dispatch/plan` | Build the assignment plan with explanations |
| GET | `/dispatch/current` | The plan currently awaiting approval |
| POST | `/dispatch/approve` | Commit the plan, mark resources busy |
| POST | `/replan/road-closure` | Close an edge, recompute affected routes |
| POST | `/replan/approve` | Accept the replanned routes |
| POST | `/replan/reopen` | Reopen a closed edge |
| GET | `/metrics` | RescueIQ vs first-come-first-served baseline |
| GET | `/events` | Audit log of closures and approvals |
| POST | `/reset` | Clear requests, plan and closures (between demo runs) |

### Example

```bash
curl -X POST http://localhost:8080/intake/process \
  -H "Content-Type: application/json" \
  -d "{\"message\": \"HELP ward7 paani bahut zyada 3 log please boat jaldi\"}"
```

---

## Architecture

```
React frontend  frontend/ (Vite + React 18)
      │ HTTP
FastAPI  backend/main.py                     ← transport + validation only
      │
RescueIQOrchestrator  backend/app/main_orchestrator.py
      │                                       ← owns live state, routes intent
      ├── IntakeAgent      language detect, extract, classify urgency   [Gemini]
      ├── ResourceAgent    Firestore reads, availability                [Python]
      ├── DispatchAgent    score candidates, explain tradeoffs          [both]
      └── ReplanAgent      detect impact, reroute, explain              [both]
                │
      ├── DistrictRouter     Dijkstra over the road graph               [Python]
      ├── ConstraintEngine   capacity + availability rules, scoring     [Python]
      └── EvaluationHarness  baseline comparison                        [Python]
                │
      Firestore  (requests · resources · assignments · road_graph)
```

### Layout

```
backend/
  main.py                  FastAPI app — all endpoints
  app/
    config.py              MOCK_MODE, project IDs, location→node map
    main_orchestrator.py   root agent, live state, approval flow
    agents/                intake · resource · dispatch · replan
    optimizer/
      router.py            Dijkstra, edge closure
      constraints.py       hard/soft rules, scoring
      evaluation.py        baseline vs RescueIQ metrics
    tools/firestore_tools.py
  test_api.py              end-to-end HTTP test
  test_graph.py            road graph resilience test
  app.py                   legacy Streamlit prototype (needs `pip install streamlit`)
data/
  road_graph.json          12 nodes, 17 edges
  seed_firestore.py        seeds resources, requests and the graph
Dockerfile                 Cloud Run container
```

### Scoring

Every candidate pairing is scored in Python, never by the model:

```
score = 100
      + urgency_bonus     (CRITICAL 40 · HIGH 25 · MEDIUM 10 · LOW 0)
      + 20 if vulnerable
      - 2 × ETA_minutes
      + 10 if available
```

Hard constraints (capacity, availability) are validated *before* scoring — a
candidate that violates one is never considered, so it cannot be outweighed by
a high urgency bonus.

---

## Metrics

`GET /metrics` compares the current plan against a first-come-first-served
baseline — requests served in arrival order by whichever resource is reachable
first, which is what an overloaded control room actually does.

Both plans are computed over the same road graph and the same resources, so
the comparison is real rather than asserted. The headline number is
`critical_first_pct`: the share of life-critical cases dispatched before any
routine one. Triage is the thing FCFS cannot do, and the gap shows it.

Reported numbers depend on the request mix loaded — they are computed at call
time, not hardcoded.

---

## Road graph

Synthetic, 12 nodes and 17 edges, in `data/road_graph.json`.

Every node has **at least two connections**. This matters: a node served by a
single road becomes unreachable the moment that road closes, and the replan
agent can only report `BLOCKED` — there is no alternative to find. With the
graph connected, closing any single edge forces a genuine reroute.
`test_graph.py` enforces this by closing all 17 edges one at a time and
checking that every node pair stays reachable.

The scripted demo closure is **Bridge Road (`N09__N10`)**, which reroutes
Control Room → Ward 7 from 13 to 22 minutes via Market Road.

### Node resolution

Gemini describes where a caller is in words ("near Ward 7 temple") and cannot
be relied on to invent graph node ids. Two things keep requests off the
default node:

1. The intake prompt is given the actual node list (`config.NODE_HINT`), so
   the model picks a real id.
2. `config.request_node()` falls back to matching the free-text description
   against ward numbers and landmarks.

Without both, every live request resolved to `N04` (Market Road) and the
dispatcher sent the wrong resources.

---

## Deploying to Cloud Run

Run from the repo root, once `gcloud` is installed and authenticated:

```powershell
gcloud config set project resourceworkflow

gcloud services enable run.googleapis.com cloudbuild.googleapis.com `
  aiplatform.googleapis.com firestore.googleapis.com

gcloud builds submit --tag gcr.io/resourceworkflow/rescueiq-api .

gcloud run deploy rescueiq-api `
  --image gcr.io/resourceworkflow/rescueiq-api `
  --region us-central1 `
  --platform managed `
  --allow-unauthenticated `
  --port 8080 `
  --memory 1Gi `
  --min-instances 1 `
  --set-env-vars "MOCK_MODE=false,GOOGLE_CLOUD_PROJECT=resourceworkflow,VERTEX_LOCATION=us-central1,FIRESTORE_DATABASE=rescource-graph,CORS_ORIGINS=*"
```

The container honours Cloud Run's injected `$PORT`. The service account needs
**Vertex AI User** and **Cloud Datastore User** roles.

Smoke test the deployment against the same suite used locally:

```powershell
$env:RESCUEIQ_API="https://<your-service-url>"
.\backend\venv\Scripts\python.exe backend\test_api.py
```

---

## Seeding Firestore

```powershell
cd data
..ackend\venv\Scripts\python.exe seed_firestore.py
```

Requires `backend/serviceAccountKey.json` (git-ignored, never commit it).

---

## Status

| Component | State |
|---|---|
| Five agents | Done |
| Router + constraint engine | Done |
| FastAPI backend | Done, tested end to end |
| Evaluation harness | Done |
| Firestore schema + seed | Done |
| Container + deploy config | Done, not yet deployed |
| Frontend | React console built (`frontend/`); needs `npm install` — Node not yet installed |
| Gemini live mode | Code paths written, needs a network that allows Vertex AI |
