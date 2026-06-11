"""
V21 Variant BT Cache + Skeleton Hash Index.

Replaces V17-V20 Jaccard signature matching with:
  1. Exact skeleton hash → variant lookup (free, ~0ms)
  2. Variant → stored BT lookup (no fuzzy matching)

Two data files per platform under TAEY_ED_DATA_DIR (see paths.py):
  variant_bts/{platform}.json   — BT per variant
  hash_index/{platform}.json    — hash → variant mapping

No Jaccard. No discriminative markers. No common element computation.
If the hash doesn't match exactly, don't guess — let Flash classify.
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .knowledge_loader import (
    get_verified_bt_template_entry,
    increment_operational_note_verified_count,
    load_knowledge,
)
from .paths import VARIANT_BTS_DIR, HASH_INDEX_DIR

logger = logging.getLogger("taey-ed")

VERIFIED_TEMPLATE_REUSE_THRESHOLD = 3

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


def is_non_deterministic(platform: str, variant: str) -> bool:
    """Check if a variant needs a fresh BT every time.
    Any EXERCISE_* variant is non-deterministic (questions change per instance).
    """
    # Bare "EXERCISE" included: questions change per instance regardless of
    # subtype. Observed live 2026-06-11: a hash relabeled to bare EXERCISE
    # was treated deterministic and replayed a stale stored BT on a test Q.
    if (variant in NON_DETERMINISTIC_VARIANTS
            or variant == "EXERCISE"
            or variant.startswith("EXERCISE_")):
        try:
            knowledge = load_knowledge(platform)
            return (
                get_verified_bt_template_entry(
                    knowledge,
                    variant,
                    min_verified=VERIFIED_TEMPLATE_REUSE_THRESHOLD,
                )
                is None
            )
        except Exception as e:
            logger.warning(f"variant_cache: verified template check failed for {platform}/{variant}: {e}")
            return True
    return False


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
        knowledge = load_knowledge(platform)
        template_entry = get_verified_bt_template_entry(
            knowledge,
            variant,
            min_verified=VERIFIED_TEMPLATE_REUSE_THRESHOLD,
        )
        if not template_entry:
            return None
        template = template_entry["template"]
        return {
            "behavior_tree": template["tree"],
            "extract": template.get("extract"),
            "expected_next": template.get("expected_next", []),
            "master_type": template_entry["source"].get("master_screen_type", variant.split("_")[0]),
            "validated": True,
            "success_count": int(template_entry["source"].get("verified_count", 0) or 0),
            "source": template_entry["source"],
        }
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
    source="gemini_pro",
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


def _subtype_matches_variant(variant: str, source: dict) -> bool:
    """Credit/debit attribution guard (grok task-8c8a258f counter-example,
    observed live 2026-06-11): the template lookup falls back across ALL of a
    master's subtypes, so first-match crediting gave checkbox/ranking
    successes to the DROPDOWN note — inflating its verified_count to the
    replay gate and causing a stale-template replay on a ranking question.
    A fallback-resolved source may only be credited/debited when its subtype
    matches the variant's subtype EXACTLY. No match -> no attribution
    (under-crediting is safe; cross-crediting poisons the gate)."""
    try:
        from spark.tasks.knowledge_loader import (
            _variant_subtype_key, _normalize_subtype_name,
        )
        from spark.tasks.screen_type_util import get_master_category
        master = get_master_category(variant) or variant
        want = _variant_subtype_key(variant, master)
        if not want:
            return False  # bare master — cannot attribute to any one note
        have = _normalize_subtype_name(str(source.get("subtype_name") or ""))
        return bool(have) and have == want
    except Exception as e:
        logger.warning(f"variant_cache: subtype-match check failed ({variant}): {e}")
        return False


def mark_variant_validated(platform: str, variant: str):
    """Increment success count and set validated=True.

    Source-note credit resolves at min_verified=0, NOT the reuse threshold:
    a note must accumulate verified_count 0->1->2->3 through worker-built
    successes BEFORE it qualifies for template replay. Resolving credit
    through the reuse gate would trap every note below 3 forever (the
    chicken-and-egg Jesse flagged 2026-05-18).
    """
    source = None
    resolved = lookup_variant_bt(platform, variant)
    if resolved:
        source = resolved.get("source")
    if not isinstance(source, dict):
        try:
            knowledge = load_knowledge(platform)
            template_entry = get_verified_bt_template_entry(
                knowledge, variant, min_verified=0,
            )
            if template_entry and _subtype_matches_variant(
                variant, template_entry.get("source") or {},
            ):
                source = template_entry["source"]
        except Exception as e:
            logger.warning(
                f"variant_cache: credit-source lookup failed for {platform}/{variant}: {e}"
            )

    data = _load_variants(platform)
    entry = data["variants"].get(variant)
    if entry:
        entry["validated"] = True
        entry["success_count"] = entry.get("success_count", 0) + 1
        entry["consecutive_failures"] = 0
        entry["last_success"] = datetime.now(timezone.utc).isoformat()
        _atomic_write(_variant_path(platform), data)
        logger.info(f"variant_cache: validated {variant} (count={entry['success_count']})")

    if isinstance(source, dict) and source.get("kind") == "operational_note":
        increment_operational_note_verified_count(platform, source)


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
        entry["consecutive_failures"] = 0
        entry["last_seen"] = datetime.now(timezone.utc).isoformat()
        _atomic_write(_hash_index_path(platform), data)


def record_validated_map_failure(platform: str, skel_hash: str, variant: str = None) -> bool:
    """Demotion path (INTENDED_FLOW §E): a VALIDATED map that fails twice
    consecutively is demoted back into the learning loop — validated=False
    (the normal unvalidated handling applies on its next failure) and its
    credited operational_note debited one step, dropping it below the replay
    gate. Never deleted here; never demoted on a one-off failure.

    Returns True if a demotion happened, False otherwise.
    """
    data = _load_hash_index(platform)
    entry = data["hashes"].get(skel_hash)
    if not entry or not entry.get("validated"):
        return False
    fails = int(entry.get("consecutive_failures", 0) or 0) + 1
    entry["consecutive_failures"] = fails
    if fails < 2:
        _atomic_write(_hash_index_path(platform), data)
        logger.info(
            f"variant_cache: validated map {variant or skel_hash[:12]} failure "
            f"{fails}/2 — keeping (demotes at 2 consecutive)"
        )
        return False

    entry["validated"] = False
    entry["consecutive_failures"] = 0
    _atomic_write(_hash_index_path(platform), data)

    source = None
    if variant:
        vdata = _load_variants(platform)
        ventry = vdata["variants"].get(variant)
        if ventry:
            ventry["validated"] = False
            source = ventry.get("source")
            _atomic_write(_variant_path(platform), vdata)
        if not isinstance(source, dict):
            try:
                knowledge = load_knowledge(platform)
                template_entry = get_verified_bt_template_entry(
                    knowledge, variant, min_verified=1,
                )
                if template_entry and _subtype_matches_variant(
                    variant, template_entry.get("source") or {},
                ):
                    source = template_entry["source"]
            except Exception as e:
                logger.warning(
                    f"variant_cache: demote-source lookup failed for {platform}/{variant}: {e}"
                )
    if isinstance(source, dict) and source.get("kind") == "operational_note":
        increment_operational_note_verified_count(platform, source, increment=-1)

    logger.warning(
        f"variant_cache: DEMOTED {variant or skel_hash[:12]} after {fails} "
        f"consecutive failures — back into the learning loop (not deleted)"
    )
    return True


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
