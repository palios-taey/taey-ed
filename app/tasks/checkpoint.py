# STATUS: FROZEN - Proven in v7. Verified 2026-02-19. Do not modify.
"""
Checkpoint management for crash recovery.

P1.2: Save/load/clear checkpoints so continuous mode can resume
after crashes or restarts. Stores state in SQLite alongside content.
"""

import logging

from app.storage.sqlite_store import TaeyEdStorage

logger = logging.getLogger("taey-ed")


def save_checkpoint(
    platform: str,
    course_id: str,
    app_name: str,
    screens_completed: int,
    last_screen: str = "",
    last_action: str = "",
):
    """Save or update checkpoint after each successful screen."""
    try:
        storage = TaeyEdStorage(platform=platform, course_id=course_id)
        cursor = storage.conn.cursor()
        cursor.execute('''
            INSERT INTO checkpoints (platform, course_id, app_name,
                                     screens_completed, last_screen, last_action)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform, course_id, app_name)
            DO UPDATE SET
                screens_completed = excluded.screens_completed,
                last_screen = excluded.last_screen,
                last_action = excluded.last_action,
                updated_at = CURRENT_TIMESTAMP
        ''', (platform, course_id, app_name, screens_completed, last_screen, last_action))
        storage.conn.commit()
        storage.close()
        logger.debug(f"Checkpoint saved: {platform}/{course_id} screens={screens_completed}")
    except Exception as e:
        # Checkpoint failure must not crash the pipeline
        logger.error(f"Failed to save checkpoint: {e}")


def load_checkpoint(platform: str, course_id: str, app_name: str) -> dict:
    """
    Load checkpoint for a platform/course/app combination.
    Returns dict with screens_completed, last_screen, last_action, updated_at.
    Returns None if no checkpoint exists.
    """
    try:
        storage = TaeyEdStorage(platform=platform, course_id=course_id)
        cursor = storage.conn.cursor()
        cursor.execute('''
            SELECT screens_completed, last_screen, last_action, updated_at
            FROM checkpoints
            WHERE platform = ? AND course_id = ? AND app_name = ?
        ''', (platform, course_id, app_name))
        row = cursor.fetchone()
        storage.close()
        if row:
            return {
                "screens_completed": row["screens_completed"],
                "last_screen": row["last_screen"],
                "last_action": row["last_action"],
                "updated_at": row["updated_at"],
            }
        return None
    except Exception as e:
        logger.error(f"Failed to load checkpoint: {e}")
        return None


def clear_checkpoint(platform: str, course_id: str, app_name: str):
    """Clear checkpoint on clean stop (user clicked Stop)."""
    try:
        storage = TaeyEdStorage(platform=platform, course_id=course_id)
        cursor = storage.conn.cursor()
        cursor.execute('''
            DELETE FROM checkpoints
            WHERE platform = ? AND course_id = ? AND app_name = ?
        ''', (platform, course_id, app_name))
        storage.conn.commit()
        storage.close()
        logger.info(f"Checkpoint cleared: {platform}/{course_id}")
    except Exception as e:
        logger.error(f"Failed to clear checkpoint: {e}")
