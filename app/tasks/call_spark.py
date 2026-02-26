"""
HTTP client to call Spark API.
Single function. No fallbacks. Supports GET and POST.
P0.4: API key auth via X-API-Key header.

Config precedence: env vars > ~/.taey-ed/config.json > defaults
See app/config.py for details.
"""

import httpx
from app.config import get_spark_url, get_api_key

# Connect timeout: 30s (fail fast if Spark unreachable)
# Read timeout: None (consultations/reviews take as long as Spark Claude needs)
TIMEOUT = httpx.Timeout(connect=30.0, read=None, write=30.0, pool=30.0)


def _headers() -> dict:
    """Build request headers. Includes API key if configured."""
    h = {}
    api_key = get_api_key()
    if api_key:
        h["X-API-Key"] = api_key
    return h


def call_spark(endpoint: str, payload: dict = None, method: str = "POST") -> dict:
    """
    Call Spark API endpoint.

    Args:
        endpoint: API endpoint (e.g., "/match", "/consult/abc123")
        payload: JSON payload dict (for POST)
        method: HTTP method ("POST" or "GET")

    Returns:
        Response as dict

    Raises:
        Exception on any failure (no silent errors)
    """
    url = f"{get_spark_url()}{endpoint}"
    headers = _headers()

    if method.upper() == "GET":
        response = httpx.get(url, headers=headers, timeout=TIMEOUT)
    else:
        response = httpx.post(url, json=payload, headers=headers, timeout=TIMEOUT)

    response.raise_for_status()
    return response.json()


if __name__ == "__main__":
    # Test with mock tree
    result = call_spark("/match", {
        "platform": "acellus",
        "tree": {"children": [{"name": "Classes"}, {"value": "START"}]}
    })
    print(result)
