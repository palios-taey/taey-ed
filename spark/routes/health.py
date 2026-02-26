"""Health and stats endpoints."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health():
    return {"status": "healthy", "version": "8.1.0", "phase": 8}


@router.get("/screen-memory/stats")
def screen_memory_stats_endpoint():
    """Get screen signature stats."""
    from spark.tasks.screen_signatures import get_stats
    return get_stats()
