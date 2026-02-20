# STATUS: FROZEN - Bug-fixed from v7. Verified 2026-02-20. Do not modify.
"""
Layer 3: Weaviate Screen Memory

Stores and retrieves screen embeddings with associated behavior trees.

Schema: ScreenEmbedding collection with:
  - skeleton_hash: deterministic hash of tree structure
  - platform: which platform (khan_academy, coursera, etc.)
  - screen_type: screen category (QUIZ_MULTIPLE_CHOICE, VIDEO_PLAYING, etc.)
  - behavior_tree: JSON-serialized BT that worked for this screen
  - success_count: how many times this BT succeeded
  - last_seen: when this screen was last encountered
  - skeleton_text: the raw skeleton string (for debugging)
  - validated: whether Mac has proven this BT works (False = provisional)
  - expected_next: JSON list of screen_types that should follow this screen
  - source: origin of this entry ("consultation" | "collapse" | "reinforcement")

Uses Weaviate v4 client API.
Embedding vectors are 4096-dim from Qwen3-Embedding-8B.
"""

import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

import requests
import weaviate
from weaviate.classes.config import Configure, Property, DataType
from weaviate.classes.query import MetadataQuery

# Infrastructure
WEAVIATE_HOST = "192.168.100.10"
WEAVIATE_PORT = 8088
WEAVIATE_GRPC = 50052
EMBEDDING_URL = "http://192.168.100.10:8091"
EMBEDDING_MODEL = "Qwen/Qwen3-Embedding-8B"

COLLECTION_NAME = "ScreenEmbedding"
VECTOR_DIMS = 4096


class ScreenMemoryError(Exception):
    """Raised on screen memory failures. No fallbacks."""
    pass


def get_client() -> weaviate.WeaviateClient:
    """Get connected Weaviate client."""
    client = weaviate.connect_to_local(
        host=WEAVIATE_HOST, port=WEAVIATE_PORT, grpc_port=WEAVIATE_GRPC
    )
    return client


def ensure_schema(client: Optional[weaviate.WeaviateClient] = None):
    """Create ScreenEmbedding collection if it doesn't exist. Migrate if needed."""
    own_client = client is None
    if own_client:
        client = get_client()

    try:
        if client.collections.exists(COLLECTION_NAME):
            # Collection exists — check for missing properties and add them
            migrate_schema(client)
            return

        client.collections.create(
            name=COLLECTION_NAME,
            vectorizer_config=Configure.Vectorizer.none(),  # We provide vectors
            properties=[
                Property(name="skeleton_hash", data_type=DataType.TEXT),
                Property(name="platform", data_type=DataType.TEXT),
                Property(name="screen_type", data_type=DataType.TEXT),
                Property(name="behavior_tree", data_type=DataType.TEXT),
                Property(name="success_count", data_type=DataType.INT),
                Property(name="last_seen", data_type=DataType.DATE),
                Property(name="skeleton_text", data_type=DataType.TEXT),
                Property(name="validated", data_type=DataType.BOOL),
                Property(name="expected_next", data_type=DataType.TEXT),
                Property(name="source", data_type=DataType.TEXT),
            ],
        )
    finally:
        if own_client:
            client.close()


def migrate_schema(client: Optional[weaviate.WeaviateClient] = None):
    """Add missing properties to existing ScreenEmbedding collection."""
    own_client = client is None
    if own_client:
        client = get_client()

    try:
        if not client.collections.exists(COLLECTION_NAME):
            return

        collection = client.collections.get(COLLECTION_NAME)
        existing_props = {p.name for p in collection.config.get().properties}

        new_props = [
            ("validated", DataType.BOOL),
            ("expected_next", DataType.TEXT),
            ("source", DataType.TEXT),
        ]
        for name, dtype in new_props:
            if name not in existing_props:
                collection.config.add_property(Property(name=name, data_type=dtype))
                logger.info(f"Migrated: added property '{name}' to {COLLECTION_NAME}")
    except Exception as e:
        logger.warning(f"Schema migration error (non-fatal): {e}")
    finally:
        if own_client:
            client.close()


def embed_text(text: str, timeout: int = 30) -> list[float]:
    """Get embedding vector from Qwen3-Embedding-8B via vLLM."""
    r = requests.post(
        f"{EMBEDDING_URL}/v1/embeddings",
        json={"input": text, "model": EMBEDDING_MODEL},
        timeout=timeout,
    )
    r.raise_for_status()
    data = r.json()
    if "data" in data and len(data["data"]) > 0:
        return data["data"][0]["embedding"]
    raise ScreenMemoryError(f"Empty embedding from {EMBEDDING_URL}")


