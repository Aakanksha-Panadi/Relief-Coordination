# RescueIQ frontend

React coordinator console for the RescueIQ relief coordination API. Every
panel is bound to live backend state — there is no mock data in this app.

## Prerequisites

**Node.js 18 or newer** (ships with npm). Not currently installed on this
machine — get it from <https://nodejs.org>, then reopen your terminal.

Verify:

```powershell
node --version
npm --version
```

## Run it

```powershell
cd frontend
npm install
npm run dev
```

Opens on <http://localhost:5173>.

The backend must be running too, in a second terminal:

```powershell
cd backend
.\venv\Scripts\python.exe -m uvicorn main:app --reload --port 8080
```

If the backend is unreachable the app says so in a banner rather than
rendering empty panels.

## Configuration

Copy `.env.example` to `.env` to point at a different backend:

```
VITE_API_BASE=http://localhost:8080
VITE_POLL_SECONDS=10
```

For the deployed service use the Cloud Run HTTPS URL. `VITE_POLL_SECONDS=0`
turns off background refresh.

## Build for production

```powershell
npm run build     # outputs to frontend/dist
npm run preview   # serve the build locally to check it
```

## What each panel does

| Panel | Backend call |
|---|---|
| Intake textarea → **Process request** | `POST /intake/process` |
| **Load all 5** (multilingual demo set) | `POST /intake/batch` |
| Request queue (sorted by urgency) | `GET /requests`, polled |
| District map | `GET /graph` |
| **Generate plan** | `POST /dispatch/plan` |
| **Approve plan** | `POST /dispatch/approve` |
| **Close road** / map road click | `POST /replan/road-closure` |
| **Approve replan** | `POST /replan/approve` |
| Results tab | `GET /metrics` |
| **Reset** / **Release all** | `POST /reset`, `POST /resources/release-all` |

## Notes on behaviour

**The input field is the point.** Type or paste a message in any language and
press *Process request* (or Ctrl+Enter). The extracted structure — language,
urgency, people count, resolved map node, vulnerability — appears directly
beneath the box, and the request joins the queue. Sample chips fill the box
for the demo; they do not bypass it.

**The map is generated from the real graph.** Node positions are projected
from the lat/lng in `data/road_graph.json`, so the map follows the data if
nodes change. Click any road to close it and trigger a replan. Roads carrying
a planned route are drawn in blue; rerouted ones in green; closed ones dashed
red.

**Mock mode is shown, not hidden.** The pill next to the logo reads
`MOCK MODE` or `LIVE GEMINI` from `GET /health`, so a demo is never mistaken
for live model output. The footer likewise shows whether Firestore is
connected and whether the fleet is the seeded one or the fallback.

**Failures surface.** The backend returns explicit errors when extraction or
persistence fails rather than a plausible-looking 200; those messages are
shown as toasts instead of being swallowed. Text you typed is kept if a
submission fails.

## Original mockup

The static design this was built from is preserved at `mockup.html`. It is
not part of the build — open it directly in a browser if you want to compare.
