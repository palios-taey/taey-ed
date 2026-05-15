"""FastAPI scaffold for spark_v2.

Phase B only wires the endpoint surface and the decision-pipeline shell.
Real behavior lands in later rebuild phases.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from spark_v2.routes.next_action import decide_next_action

app = FastAPI(title="Taey-Ed Spark V2", version="0.1.0-phase-b")

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
        "phase": "C",
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


@app.post("/api/v1/embed")
async def embed(_: Request) -> JSONResponse:
    # TODO Phase C7: replace placeholder with knowledge-gated compute path.
    return _todo_stub("/api/v1/embed", "Phase C7")


@app.post("/api/v1/generate")
async def generate(_: Request) -> JSONResponse:
    # TODO Phase C7: wire Claude-only generation through spark_v2 worker/task layer.
    return _todo_stub("/api/v1/generate", "Phase C7")


@app.post("/api/v1/action_review")
async def action_review(_: Request) -> JSONResponse:
    # TODO Phase E: implement failure-recovery loop ingestion and routing.
    return _todo_stub("/api/v1/action_review", "Phase E")


@app.post("/api/v1/abandon_consultation/{consultation_id}")
async def abandon_consultation(consultation_id: str) -> JSONResponse:
    # TODO Phase E: implement lifecycle-aware consultation abandonment semantics.
    meta_path = CONSULT_DIR / consultation_id / "metadata.json"
    return _todo_stub(
        "/api/v1/abandon_consultation/{id}",
        "Phase E",
        {
            "consultation_id": consultation_id,
            "metadata_exists": meta_path.exists(),
        },
    )
