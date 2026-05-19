"""
HTTP client to call Spark API.

Auth: Bearer JWT (Authorization header) sourced from app.tasks.auth. On 401,
auto-refresh the access token once and retry. If refresh fails, raise — UI
will catch it and present the login screen.

A residual X-API-Key fallback is kept ONLY for transitional non-user endpoints
(e.g. /health). Per LAUNCH_PLAN §Gap B, user-facing endpoints MUST use Bearer.
"""

import logging
import time
import httpx

from app.config import get_spark_url, get_api_key
from app.tasks.auth import bearer_header, refresh_access_token

logger = logging.getLogger("taey-ed")

# Connect timeout: 30s (fail fast if Spark unreachable)
# Read timeout: None (consultations/reviews take as long as Claude needs)
TIMEOUT = httpx.Timeout(connect=30.0, read=None, write=30.0, pool=30.0)

# Retry on transient TLS / network errors. send_to_llm POSTs a screenshot
# (~500KB) which intermittently fails mid-TLS-handshake against the
# Cloudflare-Tunnel-fronted backend with _ssl.c:2580 ReadError. A small
# bounded retry with backoff masks these flakes without papering over
# real server failures (HTTPStatusError still raises immediately).
# Jesse defect 2026-05-19 23:19 — two consecutive send_to_llm hits.
_TRANSIENT_ERRORS = (
    httpx.ReadError,
    httpx.ConnectError,
    httpx.RemoteProtocolError,
    httpx.WriteError,
)
_MAX_TRANSIENT_RETRIES = 3
_BACKOFF_BASE_SECONDS = 0.5


def _auth_headers() -> dict:
    """Return Bearer header if we have a session. Falls back to X-API-Key only
    if explicitly configured (transitional dev support; no UI surface for it)."""
    h = bearer_header()
    if h:
        return h
    api_key = get_api_key()
    if api_key:
        return {"X-API-Key": api_key}
    return {}


def call_spark(endpoint: str, payload: dict = None, method: str = "POST") -> dict:
    """
    Call Spark API endpoint with Bearer JWT auth + 401-refresh-retry.

    Args:
        endpoint: API endpoint (e.g., "/next_action", "/auth/me")
        payload: JSON payload dict (for POST)
        method: HTTP method ("POST" or "GET")

    Returns:
        Response as dict

    Raises:
        httpx.HTTPStatusError on any non-2xx after refresh-retry exhausted.
        httpx.HTTPError on network issues.
    """
    url = f"{get_spark_url()}{endpoint}"
    method_u = method.upper()

    def _do() -> httpx.Response:
        headers = _auth_headers()
        if method_u == "GET":
            return httpx.get(url, headers=headers, timeout=TIMEOUT)
        return httpx.post(url, json=payload, headers=headers, timeout=TIMEOUT)

    def _attempt_with_401_refresh() -> httpx.Response:
        r = _do()
        if r.status_code == 401 and bearer_header():
            logger.info(f"call_spark {endpoint}: 401 — refreshing access token")
            if refresh_access_token():
                r = _do()
        return r

    last_exc = None
    for attempt in range(_MAX_TRANSIENT_RETRIES):
        try:
            response = _attempt_with_401_refresh()
            response.raise_for_status()
            return response.json()
        except _TRANSIENT_ERRORS as e:
            last_exc = e
            if attempt == _MAX_TRANSIENT_RETRIES - 1:
                logger.error(
                    f"call_spark {endpoint}: transient {type(e).__name__} "
                    f"after {_MAX_TRANSIENT_RETRIES} attempts: {e!r}"
                )
                raise
            backoff = _BACKOFF_BASE_SECONDS * (2 ** attempt)
            logger.warning(
                f"call_spark {endpoint}: transient {type(e).__name__}: {e!r} — "
                f"retry {attempt + 1}/{_MAX_TRANSIENT_RETRIES} in {backoff:.1f}s"
            )
            time.sleep(backoff)
    # Unreachable — loop either returns or raises.
    raise last_exc if last_exc else RuntimeError("call_spark: retry loop fell through")


if __name__ == "__main__":
    # Test with mock tree
    result = call_spark("/match", {
        "platform": "acellus",
        "tree": {"children": [{"name": "Classes"}, {"value": "START"}]}
    })
    print(result)
