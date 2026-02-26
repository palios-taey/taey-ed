"""
Extract content from screen based on YAML extract: config and store in SQLite.

Phase 5 task file.
"""

import logging

from app.tasks.call_spark import call_spark
from app.tasks.extract_text import extract_text
from app.tasks.crop_image import crop_image_region, find_element_bbox
from app.storage.sqlite_store import TaeyEdStorage

logger = logging.getLogger("taey-ed")


def handle_extraction(
    platform: str,
    course_id: str,
    tree: dict,
    screenshot_b64: str,
    extract_config: dict,
    screen_type: str,
    lesson: str = ""
) -> dict:
    """
    Extract content based on YAML extract: section and store locally.

    Args:
        platform: Platform name (e.g., "acellus")
        course_id: Course identifier (e.g., "intro_banking")
        tree: Accessibility tree from capture_tree
        screenshot_b64: Screenshot as base64
        extract_config: YAML extract section
        screen_type: Screen type from YAML match
        lesson: Lesson name (optional)

    Returns:
        dict with extraction result
    """
    logger.info(f"Extracting content from {screen_type}...")

    extracted_texts = []
    extracted_images = []

    # Extract text based on YAML criteria
    text_config = extract_config.get("text")
    if text_config:
        logger.info(f"Extracting text with {len(text_config)} criteria...")
        extracted_texts = extract_text(tree, text_config)
        logger.info(f"Extracted {len(extracted_texts)} text items")

    # Extract images based on YAML image config
    # Supports:
    #   - Direct bbox: {"bbox": [x, y, w, h], "purpose": "..."}
    #   - Element-based: {"role": "AXImage", "name": "", "purpose": "..."} - finds bbox from tree
    #   - Full window: {"source": "window"} - sends entire screenshot
    image_config = extract_config.get("images")
    if image_config and screenshot_b64:
        logger.info(f"Extracting {len(image_config)} image regions...")
        for img_spec in image_config:
            bbox = img_spec.get("bbox")
            purpose = img_spec.get("purpose", "image")
            source = img_spec.get("source")

            # Full window screenshot: send entire image without cropping
            if source == "window":
                logger.info("Using full window screenshot for VLM")
                try:
                    vlm_result = call_spark("/api/v1/extract_image", {
                        "image_b64": screenshot_b64,
                        "purpose": purpose
                    })
                    description = vlm_result.get("description", "")
                    extracted_images.append({
                        "description": description,
                        "purpose": purpose,
                        "source": "window"
                    })
                    logger.info(f"Extracted image [window]: {description[:80]}...")
                except Exception as e:
                    logger.error(f"Failed to extract window image: {e}")
                continue

            # Element-based lookup: find bbox from tree by role/name
            if not bbox and img_spec.get("role"):
                element_role = img_spec.get("role")
                element_name = img_spec.get("name")
                logger.info(f"Looking up element bbox: role={element_role}, name={element_name}")
                bbox = find_element_bbox(tree, role=element_role, name=element_name)
                if bbox:
                    logger.info(f"Found element bbox: {bbox}")
                else:
                    logger.warning(f"Element not found in tree: role={element_role}, name={element_name}")
                    continue

            if not bbox:
                logger.warning(f"Image spec missing bbox and no element found: {img_spec}")
                continue

            try:
                cropped_b64 = crop_image_region(screenshot_b64, bbox)
                vlm_result = call_spark("/api/v1/extract_image", {
                    "image_b64": cropped_b64,
                    "purpose": purpose
                })
                description = vlm_result.get("description", "")
                extracted_images.append({
                    "description": description,
                    "purpose": purpose,
                    "bbox": bbox
                })
                logger.info(f"Extracted image [{purpose}]: {description[:50]}...")
            except Exception as e:
                logger.error(f"Failed to extract image region {bbox}: {e}")

    # Skip storage if nothing extracted
    if not extracted_texts and not extracted_images:
        logger.info("No content extracted, skipping storage")
        return {"extracted": False, "reason": "no_content"}

    # Get embeddings for the extracted content
    embeddings = []
    if extracted_texts:
        try:
            combined_text = " ".join(extracted_texts)
            embed_result = call_spark("/api/v1/embed", {"texts": [combined_text]})
            embeddings = embed_result.get("embeddings", [[]])[0]
            logger.info(f"Got {len(embeddings)}-dim embeddings")
        except Exception as e:
            logger.error(f"Failed to get embeddings: {e}")

    # Store in SQLite database
    try:
        storage = TaeyEdStorage(platform=platform, course_id=course_id)
        content_id = storage.store_content(
            platform=platform,
            course_id=course_id,
            screen_type=screen_type,
            texts=extracted_texts,
            images=extracted_images,
            embeddings=embeddings,
            lesson=lesson
        )
        logger.info(f"Stored content {content_id} in SQLite")
    except Exception as e:
        logger.error(f"Failed to store in SQLite: {e}")
        return {
            "extracted": False,
            "reason": f"storage_failed: {e}",
            "text_count": len(extracted_texts),
            "image_count": len(extracted_images),
        }

    return {
        "extracted": True,
        "text_count": len(extracted_texts),
        "image_count": len(extracted_images),
        "has_embeddings": len(embeddings) > 0
    }
