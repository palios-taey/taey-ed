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


def _state_evidence(source: str, **extra) -> dict:
    return {"source": f"variant_cache.{source}", **extra}


def _state_repo():
    from spark.state_repo import get_state_repo
    return get_state_repo()


def _canonical_variant(platform: str, variant: str | None, tree: dict | None = None) -> str:
    value = str(variant or "").strip().upper()
    if not value:
        return ""
    try:
        from spark.tasks.classify_screen import canonicalize_screen_type
        canonical = canonicalize_screen_type(platform, value, tree)
        if canonical != "UNKNOWN":
            return canonical
    except Exception:
        logger.exception("variant_cache: canonicalize failed for %s/%s", platform, value)
    return value


def _canonical_expected_next(platform: str, expected_next) -> list[str]:
    if not isinstance(expected_next, list):
        return []
    return [
        canonical
        for item in expected_next
        if (canonical := _canonical_variant(platform, item))
        and canonical != "UNKNOWN"
    ]


def _mirror_hash_mapping(platform: str, skel_hash: str, variant: str, source: str, validated: bool = False) -> None:
    if not variant:
        return
    try:
        _state_repo().mirror_hash_mapping(
            platform=platform,
            skel_hash=skel_hash,
            screen_type=variant,
            actor="api",
            evidence=_state_evidence(source, skel_hash=skel_hash),
            validated=validated,
        )
    except Exception:
        logger.exception("state-store dual-write failed: variant_cache.%s", source)


def _mirror_screen_type_promotion(platform: str, variant: str, source: str) -> None:
    if not variant:
        return
    try:
        _state_repo().promote_screen_type(
            platform=platform,
            screen_type=variant,
            actor="api",
            evidence=_state_evidence(source),
        )
    except Exception:
        logger.exception("state-store dual-write failed: variant_cache.%s", source)


def _mirror_screen_type_demotion(platform: str, variant: str, source: str) -> None:
    if not variant:
        return
    try:
        _state_repo().demote_screen_type(
            platform=platform,
            screen_type=variant,
            actor="api",
            evidence=_state_evidence(source),
        )
    except Exception:
        logger.exception("state-store dual-write failed: variant_cache.%s", source)


def _mirror_variant_bt_delete(platform: str, variant: str, source: str) -> None:
    if not variant:
        return
    try:
        _state_repo().record_cache_delete(
            platform=platform,
            key_kind="widget_set",
            key_hash=f"variant_bt:{variant}",
            screen_type=variant,
            actor="api",
            evidence=_state_evidence(source, variant=variant),
        )
    except Exception:
        logger.exception("state-store dual-write failed: variant_cache.%s", source)


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


def _contains_generic_solver(bt: dict) -> bool:
    if not isinstance(bt, dict):
        return False
    stack = [bt]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        if node.get("action") == "send_to_llm":
            return True
        for key in ("children",):
            children = node.get(key)
            if isinstance(children, list):
                stack.extend(children)
        for key in ("do", "then", "else"):
            child = node.get(key)
            if isinstance(child, dict):
                stack.append(child)
    return False


def _has_frozen_answer(bt: dict) -> bool:
    if not isinstance(bt, dict):
        return False
    stack = [bt]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        action = node.get("action")
        params = node.get("params") or {}
        if action == "select_dropdown_option":
            option = str(params.get("option") or "")
            if option and not option.startswith("$"):
                return True
        if action == "find_and_type":
            text = str(params.get("text") or "")
            if text and not text.startswith("$"):
                return True
        if action == "find_and_click":
            role = params.get("role")
            target = str(params.get("target") or "")
            if role in {"AXRadioButton", "AXCheckBox"} and target and not target.startswith("$"):
                return True
        children = node.get("children")
        if isinstance(children, list):
            stack.extend(children)
        for key in ("do", "then", "else"):
            child = node.get(key)
            if isinstance(child, dict):
                stack.append(child)
    return False


def _cache_safe_behavior_tree(variant: str, behavior_tree: dict) -> bool:
    from spark.tasks.screen_type_util import get_master_category

    if get_master_category(variant) != "EXERCISE":
        return True
    return _contains_generic_solver(behavior_tree) and not _has_frozen_answer(behavior_tree)


def _is_transition_variant(variant: str) -> bool:
    from spark.tasks.screen_type_util import get_master_category

    return get_master_category(variant) == "TRANSITION"


def _purge_variant_bt_entry(entry: dict, *, source: str) -> bool:
    if not isinstance(entry, dict) or not entry.get("behavior_tree"):
        return False

    now = datetime.now(timezone.utc).isoformat()
    history = list(entry.get("history") or [])
    history.append({
        "behavior_tree": entry.get("behavior_tree"),
        "extract": entry.get("extract"),
        "expected_next": entry.get("expected_next", []),
        "source": entry.get("source"),
        "validated": entry.get("validated", False),
        "success_count": entry.get("success_count", 0),
        "purged_at": now,
        "purge_source": source,
    })
    entry["history"] = history[-25:]
    entry["behavior_tree"] = None
    entry["extract"] = None
    entry["expected_next"] = []
    entry["source"] = None
    entry["validated"] = False
    entry["consecutive_failures"] = 0
    entry["purged_at"] = now
    entry["purge_source"] = source
    return True


