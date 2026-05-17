"""Embedding proxy with deterministic 4096-d fallback for Mac compatibility."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()
logger = logging.getLogger(__name__)

UPSTREAM = os.environ.get("EMBEDDING_UPSTREAM", "http://10.0.0.68:8091/embed")
DIMENSION = 4096


def _stub_vectors(texts: list[str]) -> dict:
    return {
        "success": True,
        "embeddings": [[0.0] * DIMENSION for _ in texts],
        "model": "stub_zero_vector_fallback",
        "dimension": DIMENSION,
        "count": len(texts),
    }


def _validate_vectors(vectors: object, count: int) -> list[list[float]]:
    if not isinstance(vectors, list):
        raise ValueError("upstream embeddings is not a list")
    if len(vectors) != count:
        raise ValueError(f"upstream embeddings count mismatch: expected {count}, got {len(vectors)}")
    for index, vector in enumerate(vectors):
        if not isinstance(vector, list):
            raise ValueError(f"upstream embedding {index} is not a list")
        if len(vector) != DIMENSION:
            raise ValueError(f"upstream embedding {index} has dim {len(vector)} not {DIMENSION}")
    return vectors


def _proxy_embeddings(texts: list[str], payload: dict) -> dict:
    body = json.dumps({"texts": texts}).encode("utf-8")
    request = urllib.request.Request(
        UPSTREAM,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError(f"upstream status {response.status}")
        parsed = json.loads(response.read().decode("utf-8"))
    vectors = _validate_vectors(parsed.get("embeddings"), len(texts))
    return {
        "success": True,
        "embeddings": vectors,
        "model": str(payload.get("model") or parsed.get("model") or "Qwen/Qwen3-Embedding-8B"),
        "dimension": DIMENSION,
        "count": len(texts),
    }


@router.post("/api/v1/embed")
async def embed(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    texts = payload.get("texts")
    if not isinstance(texts, list):
        texts = []
    texts = [str(text) for text in texts]
    try:
        result = _proxy_embeddings(texts, payload if isinstance(payload, dict) else {})
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, RuntimeError) as exc:
        logger.warning("embed fallback to zero vectors: %s", exc)
        result = _stub_vectors(texts)
    return JSONResponse(status_code=200, content=result)
