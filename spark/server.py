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

from spark.secrets_loader import (
    get_taey_ed_secrets,
    validate_for_production,
    is_production,
)

# Eager production-secret validation. Raises SecretsError before app accepts
# any request if TAEY_ED_PRODUCTION=1 and required secrets are missing/weak.
validate_for_production()

_TAEY_ED_SECRETS = get_taey_ed_secrets()
JWT_SECRET = _TAEY_ED_SECRETS.jwt_secret
JWT_ALGORITHM = "HS256"

# Internal API key path is localhost/dev-only. Never accepted from non-loopback
# clients. In production, the secrets loader may yield None and the X-API-Key
# branch simply rejects all callers (correct behavior).
INTERNAL_API_KEY = _TAEY_ED_SECRETS.internal_api_key

logger.info(
    f"Auth initialized: mode={'PRODUCTION' if is_production() else 'DEV'}, "
    f"internal_api_key={'set' if INTERNAL_API_KEY else 'not-set'}"
)

import jwt as pyjwt


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Authenticate via JWT Bearer (user-facing) or loopback-scoped internal
    API key (dev / server-to-server only).

    The X-API-Key branch is restricted to loopback callers; it must never be
    accepted from the public internet. User-billable endpoints require a
    Bearer JWT tied to a real user_id.
    """

    async def dispatch(self, request: Request, call_next):
        # Public endpoints that bypass auth.
        if request.url.path in (
            "/health",
            "/screen-memory/stats",
            # Auth endpoints: signup, login, refresh must work pre-auth.
            # /auth/me requires auth (handled below).
            "/auth/signup",
            "/auth/login",
            "/auth/refresh",
            "/auth/logout",  # client may have lost access token but still wants to revoke refresh
            "/auth/verify-email",
            # Stripe webhook is public; route handler verifies the
            # webhook signature for authenticity.
            "/billing/webhook",
        ):
            return await call_next(request)
        if request.url.path.startswith("/api/v1/consult") and request.client.host == "127.0.0.1":
            request.state.user_id = "spark_claude"
            return await call_next(request)

        # Internal API key:
        #   - PRODUCTION: loopback only (Mac/web must use JWT).
        #   - DEV: any client (preserves Jesse's LAN dev workflow until Mac
        #          switches to Bearer JWT in Phase 4).
        # With no internal_api_key configured at all, this branch is unreachable.
        key = request.headers.get("X-API-Key", "")
        if INTERNAL_API_KEY and key and secrets.compare_digest(key, INTERNAL_API_KEY):
            if is_production() and request.client.host not in ("127.0.0.1", "::1", "localhost"):
                # Production: X-API-Key from non-loopback is rejected.
                pass
            else:
                request.state.user_id = "internal"
                return await call_next(request)

        # JWT Bearer token (the real user-facing auth path)
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
            content={"detail": "Authentication required"},
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


# ── DB Initialization ──

from spark.db import init_db
init_db()


# ── Route Registration ──

from spark.routes.health import router as health_router
from spark.routes.next_action import router as next_action_router
from spark.routes.consultation import router as consultation_router
from spark.routes.compute import router as compute_router
from spark.routes.review import router as review_router
from spark.routes.chat import router as chat_router
from spark.routes.auth import router as auth_router
from spark.routes.credits import router as credits_router
from spark.routes.billing import router as billing_router

app.include_router(health_router)
app.include_router(next_action_router)
app.include_router(consultation_router)
app.include_router(compute_router)
app.include_router(review_router)
app.include_router(chat_router)
app.include_router(auth_router)
app.include_router(credits_router)
app.include_router(billing_router)


# ── Entry Point ──

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5002)