def query_nearest(
    vector: list[float],
    platform: Optional[str] = None,
    limit: int = 1,
    validated_only: bool = False,
    client: Optional[weaviate.WeaviateClient] = None,
) -> list[dict]:
    """
    Find nearest screen embeddings by vector similarity.

    Returns list of dicts with:
      - skeleton_hash, platform, behavior_tree, success_count, last_seen
      - distance (0.0 = identical, higher = more different)

    Uses cosine distance. Thresholds:
      < 0.05  → KNOWN (exact structural match)
      < 0.191 → ISOMORPHIC (same type, different content)
      >= 0.191 → UNCHARTED
    """
    own_client = client is None
    if own_client:
        client = get_client()

    try:
        collection = client.collections.get(COLLECTION_NAME)

        filters = None
        from weaviate.classes.query import Filter
        if platform:
            filters = Filter.by_property("platform").equal(platform)
        if validated_only:
            v_filter = Filter.by_property("validated").equal(True)
            filters = filters & v_filter if filters else v_filter

        results = collection.query.near_vector(
            near_vector=vector,
            limit=limit,
            filters=filters,
            return_metadata=MetadataQuery(distance=True),
        )

        matches = []
        for obj in results.objects:
            props = obj.properties
            matches.append({
                "skeleton_hash": props.get("skeleton_hash", ""),
                "platform": props.get("platform", ""),
                "screen_type": props.get("screen_type", ""),
                "behavior_tree": props.get("behavior_tree", ""),
                "success_count": props.get("success_count", 0),
                "last_seen": props.get("last_seen"),
                "skeleton_text": props.get("skeleton_text", ""),
                "validated": props.get("validated", False),
                "expected_next": props.get("expected_next", "[]"),
                "source": props.get("source", ""),
                "distance": obj.metadata.distance,
                "uuid": str(obj.uuid),
            })
        return matches

    finally:
        if own_client:
            client.close()


def store_screen(
    vector: list[float],
    skeleton_hash: str,
    platform: str,
    behavior_tree: dict,
    skeleton_text: str = "",
    screen_type: str = "",
    client: Optional[weaviate.WeaviateClient] = None,
    validated: bool = False,
    expected_next: str = "[]",
    source: str = "unknown",
):
    """
    Store a screen embedding with its behavior tree.

    If a screen with the same skeleton_hash already exists,
    update behavior_tree and last_seen. Does NOT auto-promote validated.
    Otherwise create new entry with validated=False (provisional).
    """
    own_client = client is None
    if own_client:
        client = get_client()

    try:
        collection = client.collections.get(COLLECTION_NAME)
        bt_json = json.dumps(behavior_tree) if isinstance(behavior_tree, dict) else behavior_tree
        now = datetime.now(timezone.utc).isoformat()

        # Check if this skeleton_hash + screen_type already exists.
        # Multiple DIFFERENT screen types can share the same skeleton hash
        # (same DOM structure, different content). Each gets its own entry.
        from weaviate.classes.query import Filter
        existing = collection.query.fetch_objects(
            filters=Filter.by_property("skeleton_hash").equal(skeleton_hash),
            limit=10,
        )

        # Find entry matching THIS screen_type (not just any hash match)
        same_type_obj = None
        for obj in existing.objects:
            if obj.properties.get("screen_type", "") == screen_type:
                same_type_obj = obj
                break

        if same_type_obj:
            # Same hash AND same screen_type — update or skip
            is_validated = same_type_obj.properties.get("validated", False)
            old_count = same_type_obj.properties.get("success_count", 0)

            # GUARD: Do NOT overwrite validated entries with provisional BTs.
            if is_validated and not validated:
                logger.info(
                    f"store_screen: SKIPPED overwrite of validated entry "
                    f"{skeleton_hash[:12]} ({screen_type}). "
                    f"New BT from {source} is provisional — keeping proven BT."
                )
                return

            # Update existing: update BT and timestamp, preserve validated status
            update_props = {
                "behavior_tree": bt_json,
                "success_count": old_count + 1,
                "last_seen": now,
            }
            if screen_type:
                update_props["screen_type"] = screen_type
            if expected_next and expected_next != "[]":
                update_props["expected_next"] = expected_next
            if source:
                update_props["source"] = source
            collection.data.update(
                uuid=same_type_obj.uuid,
                properties=update_props,
            )
            logger.info(f"store_screen: Updated existing {screen_type} (hash={skeleton_hash[:12]})")
        else:
            # No entry with this screen_type — INSERT new entry.
            # (Other screen types may exist with the same hash — that's fine,
            # vector similarity distinguishes them by content.)
            collection.data.insert(
                properties={
                    "skeleton_hash": skeleton_hash,
                    "platform": platform,
                    "screen_type": screen_type,
                    "behavior_tree": bt_json,
                    "success_count": 1,
                    "last_seen": now,
                    "skeleton_text": skeleton_text,
                    "validated": validated,
                    "expected_next": expected_next or "[]",
                    "source": source,
                },
                vector=vector,
            )
            if existing.objects:
                other_types = [o.properties.get("screen_type", "?") for o in existing.objects]
                logger.info(
                    f"store_screen: NEW entry for {screen_type} (hash={skeleton_hash[:12]}, "
                    f"same hash as: {other_types})"
                )

    finally:
        if own_client:
            client.close()


