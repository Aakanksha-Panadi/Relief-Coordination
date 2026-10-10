"""RescueIQ FastAPI backend.

The bridge between the agent layer and the frontend. Every endpoint is a thin
wrapper over RescueIQOrchestrator: the API does transport and validation, the
orchestrator does the work.

Run locally:  uvicorn main:app --reload --port 8080
Docs:         http://localhost:8080/docs
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app import config
from app.agents.intake_agent import IntakeExtractionError
from app.agents.resource_agent import PersistenceError
from app.main_orchestrator import RescueIQOrchestrator

# Single long-lived orchestrator: it holds the in-flight plan and road
# closures, which must survive across requests for the demo to work.
orchestrator: RescueIQOrchestrator | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global orchestrator
    print("Starting RescueIQ... (MOCK_MODE=%s)" % config.MOCK_MODE)
    orchestrator = RescueIQOrchestrator()
    print("RescueIQ ready.")
    yield
    print("RescueIQ shutting down.")


app = FastAPI(
    title="RescueIQ API",
    description="Multi-agent disaster relief coordination",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_orchestrator() -> RescueIQOrchestrator:
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="System still starting up")
    return orchestrator


def unwrap(result: dict) -> dict:
    """Orchestrator signals failure with an `error` key; surface it as 4xx."""
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


# ---------- request models ----------


class IntakeRequest(BaseModel):
    message: str = Field(..., min_length=1, description="Raw help message, any language")


class BatchIntakeRequest(BaseModel):
    messages: list[str] = Field(..., min_length=1)


class RoadClosureRequest(BaseModel):
    edge_id: str = Field(..., description="Edge to close, e.g. N09__N10")


class ApprovalRequest(BaseModel):
    approved_by: str = "coordinator"


class CompleteRequest(BaseModel):
    request_id: str = Field(..., min_length=1, description="Request that has been served")


# ---------- health & meta ----------


@app.get("/health", tags=["meta"])
def health():
    orch = get_orchestrator()
    return {
        "status": "healthy",
        "service": "RescueIQ",
        "mock_mode": config.MOCK_MODE,
        "firestore": orch.db is not None,
        # "fallback" means the hardcoded 8-resource fleet is live instead of
        # the 16 seeded ones — different capacities and positions entirely.
        "resource_source": orch.resource_agent.source,
        "resource_count": len(orch.resources),
    }


@app.get("/", tags=["meta"])
def root():
    return {"service": "RescueIQ API", "docs": "/docs", "health": "/health"}


@app.get("/graph", tags=["meta"])
def graph():
    """Road graph plus currently closed edges, for map rendering."""
    return get_orchestrator().get_graph()


# ---------- intake ----------


@app.post("/intake/process", tags=["intake"])
def intake_process(payload: IntakeRequest):
    """Raw multilingual message in, structured request out.

    Failures are explicit on purpose: a request that was not extracted or not
    stored must not come back as 200 with a plausible-looking body.
    """
    try:
        return get_orchestrator().process_intake(payload.message)
    except IntakeExtractionError as e:
        raise HTTPException(
            status_code=502,
            detail={"error": "Extraction failed", "message": str(e),
                    "raw_output": e.raw_output[:500]},
        ) from e
    except PersistenceError as e:
        raise HTTPException(
            status_code=503,
            detail={"error": "Request was not stored", "message": str(e)},
        ) from e


@app.post("/intake/batch", tags=["intake"])
def intake_batch(payload: BatchIntakeRequest):
    """Several messages at once, for the 'three messages land together' demo."""
    results = get_orchestrator().process_intake_batch(payload.messages)
    return {"processed": len(results), "requests": results}


@app.get("/requests", tags=["intake"])
def list_requests():
    reqs = get_orchestrator().get_requests()
    return {"count": len(reqs), "requests": reqs}


@app.get("/requests/pending", tags=["intake"])
def pending_requests():
    reqs = get_orchestrator().get_requests(status="PENDING")
    return {"count": len(reqs), "requests": reqs}


# ---------- resources ----------


@app.get("/resources", tags=["resources"])
def list_resources(type: str | None = None, available_only: bool = False):
    agent = get_orchestrator().resource_agent
    resources = agent.get_available(type) if available_only else agent.get_all_resources()
    if type and not available_only:
        resources = [r for r in resources if r.get("type") == type]
    return {"count": len(resources), "resources": resources}


@app.get("/resources/summary", tags=["resources"])
def resources_summary():
    """Totals and availability per resource type, for the dashboard tiles."""
    return get_orchestrator().get_resource_summary()


# ---------- dispatch ----------


@app.post("/dispatch/plan", tags=["dispatch"])
def dispatch_plan():
    """Score every request/resource pair and return the proposed assignments."""
    return unwrap(get_orchestrator().generate_dispatch_plan())


@app.get("/dispatch/current", tags=["dispatch"])
def dispatch_current():
    plan = get_orchestrator().current_plan
    if not plan:
        raise HTTPException(status_code=404, detail="No active dispatch plan")
    return plan


@app.post("/dispatch/approve", tags=["dispatch"])
def dispatch_approve(payload: ApprovalRequest | None = None):
    """Human in the loop. Commits the plan and marks resources busy."""
    approved_by = payload.approved_by if payload else "coordinator"
    return unwrap(get_orchestrator().approve_plan(approved_by))


@app.post("/dispatch/complete", tags=["dispatch"])
def dispatch_complete(payload: CompleteRequest):
    """Rescue finished: free the resource and move it to where it went.

    The only route back from busy other than a full reset.
    """
    return unwrap(get_orchestrator().complete_assignment(payload.request_id))


# ---------- replanning ----------


@app.post("/replan/road-closure", tags=["replan"])
def replan_road_closure(payload: RoadClosureRequest):
    """Close a road and recompute every affected route."""
    return unwrap(get_orchestrator().simulate_road_closure(payload.edge_id))


@app.post("/replan/approve", tags=["replan"])
def replan_approve(payload: ApprovalRequest | None = None):
    approved_by = payload.approved_by if payload else "coordinator"
    return unwrap(get_orchestrator().approve_replan(approved_by))


@app.post("/replan/reopen", tags=["replan"])
def replan_reopen(payload: RoadClosureRequest):
    return get_orchestrator().reopen_edge(payload.edge_id)


# ---------- metrics & lifecycle ----------


@app.get("/metrics", tags=["metrics"])
def metrics():
    """RescueIQ vs a first-come-first-served baseline, over the current plan."""
    return unwrap(get_orchestrator().get_metrics())


@app.get("/events", tags=["metrics"])
def events():
    ev = get_orchestrator().events
    return {"count": len(ev), "events": ev}


@app.post("/reset", tags=["metrics"])
def reset():
    """Clear requests, plan and road closures. Used between demo runs.

    Writes through to Firestore so the next run starts with a full fleet.
    """
    return get_orchestrator().reset()


@app.post("/resources/release-all", tags=["resources"])
def release_all_resources():
    """Force every resource back to available.

    Recovery hatch when an earlier run (or a crash) left resources marked
    busy in Firestore.
    """
    return get_orchestrator().release_all_resources()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=config.PORT, reload=True)
