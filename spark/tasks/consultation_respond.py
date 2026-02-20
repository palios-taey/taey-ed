"""
Consultation response handling.

V10: Now embeds screen into Weaviate ScreenEmbedding after response.
Every consultation response teaches the vector store for future recognition.

Post-V8 fix (2026-02-20):
  - Updates metadata.json status to "complete" after writing response.json
    (fixes 1-at-a-time deadlock where new consultations were blocked forever)
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from .atomic_write import atomic_write_json
from .consultation_state import get_consultation_state

logger = logging.getLogger(__name__)

CONSULT_DIR = Path("/tmp/taey-ed-consult")


def _embed_screen_to_weaviate(
    consultation_id: str,
    screen_type: str,
    tree: dict,
    consult_path: Path,
    expected_next: list = None,
):
    """
    Embed the consultation's accessibility tree into Weaviate ScreenEmbedding.

    Stores as PROVISIONAL (validated=False). The BT is not yet proven by Mac.
    Promotion to validated=True happens in /next_action after Mac executes
    the BT and Spark confirms the screen transitioned correctly.

    Non-fatal — failure here doesn't block the consultation response.
    """
    try:
        tree_file = consult_path / "tree.json"
        if not tree_file.exists():
            logger.warning(f"No tree.json for {consultation_id}, skipping embed")
            return

        with open(tree_file) as f:
            ax_tree = json.load(f)

        # Read platform from metadata
        meta_file = consult_path / "metadata.json"
        platform = "unknown"
        if meta_file.exists():
            with open(meta_file) as f:
                meta = json.load(f)
                platform = meta.get("platform", "unknown")

        from .skeleton import extract_skeleton, skeleton_hash
        from .screen_memory import embed_text, store_screen, get_client

        # Layer 1: Extract skeleton (structure only, no content)
        skel = extract_skeleton(ax_tree)
        shash = skeleton_hash(skel)

        # Layer 2: Embed skeleton
        vec = embed_text(skel)

        # Layer 3: Store in Weaviate — PROVISIONAL (validated=False)
        bt = tree if tree else {}
        en_json = json.dumps(expected_next) if expected_next else "[]"
        client = get_client()
        try:
            store_screen(
                vector=vec,
                skeleton_hash=shash,
                platform=platform,
                behavior_tree=bt,
                skeleton_text=skel,
                screen_type=screen_type,
                client=client,
                validated=False,
                expected_next=en_json,
                source="consultation",
            )
        finally:
            client.close()

        logger.info(
            f"Embedded screen (PROVISIONAL) into Weaviate: {consultation_id} → "
            f"{screen_type} (hash={shash}, platform={platform})"
        )

    except Exception as e:
        logger.warning(f"Failed to embed screen (non-fatal): {e}")


def respond_to_consultation(
    consultation_id: str,
    screen_type: str,
    action: dict = None,
    requires_validation: bool = True,
    yaml_created: bool = False,
    extract: dict = None,
    tree: dict = None,
    expected_next: list = None,
) -> dict:
    """
    Create consultation response (called by Spark Claude).

    Args:
        consultation_id: The consultation ID
        screen_type: Name of the screen type
        action: Legacy action dict (prefer tree:)
        requires_validation: Whether Mac should send validation request after
        yaml_created: Whether YAML config was created/updated
        extract: Phase 5 extraction config (text criteria, image bbox, etc.)
        tree: V9 behavior tree for Mac to execute
        expected_next: List of screen_types that should follow this BT

    Returns:
        Response dict written to response.json
    """
    consult_path = CONSULT_DIR / consultation_id

    if not consult_path.exists():
        return {"error": f"Consultation {consultation_id} not found"}

    response = {
        "consultation_id": consultation_id,
        "screen_type": screen_type,
        "yaml_created": yaml_created,
        "requires_validation": requires_validation,
        "responded_at": datetime.now().isoformat()
    }

    # V9: Include behavior tree if provided
    if tree:
        response["tree"] = tree

    # Legacy: include action if provided
    if action:
        response["action"] = action

    # Include Phase 5 extraction config if provided
    if extract:
        response["extract"] = extract

    # Include expected_next for validation chain
    if expected_next:
        response["expected_next"] = expected_next

    # Write response file (atomic to prevent partial reads by Mac polling)
    atomic_write_json(consult_path / "response.json", response)

    # Update metadata status to "complete" so 1-at-a-time check doesn't deadlock
    meta_file = consult_path / "metadata.json"
    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text())
            meta["status"] = "complete"
            meta["responded_at"] = response["responded_at"]
            atomic_write_json(meta_file, meta)
        except Exception as e:
            logger.warning(f"Failed to update metadata status (non-fatal): {e}")

    # Update state
    state = get_consultation_state(consultation_id)
    if state:
        state.spark_attempts += 1
        state.add_attempt("spark_claude", {"screen_type": screen_type, "action": action})

    # Embed screen into Weaviate as PROVISIONAL (validated=False).
    # Will be promoted to validated=True after Mac proves the BT works.
    _embed_screen_to_weaviate(consultation_id, screen_type, tree, consult_path, expected_next)

    logger.info(f"Consultation responded: {consultation_id} → {screen_type}")

    return response