def _purge_active_variant_bt(platform: str, variant: str, source: str) -> bool:
    data = _load_variants(platform)
    entry = data["variants"].get(variant)
    if not _purge_variant_bt_entry(entry, source=source):
        return False
    _atomic_write(_variant_path(platform), data)
    _mirror_variant_bt_delete(platform, variant, source)
    logger.warning("variant_cache: purged BT for %s (source=%s)", variant, source)
    return True


def store_variant_bt(
    platform: str,
    variant: str,
    behavior_tree: dict,
    *,
    extract=None,
    expected_next=None,
    master_type: str | None = None,
    source: str = "validated_success",
) -> bool:
    variant = _canonical_variant(platform, variant)
    if not variant or variant == "UNKNOWN":
        return False

    from spark.tasks.screen_type_util import get_master_category

    if get_master_category(variant) == variant:
        logger.info("variant_cache: NOT storing BT for bare master %s", variant)
        return False
    if _is_transition_variant(variant):
        _purge_active_variant_bt(
            platform,
            variant,
            f"store_variant_bt_refused_transition.{source}",
        )
        logger.warning(
            "variant_cache: NOT storing BT for transition %s "
            "(transitions deterministic-serve from live tree)",
            variant,
        )
        return False
    if not isinstance(behavior_tree, dict) or not behavior_tree:
        logger.info("variant_cache: NOT storing empty BT for %s", variant)
        return False
    if not _cache_safe_behavior_tree(variant, behavior_tree):
        logger.warning(
            "variant_cache: NOT storing non-generic EXERCISE BT for %s "
            "(frozen answer guard)",
            variant,
        )
        return False

    data = _load_variants(platform)
    variants = data["variants"]
    now = datetime.now(timezone.utc).isoformat()
    existing = variants.get(variant) or {}
    replacing = existing.get("behavior_tree") and existing.get("behavior_tree") != behavior_tree
    entry = dict(existing)
    if replacing:
        history = list(entry.get("history") or [])
        history.append({
            "behavior_tree": entry.get("behavior_tree"),
            "extract": entry.get("extract"),
            "expected_next": entry.get("expected_next", []),
            "source": entry.get("source"),
            "validated": entry.get("validated", False),
            "success_count": entry.get("success_count", 0),
            "superseded_at": now,
        })
        entry["history"] = history[-25:]
        entry["validated"] = False
        entry["success_count"] = 0
        entry["consecutive_failures"] = 0

    entry.update({
        "behavior_tree": behavior_tree,
        "extract": extract,
        "expected_next": _canonical_expected_next(platform, expected_next),
        "master_type": master_type or get_master_category(variant),
        "source": source,
        "stored_at": now,
        "validated": bool(entry.get("validated", False)) and not replacing,
        "success_count": int(entry.get("success_count", 0) or 0) if not replacing else 0,
        "consecutive_failures": int(entry.get("consecutive_failures", 0) or 0),
    })
    variants[variant] = entry
    _atomic_write(_variant_path(platform), data)
    logger.info("variant_cache: stored BT for %s (source=%s)", variant, source)
    return True


def lookup_variant_bt(platform: str, variant: str) -> dict | None:
    variant = _canonical_variant(platform, variant)
    if _is_transition_variant(variant):
        _purge_active_variant_bt(platform, variant, "lookup_variant_bt_refused_transition")
        return None
    data = _load_variants(platform)
    entry = data["variants"].get(variant)
    if not entry or not entry.get("behavior_tree"):
        return None
    return {
        "behavior_tree": entry["behavior_tree"],
        "extract": entry.get("extract"),
        "expected_next": _canonical_expected_next(platform, entry.get("expected_next", [])),
        "master_type": entry.get("master_type", variant.split("_")[0]),
        "validated": entry.get("validated", False),
        "success_count": entry.get("success_count", 0),
        "source": entry.get("source"),
    }


def mark_variant_validated(platform: str, variant: str, *, mirror_state: bool = True):
    variant = _canonical_variant(platform, variant)
    data = _load_variants(platform)
    entry = data["variants"].get(variant)
    if not entry:
        return
    entry["validated"] = True
    entry["success_count"] = entry.get("success_count", 0) + 1
    entry["consecutive_failures"] = 0
    entry["last_success"] = datetime.now(timezone.utc).isoformat()
    _atomic_write(_variant_path(platform), data)
    if mirror_state:
        _mirror_screen_type_promotion(platform, variant, "mark_variant_validated")
    logger.info(f"variant_cache: validated {variant} (count={entry['success_count']})")


