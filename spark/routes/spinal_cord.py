# STATUS: FROZEN - V8 spinal cord routes. Verified 2026-02-19. Do not modify.
"""Spinal cord endpoints: embedding-based screen routing and post-action collapse."""

import logging

from fastapi import APIRouter

from spark.models import RouteRequest, CollapseRequest, MatchRequest
from spark.tasks.screen_router import route_screen
from spark.tasks.screen_collapse import collapse as collapse_screen
from spark.tasks.load_yaml import load_yaml
from spark.tasks.match_screen import match_screen

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/match")
def match(request: MatchRequest):
    """Match accessibility tree against YAML config. Legacy — use /next_action."""
    config = load_yaml(request.platform)
    return match_screen(request.tree, config)


@router.post("/route")
def route(request: RouteRequest):
    """Route screen through the spinal cord."""
    result = route_screen(
        tree=request.tree,
        platform=request.platform,
        viewport_height=request.viewport_height,
    )
    return {
        "category": result.category,
        "behavior_tree": result.behavior_tree,
        "dynamic_text": result.dynamic_text,
        "skeleton_hash": result.skeleton_hash,
        "distance": result.distance,
        "embedding": result.embedding,
        "success_count": result.match_data.get("success_count", 0) if result.match_data else 0,
    }


@router.post("/collapse")
def collapse(request: CollapseRequest):
    """Post-action collapse: if screen changed, store successful BT in Weaviate."""
    return collapse_screen(
        tree_before=request.tree_before,
        tree_after=request.tree_after,
        embedding=request.embedding,
        behavior_tree=request.behavior_tree,
        platform=request.platform,
        skeleton_text=request.skeleton_text,
        skeleton_hash_val=request.skeleton_hash,
    )
