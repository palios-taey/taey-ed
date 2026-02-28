"""
V21 Variant BT Cache + Skeleton Hash Index.

Replaces V17-V20 Jaccard signature matching with:
  1. Exact skeleton hash → variant lookup (free, ~0ms)
  2. Variant → stored BT lookup (no fuzzy matching)

Two data files per platform:
  /var/spark/taey-ed/variant_bts/{platform}.json   — BT per variant
  /var/spark/taey-ed/hash_index/{platform}.json     — hash → variant mapping

No Jaccard. No discriminative markers. No common element computation.
If the hash doesn't match exactly, don't guess — let Flash classify.
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("taey-ed")

VARIANT_BTS_DIR = Path("/var/spark/taey-ed/variant_bts")
HASH_INDEX_DIR = Path("/var/spark/taey-ed/hash_index")

# Variants where BT varies per instance (always rebuild via Pro)
# Any EXERCISE_* variant is non-deterministic — questions change per instance.
NON_DETERMINISTIC_VARIANTS = {
    "EXERCISE_RADIO",
    "EXERCISE_CHECKBOX",
    "EXERCISE_TEXT_INPUT",
    "EXERCISE_MATCHING",
    "EXERCISE_DROPDOWN",
    "EXERCISE_FREE_RESPONSE",
    "EXERCISE_ASSESSMENT",
    "EXERCISE_MULTIPLE_CHOICE_MIXED",
    "EXERCISE_MULTIPLE_CHOICE_MULTI",
}


def is_non_deterministic(variant: str) -> bool:
    """Check if a variant needs a fresh BT every time.
    Any EXERCISE_* variant is non-deterministic (questions change per instance).
    """
    if variant in NON_DETERMINISTIC_VARIANTS:
        return True
    # Catch-all: any variant starting with EXERCISE_ is non-deterministic
    return variant.startswith("EXERCISE_")


# ── Atomic file I/O ──

def _atomic_write(path: Path, data: dict):
    """Write JSON atomically via tempfile + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.rename(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_json(path: Path) -> dict:
    """Load JSON file, return empty dict if missing."""
    if path.exists():
        return json.loads(path.read_text())
    return {}


# ── Variant BT Store ──

def _variant_path(platform: str) -> Path:
    return VARIANT_BTS_DIR / f"{platform}.json"


def _load_variants(platform: str) -> dict:
    data = _load_json(_variant_path(platform))
    if "variants" not in data:
        data["variants"] = {}
    return data


def lookup_variant_bt(platform: str, variant: str) -> dict | None:
    """
    Get stored BT for a variant. Returns dict with keys:
        behavior_tree, extract, expected_next, master_type
    or None if no BT stored.
    """
    data = _load_variants(platform)
    entry = data["variants"].get(variant)
    if not entry or not entry.get("behavior_tree"):
        return None
    return {
        "behavior_tree": entry["behavior_tree"],
        "extract": entry.get("extract"),
        "expected_next": entry.get("expected_next", []),
        "master_type": entry.get("master_type", variant.split("_")[0]),
        "validated": entry.get("validated", False),
        "success_count": entry.get("success_count", 0),
    }


def store_variant_bt(
    platform: str,
    variant: str,
    bt: dict,
    extract: dict = None,
    expected_next: list = None,
    source: str = "gemini_pro",
):
    """Store or update a BT for a variant."""
    data = _load_variants(platform)
    now = datetime.now(timezone.utc).isoformat()

    # Determine master type from variant name
    master_type = variant.split("_")[0] if "_" in variant else variant

    existing = data["variants"].get(variant, {})
    data["variants"][variant] = {
        "master_type": master_type,
        "behavior_tree": bt,
        "extract": extract,
        "expected_next": expected_next or [],
        "validated": existing.get("validated", False),
        "success_count": existing.get("success_count", 0),
        "last_updated": now,
        "source": source,
    }

    _atomic_write(_variant_path(platform), data)
    logger.info(f"variant_cache: stored BT for {variant} on {platform} (source={source})")


def mark_variant_validated(platform: str, variant: str):
    """Increment success count and set validated=True."""
    data = _load_variants(platform)
    entry = data["variants"].get(variant)
    if not entry:
        return
    entry["validated"] = True
    entry["success_count"] = entry.get("success_count", 0) + 1
    entry["last_success"] = datetime.now(timezone.utc).isoformat()
    _atomic_write(_variant_path(platform), data)
    logger.info(f"variant_cache: validated {variant} (count={entry['success_count']})")


def invalidate_variant_bt(platform: str, variant: str):
    """Clear a stored BT (e.g., after knowledge.json change)."""
    data = _load_variants(platform)
    entry = data["variants"].get(variant)
    if not entry:
        return
    entry["behavior_tree"] = None
    entry["validated"] = False
    entry["source"] = None
    _atomic_write(_variant_path(platform), data)
    logger.info(f"variant_cache: invalidated BT for {variant}")


# ── Skeleton Hash Index ──

def _hash_index_path(platform: str) -> Path:
    return HASH_INDEX_DIR / f"{platform}.json"


def _load_hash_index(platform: str) -> dict:
    data = _load_json(_hash_index_path(platform))
    if "hashes" not in data:
        data["hashes"] = {}
    return data


def lookup_by_hash(platform: str, skel_hash: str) -> dict | None:
    """
    Exact hash lookup. Returns {"variant": ..., "validated": ...}
    or None if hash not registered.
    """
    data = _load_hash_index(platform)
    entry = data["hashes"].get(skel_hash)
    if not entry:
        return None
    return {
        "variant": entry["variant"],
        "validated": entry.get("validated", False),
    }


def register_hash(platform: str, skel_hash: str, variant: str):
    """Map a skeleton hash to a variant for future exact lookups."""
    data = _load_hash_index(platform)
    now = datetime.now(timezone.utc).isoformat()
    data["hashes"][skel_hash] = {
        "variant": variant,
        "validated": False,
        "registered_at": now,
        "last_seen": now,
    }
    _atomic_write(_hash_index_path(platform), data)
    logger.info(f"variant_cache: registered hash {skel_hash[:12]} → {variant}")


def delete_hash(platform: str, skel_hash: str):
    """Remove a bad hash mapping (after BT failure)."""
    data = _load_hash_index(platform)
    if skel_hash in data["hashes"]:
        variant = data["hashes"][skel_hash].get("variant", "?")
        del data["hashes"][skel_hash]
        _atomic_write(_hash_index_path(platform), data)
        logger.info(f"variant_cache: deleted hash {skel_hash[:12]} (was {variant})")


def mark_hash_validated(platform: str, skel_hash: str):
    """Mark a hash mapping as validated (BT succeeded for this hash)."""
    data = _load_hash_index(platform)
    entry = data["hashes"].get(skel_hash)
    if entry:
        entry["validated"] = True
        entry["last_seen"] = datetime.now(timezone.utc).isoformat()
        _atomic_write(_hash_index_path(platform), data)


# ── Stats ──

def get_stats(platform: str = None) -> dict:
    """Get cache stats for health/debug endpoints."""
    VARIANT_BTS_DIR.mkdir(parents=True, exist_ok=True)
    HASH_INDEX_DIR.mkdir(parents=True, exist_ok=True)

    if platform:
        variants = _load_variants(platform)
        hashes = _load_hash_index(platform)
        bt_count = sum(1 for v in variants["variants"].values() if v.get("behavior_tree"))
        return {
            "platform": platform,
            "variants_total": len(variants["variants"]),
            "variants_with_bt": bt_count,
            "hash_mappings": len(hashes["hashes"]),
        }

    stats = {}
    for f in VARIANT_BTS_DIR.glob("*.json"):
        p = f.stem
        stats[p] = get_stats(p)
    return stats
