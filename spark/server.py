# 2026-02-20: Added localhost bypass for /api/v1/consult* endpoints
"""
Taey-Ed API Server

Active routes:
  routes/health.py        — GET /health
  routes/next_action.py   — POST /next_action (state machine, V21)
  routes/consultation.py  — Consultation CRUD + /abandon_consultation
  routes/compute.py       — VLM, embeddings, LLM generation
  routes/review.py        — Action review endpoints
  routes/chat.py          — Mac UI chat history
"""

import logging
import logging.handlers
import os
import secrets

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

# Configure logging with rotation (5MB max, 3 backups = 20MB max)
_LOG_DIR = os.path.expanduser("~/taey-ed/logs")
os.makedirs(_LOG_DIR, exist_ok=True)
_LOG_FILE = os.path.join(_LOG_DIR, "spark_api.log")

_fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s: %(message)s",
                         datefmt="%H:%M:%S")

# File handler with rotation
_fh = logging.handlers.RotatingFileHandler(
    _LOG_FILE, maxBytes=5_000_000, backupCount=3)
_fh.setFormatter(_fmt)
_fh.setLevel(logging.INFO)

# Console handler (for nohup/journald)
_ch = logging.StreamHandler()
_ch.setFormatter(_fmt)
_ch.setLevel(logging.INFO)

logging.basicConfig(level=logging.INFO, handlers=[_fh, _ch])
logger = logging.getLogger(__name__)


# ── Authentication ──

API_KEY = os.environ.get(
    "TAEY_ED_API_KEY",
    "***REMOVED-INTERNAL-API-KEY***",
)
JWT_SECRET = os.environ.get("TAEY_ED_SECRET", "dev-secret-change-in-production")
JWT_ALGORITHM = "HS256"

import jwt as pyjwt


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Authenticate via API key (Mac) OR JWT Bearer token (web users)."""

    async def dispatch(self, request: Request, call_next):
        # Public endpoints + consultation respond (Spark Claude from localhost)
        if request.url.path in ("/health", "/screen-memory/stats"):
            return await call_next(request)
        if request.url.path.startswith("/api/v1/consult") and request.client.host == "127.0.0.1":
            request.state.user_id = "spark_claude"
            return await call_next(request)

        # Try API key first (Mac pipeline)
        key = request.headers.get("X-API-Key", "")
        if key and secrets.compare_digest(key, API_KEY):
            request.state.user_id = "mac_pipeline"
            return await call_next(request)

        # Try JWT Bearer token (web users)
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
            try:
                payload = pyjwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
                request.state.user_id = payload.get("sub", "unknown")
                return await call_next(request)
            except (pyjwt.ExpiredSignatureError, pyjwt.InvalidTokenError):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid or expired JWT token"},
                )

        return JSONResponse(
            status_code=401,
            content={"detail": "Missing API key or Bearer token"},
        )


# ── App Setup ──

app = FastAPI(title="Taey-Ed", version="1.0.0")
app.add_middleware(APIKeyMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://academy.taey.ai", "http://localhost:8080"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Log validation errors so we can diagnose 422s."""
    logger.error(
        f"Validation error on {request.method} {request.url.path}: {exc.errors()}"
    )
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


# ── Route Registration ──

from spark.routes.health import router as health_router
from spark.routes.next_action import router as next_action_router
from spark.routes.consultation import router as consultation_router
from spark.routes.compute import router as compute_router
from spark.routes.review import router as review_router
from spark.routes.chat import router as chat_router

app.include_router(health_router)
app.include_router(next_action_router)
app.include_router(consultation_router)
app.include_router(compute_router)
app.include_router(review_router)
app.include_router(chat_router)


# ── Entry Point ──

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5002)
