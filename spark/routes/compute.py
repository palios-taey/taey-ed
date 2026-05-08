# STATUS: FROZEN. Compute routes. Verified 2026-02-19. Do not modify.
"""Compute endpoints: VLM, embeddings, LLM answer generation."""

import logging

from fastapi import APIRouter, HTTPException

from spark.models import ExtractImageRequest, EmbedRequest, GenerateRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")


@router.post("/extract_image")
async def extract_image(request: ExtractImageRequest):
    """Extract text description from image using Gemini 2.5 Pro."""
    from spark.tasks.call_vision import extract_image_content
    result = await extract_image_content(
        image_b64=request.image_b64,
        purpose=request.purpose,
        context=request.context,
    )
    if not result.get("success"):
        raise HTTPException(status_code=500, detail=result.get("error"))
    return result


@router.post("/embed")
async def embed_text(request: EmbedRequest):
    """Generate embeddings using Qwen3-Embedding-8B."""
    from spark.tasks.call_embedding import get_embeddings
    result = await get_embeddings(request.texts)
    if not result.get("success"):
        raise HTTPException(status_code=500, detail=result.get("error"))
    return result


@router.post("/generate")
async def generate(request: GenerateRequest):
    """Generate answer for educational quiz question."""
    from spark.tasks.call_gemini import generate_answer

    if request.items:
        logger.info(f"generate: total items={len(request.items)}")
        for i, item in enumerate(request.items[:10]):
            logger.info(
                f"generate: item[{i}] keys={list(item.keys())} "
                f"full={str(item)[:200]}"
            )
        if len(request.items) > 10:
            logger.info(f"generate: ... ({len(request.items) - 10} more items)")
    logger.info(
        f"generate: q_type={request.question_type} "
        f"question='{request.question[:60]}'"
    )

    result = await generate_answer(
        question=request.question,
        question_type=request.question_type,
        options=request.options,
        context=request.context,
        image_descriptions=request.image_descriptions,
        has_text_field=request.has_text_field,
        screen_config=request.screen_config,
        items=request.items,
        screenshot_b64=request.screenshot_b64,
    )
    if not result.get("success"):
        raise HTTPException(status_code=500, detail=result.get("error"))
    return result
