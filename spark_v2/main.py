"""FastAPI app for spark_v2."""

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

app = FastAPI(title="Taey-Ed Spark V2", version="0.1.0")
app.include_router(abandon_route.router)
app.include_router(auth_stub.router)
app.include_router(credits_route.router)
app.include_router(embed_route.router)
app.include_router(generate_route.router)

CONSULT_DIR = Path("/tmp/taey-ed-consult-v2")


def _not_implemented(endpoint: str, extra: dict | None = None) -> JSONResponse:
    payload = {
        "ok": False,
        "endpoint": endpoint,
        "message": f"{endpoint} is not implemented in spark_v2.",
    }
    if extra:
        payload.update(extra)
    return JSONResponse(status_code=501, content=payload)


@app.get("/health")
async def health() -> dict:
    return {
        "status": "healthy",
        "service": "spark_v2",
        "phase": "D-alpha+C7+wave1+wave2+wave4+wave5+final+v7port",
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
    return _not_implemented("/api/v1/action_review")
