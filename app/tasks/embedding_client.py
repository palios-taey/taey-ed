"""
Embedding client — Mac-side caller for the central Qwen3 embedding service.

Calls Mira's `POST /api/v1/embed` (the same endpoint that lives behind
the Cloudflare Tunnel at https://taey-ed-api.taey.ai/api/v1/embed).
Bearer JWT auth via app.tasks.auth. On 401, refreshes once and retries.

Constitutional rule (per dispatch 2026-05-12, Jesse): no truncation.
Native dim is 4096 (Qwen3-Embedding-8B). If the response shape ever
disagrees with the configured EMBEDDING_DIMENSION, we raise — never
trim. Configuration changes go through the env, not the client.

Ported from /home/user/taey-ed-v4/app/agent/embedding_client.py with:
  - sync httpx instead of aiohttp (matches the rest of the Mac code)
  - Bearer auth via app.tasks.auth, not shared X-API-Key
  - Single retry on 401 after refresh
  - Endpoint pointed at central Mira service via app.config
"""

import logging
import os
from typing import List, Optional

import httpx

from app.tasks.auth import bearer_header, refresh_access_token

logger = logging.getLogger("taey-ed")

# Native dimension. Per dispatch + LAUNCH_PLAN: 4096 native Qwen3.
# If the server ever returns a different size, raise — never silently truncate.
EMBEDDING_DIMENSION = int(os.environ.get("EMBEDDING_DIMENSION", "4096"))

# Embedding-service path on the central server. The host comes from
# app.config.get_spark_url(); we just suffix the path.
EMBEDDING_PATH = os.environ.get("EMBEDDING_PATH", "/api/v1/embed")

_TIMEOUT = httpx.Timeout(connect=15.0, read=60.0, write=30.0, pool=15.0)


class EmbeddingError(RuntimeError):
    """Anything went wrong talking to /api/v1/embed."""


def _endpoint() -> str:
    """Resolve full embedding URL from current Spark base."""
    from app.config import get_spark_url
    return f"{get_spark_url().rstrip('/')}{EMBEDDING_PATH}"


def _post_with_auth_retry(payload: dict) -> dict:
    """POST with Bearer; on 401 refresh once and retry.
    Raises EmbeddingError on any non-2xx final response or network issue."""
    url = _endpoint()

    def _do_request() -> httpx.Response:
        headers = bearer_header()
        if not headers:
            raise EmbeddingError("Not logged in — cannot call embedding service.")
        return httpx.post(url, json=payload, headers=headers, timeout=_TIMEOUT)

    try:
        r = _do_request()
        if r.status_code == 401:
            logger.info("embed: 401 — refreshing access token and retrying once")
            if not refresh_access_token():
                raise EmbeddingError(
                    "Embedding call returned 401 and refresh failed. User must sign in again."
                )
            r = _do_request()
        if r.status_code != 200:
            raise EmbeddingError(
                f"Embedding API HTTP {r.status_code}: {r.text[:300]}"
            )
        return r.json()
    except httpx.HTTPError as e:
        raise EmbeddingError(f"Embedding network error: {e}") from e


def embed(texts: List[str]) -> List[List[float]]:
    """Embed a batch of texts. Returns list of vectors, same order as input.

    Raises:
      EmbeddingError: any failure (no fallback, no truncation).
      ValueError:     empty input list.
    """
    if not texts:
        raise ValueError("embed(): texts list cannot be empty")
    if not all(isinstance(t, str) for t in texts):
        raise ValueError("embed(): all texts must be str")

    payload = {
        "texts": texts,
        "model": "Qwen/Qwen3-Embedding-8B",
        "encoding_format": "float",
        "dimensions": EMBEDDING_DIMENSION,
    }

    data = _post_with_auth_retry(payload)

    # Mira's response: {success, embeddings, model, dimension, count}
    if "error" in data:
        raise EmbeddingError(f"Embedding API error payload: {data['error']}")
    if "embeddings" not in data:
        raise EmbeddingError(f"Embedding response missing 'embeddings' key. Keys: {list(data.keys())}")

    vectors = data["embeddings"]
    if not isinstance(vectors, list):
        raise EmbeddingError(f"Embedding response 'embeddings' is {type(vectors).__name__}, expected list.")
    if len(vectors) != len(texts):
        raise EmbeddingError(
            f"Embedding count mismatch: sent {len(texts)} texts, got {len(vectors)} vectors."
        )

    for i, vec in enumerate(vectors):
        if vec is None:
            raise EmbeddingError(f"Embedding response item {i} is None.")
        # Native-dim invariant. NEVER truncate — fail loud.
        if len(vec) != EMBEDDING_DIMENSION:
            raise EmbeddingError(
                f"Embedding dim mismatch on item {i}: expected {EMBEDDING_DIMENSION}, "
                f"got {len(vec)}. Refusing to truncate — set EMBEDDING_DIMENSION env "
                f"or fix the server config."
            )
    return vectors


def embed_one(text: str) -> List[float]:
    """Embed a single string. Returns the vector."""
    return embed([text])[0]
