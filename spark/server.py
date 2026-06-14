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
from starlette.requests import ClientDisconnect

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


# Hard cap on request body bytes. /next_action carries AX tree (~50KB-500KB)
# + base64 screenshot (~1-3MB) + occasional bt_debug log; observed real max
# ~10MB. 25MB gives generous headroom for the largest pages without inviting
# abuse via giant payloads.
MAX_REQUEST_BODY_BYTES = 25 * 1024 * 1024


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests whose Content-Length exceeds MAX_REQUEST_BODY_BYTES.

    Cloudflare's edge already drops requests over 100MB; this middleware adds
    a tighter app-layer cap that matches actual request shapes, so a malformed
    or hostile client can't tie up uvicorn buffering a giant body before
    handlers run. Only checks Content-Length — requests without one (chunked
    streams) are not used by our Mac client.
    """

    async def dispatch(self, request: Request, call_next):
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) > MAX_REQUEST_BODY_BYTES:
                    return JSONResponse(
                        status_code=413,
                        content={
                            "detail": (
                                f"Request body exceeds {MAX_REQUEST_BODY_BYTES} "
                                "byte limit"
                            )
                        },
                    )
            except ValueError:
                pass
        return await call_next(request)


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
# Middleware order (outermost first by add order is the LAST to dispatch in
# starlette): we want size-limit to run BEFORE auth, so add APIKey first
# (innermost) and RequestSizeLimit second (outermost) — starlette wraps in
# reverse add order.
app.add_middleware(APIKeyMiddleware)
app.add_middleware(RequestSizeLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://app.taey.ai",        # consumer-facing site (signup/login/credits/billing UI)
        "https://academy.taey.ai",    # legacy academy front, retained for back-compat
        "http://localhost:8080",      # CCM local dev cycle
    ],
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


@app.middleware("http")
async def _dump_raw_failure_bodies(request: Request, call_next):
    """Mac-visibility middleware (Jesse 2026-05-19): capture the RAW request
    body for any /next_action call that carries a last_result with success=False.
    Dumps to /tmp/taey-ed-mac-raw-dumps/ so we can see EXACTLY what Mac sent
    on the wire — including fields that Pydantic would drop (e.g. if Mac is
    sending bt_debug_tail under a different name, or sending it on a sub-object,
    or sending it but empty).
    """
    if request.url.path == "/next_action" and request.method == "POST":
        try:
            body = await request.body()
            # Quick peek to see if last_result.success is False
            import json as _json
            try:
                parsed = _json.loads(body) if body else {}
                lr = parsed.get("last_result") or {}
                # Dump on ANY last_result (success or failure) so we can verify
                # bt_debug_tail flows on every BT execution post-fix.
                if lr:
                    from pathlib import Path
                    import time
                    dump_dir = Path("/tmp/taey-ed-mac-raw-dumps")
                    dump_dir.mkdir(parents=True, exist_ok=True)
                    ts = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
                    # Don't dump the screenshot_b64 — it's huge.
                    sanitized = dict(parsed)
                    if sanitized.get("screenshot_b64"):
                        sanitized["screenshot_b64"] = f"<{len(parsed['screenshot_b64'])} bytes redacted>"
                    if sanitized.get("tree"):
                        sanitized["tree"] = "<tree redacted for brevity>"
                    dump_path = dump_dir / f"raw_{ts}.json"
                    dump_path.write_text(_json.dumps(sanitized, indent=2, default=str))
                    # Log all field names on last_result so we can see what Mac sent
                    logger.warning(
                        f"MAC RAW FAILURE BODY → {dump_path}; "
                        f"last_result keys: {sorted(lr.keys())}; "
                        f"bt_debug_tail in body: {'bt_debug_tail' in lr}, "
                        f"value type/len: {type(lr.get('bt_debug_tail')).__name__}/"
                        f"{len(lr.get('bt_debug_tail') or '') if isinstance(lr.get('bt_debug_tail'), str) else 'N/A'}"
                    )
            except Exception:
                pass

            # Restore body so downstream can read it
            async def receive():
                return {"type": "http.request", "body": body}
            request._receive = receive
        except ClientDisconnect:
            # Expected: the Mac aborted mid-request on a flaky tunnel (the SSL /
            # 'context canceled' class). Not an error — log a clean one-liner, no
            # traceback. The request is already dead; nothing to dump or restore.
            logger.info("raw-dump: client disconnected before body received (tunnel flake) — skipped")
        except Exception:
            logger.exception("raw-dump middleware failed (non-fatal)")
    return await call_next(request)


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
