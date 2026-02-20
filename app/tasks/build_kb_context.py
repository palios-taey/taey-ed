# STATUS: FROZEN - Proven in v7. Verified 2026-02-19. Do not modify.
"""
Build knowledge base context for quiz answer generation.
Searches SQLite for relevant content from recent lessons.

Phase 6 task file.
"""

import logging

from app.storage.sqlite_store import TaeyEdStorage

logger = logging.getLogger("taey-ed")


def build_kb_context(platform: str, course_id: str, question_text: str) -> list:
    """
    Build knowledge base context for answer generation.
    Searches SQLite for relevant content from recent lessons.

    Args:
        platform: Platform name
        course_id: Course identifier
        question_text: The question being asked

    Returns:
        List of context strings (most relevant first)
    """
    storage = TaeyEdStorage(platform=platform, course_id=course_id)
    context_texts = []

    # Strategy 1: Search for content matching question keywords
    skip_words = {"the", "a", "an", "is", "are", "was", "were", "what", "which",
                  "how", "does", "do", "of", "in", "to", "for", "and", "or", "that",
                  "this", "with", "from", "on", "at", "by", "it", "its", "be", "as"}
    words = [w.strip("?.,!") for w in question_text.lower().split()
             if w.strip("?.,!") not in skip_words and len(w.strip("?.,!")) > 2]

    seen = set()
    for word in words[:5]:
        results = storage.search_content(platform, course_id, word, limit=3)
        for r in results:
            for text in r.get("texts", []):
                if text not in seen:
                    context_texts.append(text)
                    seen.add(text)

    # Strategy 2: Get recent content as fallback
    if len(context_texts) < 3:
        recent = storage.get_recent_content(platform, course_id, limit=5)
        for r in recent:
            for text in r.get("texts", []):
                if text not in seen:
                    context_texts.append(text)
                    seen.add(text)
            for img in r.get("images", []):
                desc = img.get("description", "")
                if desc and desc not in seen:
                    context_texts.append(desc)
                    seen.add(desc)

    storage.close()
    return context_texts[:20]