def mark_validated(skeleton_hash: str, screen_type: str = "", client: Optional[weaviate.WeaviateClient] = None):
    """Promote a provisional screen to validated. Called after Mac proves BT works."""
    own_client = client is None
    if own_client:
        client = get_client()

    try:
        collection = client.collections.get(COLLECTION_NAME)
        from weaviate.classes.query import Filter
        existing = collection.query.fetch_objects(
            filters=Filter.by_property("skeleton_hash").equal(skeleton_hash),
            limit=10,
        )
        # Find the entry matching THIS screen_type. No fallback to objects[0]
        # — guessing the wrong entry corrupts the knowledge base.
        target_obj = None
        if screen_type:
            for obj in existing.objects:
                if obj.properties.get("screen_type", "") == screen_type:
                    target_obj = obj
                    break
        elif existing.objects and len(existing.objects) == 1:
            # Only fall back to first object if there's exactly one match
            target_obj = existing.objects[0]

        if target_obj:
            old_count = target_obj.properties.get("success_count", 0)
            collection.data.update(
                uuid=target_obj.uuid,
                properties={
                    "validated": True,
                    "success_count": old_count + 1,
                    "last_seen": datetime.now(timezone.utc).isoformat(),
                },
            )
            logger.info(f"mark_validated: {skeleton_hash[:12]} type={target_obj.properties.get('screen_type','')} (count={old_count + 1})")
        else:
            other_types = [o.properties.get("screen_type", "?") for o in existing.objects] if existing.objects else []
            logger.warning(
                f"mark_validated: hash {skeleton_hash[:12]} screen_type={screen_type!r} "
                f"not found (existing types: {other_types})"
            )
    finally:
        if own_client:
            client.close()


def mark_invalidated(skeleton_hash: str, screen_type: str = "", client: Optional[weaviate.WeaviateClient] = None):
    """Mark a screen as invalid (e.g., wrong answer detected). BT needs replacement."""
    own_client = client is None
    if own_client:
        client = get_client()

    try:
        collection = client.collections.get(COLLECTION_NAME)
        from weaviate.classes.query import Filter
        existing = collection.query.fetch_objects(
            filters=Filter.by_property("skeleton_hash").equal(skeleton_hash),
            limit=10,
        )
        # Find the entry matching THIS screen_type. No fallback to objects[0]
        # — invalidating the wrong entry corrupts the knowledge base.
        target_obj = None
        if screen_type:
            for obj in existing.objects:
                if obj.properties.get("screen_type", "") == screen_type:
                    target_obj = obj
                    break
        elif existing.objects and len(existing.objects) == 1:
            target_obj = existing.objects[0]

        if target_obj:
            collection.data.update(
                uuid=target_obj.uuid,
                properties={
                    "validated": False,
                    "last_seen": datetime.now(timezone.utc).isoformat(),
                },
            )
            logger.info(f"mark_invalidated: {skeleton_hash[:12]} type={target_obj.properties.get('screen_type','')}")
        else:
            other_types = [o.properties.get("screen_type", "?") for o in existing.objects] if existing.objects else []
            logger.warning(
                f"mark_invalidated: hash {skeleton_hash[:12]} screen_type={screen_type!r} "
                f"not found (existing types: {other_types})"
            )
    finally:
        if own_client:
            client.close()


def query_by_hash(
    skeleton_hash: str,
    client: Optional[weaviate.WeaviateClient] = None,
) -> Optional[dict]:
    """Fetch a specific screen by skeleton_hash. Returns properties dict or None."""
    own_client = client is None
    if own_client:
        client = get_client()

    try:
        collection = client.collections.get(COLLECTION_NAME)
        from weaviate.classes.query import Filter
        results = collection.query.fetch_objects(
            filters=Filter.by_property("skeleton_hash").equal(skeleton_hash),
            limit=1,
        )
        if results.objects:
            props = results.objects[0].properties
            return {
                "skeleton_hash": props.get("skeleton_hash", ""),
                "platform": props.get("platform", ""),
                "screen_type": props.get("screen_type", ""),
                "behavior_tree": props.get("behavior_tree", ""),
                "success_count": props.get("success_count", 0),
                "validated": props.get("validated", False),
                "expected_next": props.get("expected_next", "[]"),
                "source": props.get("source", ""),
                "uuid": str(results.objects[0].uuid),
            }
        return None
    finally:
        if own_client:
            client.close()


def get_stats(client: Optional[weaviate.WeaviateClient] = None) -> dict:
    """Get collection stats for monitoring."""
    own_client = client is None
    if own_client:
        client = get_client()

    try:
        if not client.collections.exists(COLLECTION_NAME):
            return {"exists": False, "count": 0}

        collection = client.collections.get(COLLECTION_NAME)
        agg = collection.aggregate.over_all(total_count=True)
        return {
            "exists": True,
            "count": agg.total_count,
        }
    finally:
        if own_client:
            client.close()
