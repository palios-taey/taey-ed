# STATUS: contract for Mac-side DeepTutor. Updated 2026-05-12 — native 4096-dim.
"""
Qwen3-Embedding-8B for content embeddings.

Mira provides the embedding endpoint; the Mac stores results locally in
DeepTutor (KB content + vectors live on the user's machine — see CLAUDE.md
"Hard architectural line"). Per Jesse 2026-05-12: NO truncation, ever.

The upstream service at 127.0.0.1:8089 emits the model's native 4096-dim
vectors and ignores OpenAI-style MRL `dimensions` requests. DeepTutor's
embedding-dim is fully configurable via EMBEDDING_DIMENSION env var (its
FAISS indexer is data-driven via IndexFlatIP(embeddings.shape[1])), so
shipping 4096 natively requires no client-side adaptation — the Mac just
templates DeepTutor's .env with EMBEDDING_DIMENSION=4096.
"""

import httpx
from typing import List, Union

# =============================================================================
# LOCKED CONSTANTS — Mac/DeepTutor bundle must template DeepTutor's .env to
# match (EMBEDDING_DIMENSION=4096, EMBEDDING_MODEL=Qwen/Qwen3-Embedding-8B).
# Vectors are emitted native-shape; no truncation, no MRL adaptation.
# =============================================================================
EMBED_URL = "http://127.0.0.1:8089"         # Mira-local Qwen3-Embedding service
MODEL = "Qwen/Qwen3-Embedding-8B"           # LOCKED: model id sent in API payload
EMBEDDING_DIM = 4096                        # LOCKED: native model dim (was 3072 — wrong; service ignored MRL)


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
            "embeddings": [[...], [...], ...],  # List of 4096-dim vectors
            "model": "Qwen/Qwen3-Embedding-8B",
            "dimension": 4096
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

    # OpenAI-compatible endpoint. Note: this upstream build ignores the
    # `dimensions` MRL parameter and always returns the native 4096-dim
    # vector; we send the value for documentation but rely on native dim.
    payload = {
        "input": texts,
        "model": MODEL,
        "dimensions": EMBEDDING_DIM,
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
            "embedding": [...],  # Single 4096-dim vector
            "model": "...",
            "dimension": 4096
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
