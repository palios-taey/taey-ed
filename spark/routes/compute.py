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

    try:
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
    except Exception as e:
        # Genuine server crash inside the LLM call path. Log full traceback so
        # the cause is visible in /home/user/taey-ed/logs/api.log, then 500.
        logger.exception(
            f"generate_answer crashed: q_type={request.question_type} "
            f"question={request.question[:80]!r}"
        )
        raise HTTPException(
            status_code=500,
            detail=f"generate_answer crashed: {type(e).__name__}: {e}",
        )

    if not result.get("success"):
        # Client-input problem (empty options, malformed request, etc.) — Mac
        # extracted bad data and called us with it. Return 422 (Unprocessable
        # Entity), NOT 500. Previously we returned 500 here, which made every
        # Mac-side BT failure handler treat it as "server died" and triggered
        # the loop guard → escalation cascade. The actual fix is upstream
        # (better extract_question filtering, or worker generating a BT
        # appropriate for the screen variant), but returning the correct
        # status code stops the false-positive cascade.
        err = result.get("error", "unknown")
        logger.warning(
            f"generate_answer returned !success: q_type={request.question_type} "
            f"err={err!r} question={request.question[:80]!r}"
        )
        raise HTTPException(status_code=422, detail=err)
    return result
