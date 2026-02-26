"""
Store question-answer pair in SQLite.

Phase 6 task file.
"""

import logging

from app.storage.sqlite_store import TaeyEdStorage

logger = logging.getLogger("taey-ed")


def store_qa(platform: str, course_id: str, question: str, answer: str, q_type: str):
    """Store question-answer pair in SQLite."""
    try:
        storage = TaeyEdStorage(platform=platform, course_id=course_id)
        storage.store_qa_pair(
            platform=platform,
            course_id=course_id,
            question=question,
            answer=answer,
            question_type=q_type,
        )
        storage.close()
        logger.info(f"Stored Q&A pair: {question[:50]}... -> {answer[:50]}...")
    except Exception as e:
        logger.error(f"Failed to store Q&A pair: {e}")
