# STATUS: FROZEN - V8 health routes. Verified 2026-02-19. Do not modify.
"""Health and stats endpoints."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health():
    return {"status": "healthy", "version": "8.0.0", "phase": 8}


@router.get("/screen-memory/stats")
def screen_memory_stats_endpoint():
    """Get screen memory collection stats."""
    from spark.tasks.screen_memory import get_stats
    return get_stats()
