"""Health and stats endpoints."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health():
    return {"status": "healthy", "version": "8.1.0", "phase": "8+verified-bt-bypass"}


@router.get("/screen-memory/stats")
def screen_memory_stats_endpoint():
    """Get variant cache stats (V21)."""
    from spark.tasks.variant_cache import get_stats
    return get_stats()
