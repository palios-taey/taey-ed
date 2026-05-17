"""FastAPI scaffold for spark_v2.

Phase B only wires the endpoint surface and the decision-pipeline shell.
Real behavior lands in later rebuild phases.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from spark_v2.routes import abandon as abandon_route
from spark_v2.routes import auth_stub
from spark_v2.routes import credits as credits_route
from spark_v2.routes import embed as embed_route
from spark_v2.routes import generate as generate_route
from spark_v2.routes.next_action import decide_next_action

app = FastAPI(title="Taey-Ed Spark V2", version="0.1.0-phase-b")
app.include_router(abandon_route.router)
app.include_router(auth_stub.router)
app.include_router(credits_route.router)
app.include_router(embed_route.router)
app.include_router(generate_route.router)

CONSULT_DIR = Path("/tmp/taey-ed-consult-v2")


def _todo_stub(endpoint: str, phase: str, extra: dict | None = None) -> JSONResponse:
    payload = {
        "ok": False,
        "endpoint": endpoint,
        "todo": phase,
        "message": f"Phase B scaffold placeholder. Implement in {phase}.",
    }
    if extra:
        payload.update(extra)
    return JSONResponse(status_code=501, content=payload)


@app.get("/health")
async def health() -> dict:
    return {
        "status": "healthy",
        "service": "spark_v2",
        "phase": "D-alpha+C7+mac-endpoints",
        "consult_dir": str(CONSULT_DIR),
    }


@app.post("/next_action")
async def next_action(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {"raw_payload": payload}
    return JSONResponse(content=decide_next_action(payload))


@app.post("/api/v1/action_review")
async def action_review(_: Request) -> JSONResponse:
    # TODO Phase E: implement failure-recovery loop ingestion and routing.
    return _todo_stub("/api/v1/action_review", "Phase E")

