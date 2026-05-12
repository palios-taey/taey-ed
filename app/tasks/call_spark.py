"""
HTTP client to call Spark API.

Auth: Bearer JWT (Authorization header) sourced from app.tasks.auth. On 401,
auto-refresh the access token once and retry. If refresh fails, raise — UI
will catch it and present the login screen.

A residual X-API-Key fallback is kept ONLY for transitional non-user endpoints
(e.g. /health). Per LAUNCH_PLAN §Gap B, user-facing endpoints MUST use Bearer.
"""

import logging
import httpx

from app.config import get_spark_url, get_api_key
from app.tasks.auth import bearer_header, refresh_access_token

logger = logging.getLogger("taey-ed")

# Connect timeout: 30s (fail fast if Spark unreachable)
# Read timeout: None (consultations/reviews take as long as Claude needs)
TIMEOUT = httpx.Timeout(connect=30.0, read=None, write=30.0, pool=30.0)


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

    response = _do()
    if response.status_code == 401 and bearer_header():
        # Try one refresh + retry; only meaningful if we were using Bearer.
        logger.info(f"call_spark {endpoint}: 401 — refreshing access token")
        if refresh_access_token():
            response = _do()

    response.raise_for_status()
    return response.json()


if __name__ == "__main__":
    # Test with mock tree
    result = call_spark("/match", {
        "platform": "acellus",
        "tree": {"children": [{"name": "Classes"}, {"value": "START"}]}
    })
    print(result)