def invalidate_variant_bt(platform: str, variant: str):
    variant = _canonical_variant(platform, variant)
    data = _load_variants(platform)
    entry = data["variants"].get(variant)
    if not entry:
        return
    entry["behavior_tree"] = None
    entry["validated"] = False
    entry["source"] = None
    _atomic_write(_variant_path(platform), data)
    _mirror_screen_type_demotion(platform, variant, "invalidate_variant_bt")
    logger.info(f"variant_cache: invalidated BT for {variant}")


def purge_transition_variant_bts(platform: str | None = None) -> dict:
    platforms = [platform] if platform else sorted(path.stem for path in VARIANT_BTS_DIR.glob("*.json"))
    result = {
        "platforms": platforms,
        "examined": 0,
        "purged": 0,
        "variants": [],
    }
    for platform_name in platforms:
        data = _load_variants(platform_name)
        changed = False
        for variant, entry in sorted(data["variants"].items()):
            canonical = _canonical_variant(platform_name, variant)
            if not _is_transition_variant(canonical):
                continue
            result["examined"] += 1
            if _purge_variant_bt_entry(entry, source="purge_transition_variant_bts"):
                changed = True
                result["purged"] += 1
                result["variants"].append(f"{platform_name}:{variant}")
                _mirror_variant_bt_delete(
                    platform_name,
                    canonical,
                    "purge_transition_variant_bts",
                )
        if changed:
            _atomic_write(_variant_path(platform_name), data)
            logger.warning(
                "variant_cache: purged %s transition BT(s) for %s",
                result["purged"],
                platform_name,
            )
    return result


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
        "variant": _canonical_variant(platform, entry["variant"]),
        "validated": entry.get("validated", False),
    }


def register_hash(platform: str, skel_hash: str, variant: str):
    variant = _canonical_variant(platform, variant)
    # NEVER cache a bare MASTER (subtype unresolved, e.g. "EXERCISE" without
    # _MULTIPLE_SELECT) — it has no recipe, so serving it later hands the worker
    # the generic guide -> freelance/{}. RCA 2026-06-15 (d2b842): a mid-hydration
    # capture classified to the bare master and it got cached -> the bare-master
    # Step-4 guard deleted it -> re-classified -> re-cached = churn. Don't store
    # it at all; the next (hydrated) capture will resolve and cache the subtype.
    from spark.tasks.screen_type_util import get_master_category
    if variant and get_master_category(variant) == variant:
        logger.info(
            f"variant_cache: NOT caching bare master '{variant}' for hash "
            f"{skel_hash[:12]} (subtype unresolved — will re-classify next capture)"
        )
        return
    data = _load_hash_index(platform)
    now = datetime.now(timezone.utc).isoformat()
    data["hashes"][skel_hash] = {
        "variant": variant,
        "validated": False,
        "registered_at": now,
        "last_seen": now,
    }
    _atomic_write(_hash_index_path(platform), data)
    _mirror_hash_mapping(platform, skel_hash, variant, "register_hash")
    logger.info(f"variant_cache: registered hash {skel_hash[:12]} → {variant}")


def delete_hash(platform: str, skel_hash: str):
    data = _load_hash_index(platform)
    if skel_hash in data["hashes"]:
        variant = data["hashes"][skel_hash].get("variant", "?")
        del data["hashes"][skel_hash]
        _atomic_write(_hash_index_path(platform), data)
        try:
            _state_repo().record_cache_delete(
                platform=platform,
                key_kind="skeleton",
                key_hash=skel_hash,
                screen_type=variant,
                actor="api",
                evidence=_state_evidence("delete_hash"),
            )
        except Exception:
            logger.exception("state-store dual-write failed: variant_cache.delete_hash")
        logger.info(f"variant_cache: deleted hash {skel_hash[:12]} (was {variant})")


def mark_hash_validated(platform: str, skel_hash: str):
    data = _load_hash_index(platform)
    entry = data["hashes"].get(skel_hash)
    if entry:
        entry["variant"] = _canonical_variant(platform, entry.get("variant"))
        entry["validated"] = True
        entry["consecutive_failures"] = 0
        entry["last_seen"] = datetime.now(timezone.utc).isoformat()
        _atomic_write(_hash_index_path(platform), data)
        _mirror_hash_mapping(
            platform,
            skel_hash,
            entry.get("variant", ""),
            "mark_hash_validated",
            validated=True,
        )


def record_validated_map_failure(platform: str, skel_hash: str, variant: str = None) -> bool:
    variant = _canonical_variant(platform, variant) if variant else None
    data = _load_hash_index(platform)
    entry = data["hashes"].get(skel_hash)
    if not entry or not entry.get("validated"):
        return False
    entry["variant"] = _canonical_variant(platform, entry.get("variant"))
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
            _mirror_screen_type_demotion(platform, variant, "record_validated_map_failure")

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
