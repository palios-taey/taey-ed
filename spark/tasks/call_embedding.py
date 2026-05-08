# STATUS: FROZEN. Verified 2026-02-19. Do not modify.
"""
Qwen3-Embedding-8B for content embeddings.

Phase 5: Generate embeddings for LightRAG storage on Mac.
Spark provides COMPUTE only - Mac stores results locally.

LOCKED FILE - Do not modify without Jesse's approval.
This file defines the embedding contract between Spark and Mac.
Changes break DeepTutor compatibility.
"""

import httpx
from typing import List, Union

# =============================================================================
# LOCKED CONSTANTS - These define the API contract with Mac/DeepTutor
# DO NOT CHANGE without updating all consumers (Mac LightRAG, DeepTutor)
# =============================================================================
EMBED_URL = "http://127.0.0.1:8089"  # Mira-local Qwen3-Embedding (was NCCL nginx LB)
MODEL = "Qwen/Qwen3-Embedding-8B"          # LOCKED: Model name
EMBEDDING_DIM = 3072                        # LOCKED: MRL dimension (DeepTutor compatible)


async def get_embeddings(
    texts: Union[str, List[str]]
) -> dict:
    """
    Generate embeddings for text content.

    Args:
        texts: Single string or list of strings to embed

    Returns:
        {
            "success": True,
            "embeddings": [[...], [...], ...],  # List of 3072-dim vectors
            "model": "Qwen/Qwen3-Embedding-8B",
            "dimension": 3072
        }
    """
    # Normalize to list
    if isinstance(texts, str):
        texts = [texts]

    if not texts:
        return {
            "success": False,
            "error": "No texts provided",
            "embeddings": [],
            "model": MODEL,
            "dimension": EMBEDDING_DIM
        }

    # OpenAI-compatible endpoint with MRL dimensions parameter
    payload = {
        "input": texts,
        "model": MODEL,
        "dimensions": EMBEDDING_DIM  # Request specific dimension via MRL
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{EMBED_URL}/v1/embeddings",
                json=payload
            )
            response.raise_for_status()
            data = response.json()

            # Extract embeddings in order
            embeddings = []
            for item in sorted(data.get("data", []), key=lambda x: x.get("index", 0)):
                embeddings.append(item.get("embedding", []))

            return {
                "success": True,
                "embeddings": embeddings,
                "model": MODEL,
                "dimension": EMBEDDING_DIM,
                "count": len(embeddings)
            }

    except httpx.TimeoutException:
        return {
            "success": False,
            "error": "Embedding service timeout (30s)",
            "embeddings": [],
            "model": MODEL,
            "dimension": EMBEDDING_DIM
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "embeddings": [],
            "model": MODEL,
            "dimension": EMBEDDING_DIM
        }


async def get_single_embedding(text: str) -> dict:
    """
    Convenience wrapper for single text embedding.

    Returns:
        {
            "success": True,
            "embedding": [...],  # Single 3072-dim vector
            "model": "...",
            "dimension": 3072
        }
    """
    result = await get_embeddings([text])

    if result["success"] and result["embeddings"]:
        return {
            "success": True,
            "embedding": result["embeddings"][0],
            "model": result["model"],
            "dimension": result["dimension"]
        }
    else:
        return {
            "success": False,
            "error": result.get("error", "No embedding returned"),
            "embedding": [],
            "model": MODEL,
            "dimension": EMBEDDING_DIM
        }
