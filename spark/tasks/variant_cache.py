"""
Variant BT cache + skeleton hash index.

Post-scr1 slash shape:
  - exact skeleton hash -> variant lookup
  - variant -> stored canonical BT lookup
  - validation/demotion lives entirely in cache files

No knowledge.json template replay. No self-rewrite credit/debit path.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .paths import HASH_INDEX_DIR, VARIANT_BTS_DIR

logger = logging.getLogger("taey-ed")


def is_non_deterministic(platform: str, variant: str) -> bool:
    """A variant is deterministic iff a canonical stored BT exists for it."""
    entry = lookup_variant_bt(platform, variant)
    return not bool(entry and entry.get("behavior_tree"))


def _atomic_write(path: Path, data: dict):
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
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _variant_path(platform: str) -> Path:
    return VARIANT_BTS_DIR / f"{platform}.json"


def _load_variants(platform: str) -> dict:
    data = _load_json(_variant_path(platform))
    if "variants" not in data:
        data["variants"] = {}
    return data


def lookup_variant_bt(platform: str, variant: str) -> dict | None:
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
        "source": entry.get("source"),
    }


def store_variant_bt(
    platform: str,
    variant: str,
    bt: dict,
    extract: dict = None,
    expected_next: list = None,
    source="worker",
):
    data = _load_variants(platform)
    now = datetime.now(timezone.utc).isoformat()
    master_type = variant.split("_")[0] if "_" in variant else variant
    existing = data["variants"].get(variant, {})
    data["variants"][variant] = {
        "master_type": master_type,
        "behavior_tree": bt,
        "extract": extract,
        "expected_next": expected_next or [],
        "validated": existing.get("validated", False),
        "success_count": existing.get("success_count", 0),
        "consecutive_failures": existing.get("consecutive_failures", 0),
        "last_updated": now,
        "source": source,
    }
    _atomic_write(_variant_path(platform), data)
    logger.info(f"variant_cache: stored BT for {variant} on {platform} (source={source})")


def mark_variant_validated(platform: str, variant: str):
    data = _load_variants(platform)
    entry = data["variants"].get(variant)
    if not entry:
        return
    entry["validated"] = True
    entry["success_count"] = entry.get("success_count", 0) + 1
    entry["consecutive_failures"] = 0
    entry["last_success"] = datetime.now(timezone.utc).isoformat()
    _atomic_write(_variant_path(platform), data)
    logger.info(f"variant_cache: validated {variant} (count={entry['success_count']})")


def invalidate_variant_bt(platform: str, variant: str):
    data = _load_variants(platform)
    entry = data["variants"].get(variant)
    if not entry:
        return
    entry["behavior_tree"] = None
    entry["validated"] = False
    entry["source"] = None
    _atomic_write(_variant_path(platform), data)
    logger.info(f"variant_cache: invalidated BT for {variant}")


def _hash_index_path(platform: str) -> Path:
    return HASH_INDEX_DIR / f"{platform}.json"


def _load_hash_index(platform: str) -> dict:
    data = _load_json(_hash_index_path(platform))
    if "hashes" not in data:
        data["hashes"] = {}
    return data


def lookup_by_hash(platform: str, skel_hash: str) -> dict | None:
    data = _load_hash_index(platform)
    entry = data["hashes"].get(skel_hash)
    if not entry:
        return None
    return {
        "variant": entry["variant"],
        "validated": entry.get("validated", False),
    }


def register_hash(platform: str, skel_hash: str, variant: str):
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
    data = _load_hash_index(platform)
    if skel_hash in data["hashes"]:
        variant = data["hashes"][skel_hash].get("variant", "?")
        del data["hashes"][skel_hash]
        _atomic_write(_hash_index_path(platform), data)
        logger.info(f"variant_cache: deleted hash {skel_hash[:12]} (was {variant})")


def mark_hash_validated(platform: str, skel_hash: str):
    data = _load_hash_index(platform)
    entry = data["hashes"].get(skel_hash)
    if entry:
        entry["validated"] = True
        entry["consecutive_failures"] = 0
        entry["last_seen"] = datetime.now(timezone.utc).isoformat()
        _atomic_write(_hash_index_path(platform), data)


def record_validated_map_failure(platform: str, skel_hash: str, variant: str = None) -> bool:
    data = _load_hash_index(platform)
    entry = data["hashes"].get(skel_hash)
    if not entry or not entry.get("validated"):
        return False
    fails = int(entry.get("consecutive_failures", 0) or 0) + 1
    entry["consecutive_failures"] = fails
    if fails < 2:
        _atomic_write(_hash_index_path(platform), data)
        logger.info(
            f"variant_cache: validated map {variant or skel_hash[:12]} failure {fails}/2 — keeping"
        )
        return False

    entry["validated"] = False
    entry["consecutive_failures"] = 0
    _atomic_write(_hash_index_path(platform), data)

    if variant:
        vdata = _load_variants(platform)
        ventry = vdata["variants"].get(variant)
        if ventry:
            ventry["validated"] = False
            ventry["consecutive_failures"] = 0
            _atomic_write(_variant_path(platform), vdata)

    logger.warning(
        f"variant_cache: DEMOTED {variant or skel_hash[:12]} after {fails} consecutive failures"
    )
    return True


def get_stats(platform: str = None) -> dict:
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
        stats[f.stem] = get_stats(f.stem)
    return stats
