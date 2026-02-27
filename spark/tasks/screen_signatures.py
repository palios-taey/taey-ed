"""
Screen recognition via set-difference discriminative markers.

Walk accessibility tree, extract (role, text) pairs based on role type:
  - Stable-text roles (developer-set labels): include (role, text)
  - Structural-presence roles (presence matters, text variable): include (role, "*")
  - Variable-content roles: skip entirely

Screen signature = frozenset of these tuples.
Common elements = intersection of ALL known signatures (platform chrome).
Discriminative markers = signature - common.
Matching = Jaccard similarity on discriminative markers.

V19 fix (2026-02-27): Replaced V18 structural penalty with category-constrained
matching. Instead of penalizing structural mismatches after Jaccard scoring, we now
use structural_classify() to determine the master category FIRST, then only match
against signatures in the same category. This eliminates EXERCISE<->TRANSITION false
positives entirely rather than trying to adjust scores after the fact.

The structural pre-classifier uses analyze_tree() from prompt_codex.py which already
extracts HAS_RADIO, HAS_CHECKBOX, HAS_VIDEO, etc. These signals deterministically
map to master categories (VIDEO, EXERCISE, NAVIGATION, TRANSITION).
"""

import hashlib
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

SIGNATURES_DIR = Path("/var/spark/taey-ed/signatures")

# Roles with stable developer-set text labels
STABLE_TEXT_ROLES = {
    "AXButton", "AXTab", "AXMenuItem", "AXMenuButton",
    "AXPopUpButton", "AXComboBox", "AXDisclosureTriangle",
    "AXToolbar", "AXTabGroup",
}

# Roles where presence is structural (text is variable user content)
STRUCTURAL_PRESENCE_ROLES = {
    "AXRadioButton", "AXCheckBox", "AXTextField",
    "AXTextArea", "AXForm", "AXSlider",
}

# V19: Structural penalty removed. Category-constrained matching makes it unnecessary.
# See structural_classify() for the replacement approach.


def extract_signature(tree: dict) -> frozenset:
    """Walk tree, extract (role, text) pairs for discriminative matching."""
    pairs = set()
    stack = [tree]
    while stack:
        node = stack.pop()
        role = node.get("role", "")
        name = node.get("name") or node.get("title") or ""

        if role in STABLE_TEXT_ROLES and name:
            pairs.add((role, name))
        elif role in STRUCTURAL_PRESENCE_ROLES:
            pairs.add((role, "*"))

        children = node.get("children")
        if children:
            stack.extend(children)

    return frozenset(pairs)


def structural_classify(tree: dict) -> str:
    """Deterministic pre-classification from structural features.

    Uses analyze_tree() from prompt_codex.py which already extracts structural
    signals (HAS_RADIO, HAS_CHECKBOX, HAS_VIDEO, etc.) and maps them to master
    categories.

    Returns:
        Master category string: VIDEO, EXERCISE, NAVIGATION, TRANSITION,
        or UNCLASSIFIED if no structural signal is definitive.

    The key insight: structural elements (radio buttons, checkboxes, video players)
    are the TRUE differentiators between screen types. Jaccard similarity on
    button labels cannot distinguish screens that share 95%+ of their UI chrome.
    But the PRESENCE of radio buttons is a hard signal for EXERCISE, and the
    PRESENCE of a video player is a hard signal for VIDEO.

    UNCLASSIFIED screens fall through to Gemini classification -- this is correct
    behavior. ARTICLE screens often lack distinctive structural elements and
    need Gemini's visual analysis to identify.
    """
    from spark.tasks.prompt_codex import analyze_tree
    tags = analyze_tree(tree)

    # VIDEO is highest priority -- video player is unambiguous
    if "HAS_VIDEO" in tags:
        return "VIDEO"

    # Assessment signals -> EXERCISE (radio, checkbox, text input, dropdown)
    if any(t in tags for t in ["HAS_RADIO", "HAS_CHECKBOX", "HAS_TEXT_INPUT", "HAS_COMBOBOX"]):
        return "EXERCISE"

    # Many links with no assessment signals -> NAVIGATION
    if "HAS_MANY_LINKS" in tags:
        return "NAVIGATION"

    # Post-answer or generic transition signals
    if "TRANSITION" in tags:
        return "TRANSITION"

    # No definitive structural signal -- needs Gemini
    return "UNCLASSIFIED"


def _sig_hash(sig: frozenset) -> str:
    """Deterministic hash of a signature for storage keys."""
    canonical = sorted(sig)
    return hashlib.sha256(str(canonical).encode()).hexdigest()[:16]


def _load_platform(platform: str) -> dict:
    """Load platform signature data from JSON."""
    path = SIGNATURES_DIR / f"{platform}.json"
    if path.exists():
        return json.loads(path.read_text())
    return {"screens": {}, "common": []}


def _save_platform(platform: str, data: dict):
    """Save platform signature data to JSON."""
    SIGNATURES_DIR.mkdir(parents=True, exist_ok=True)
    path = SIGNATURES_DIR / f"{platform}.json"
    path.write_text(json.dumps(data, indent=2))


def _recompute_common(data: dict) -> list:
    """Recompute common elements from intersection of all signatures."""
    all_sigs = []
    for screen in data["screens"].values():
        sig = frozenset(tuple(p) for p in screen["signature"])
        all_sigs.append(sig)
    if len(all_sigs) < 2:
        return []
    common = all_sigs[0]
    for s in all_sigs[1:]:
        common = common & s
    return [list(p) for p in sorted(common)]


def learn_screen(platform: str, tree: dict, screen_type: str,
                 behavior_tree: dict = None, source: str = "classification") -> str:
    """
    Store a new screen signature. Returns signature hash.

    Recomputes common elements after every addition.
    """
    sig = extract_signature(tree)
    sig_hash = _sig_hash(sig)
    data = _load_platform(platform)

    if sig_hash in data["screens"]:
        existing = data["screens"][sig_hash]
        if behavior_tree and not existing.get("behavior_tree"):
            existing["behavior_tree"] = behavior_tree
            existing["source"] = source
            _save_platform(platform, data)
            logger.info(f"learn_screen: updated BT for {screen_type} ({sig_hash})")
        return sig_hash

    data["screens"][sig_hash] = {
        "screen_type": screen_type,
        "signature": [list(p) for p in sorted(sig)],
        "behavior_tree": behavior_tree,
        "validated": False,
        "source": source,
    }
    data["common"] = _recompute_common(data)
    _save_platform(platform, data)
    logger.info(f"learn_screen: stored {screen_type} ({sig_hash}), "
                f"{len(sig)} pairs, {len(data['common'])} common")
    return sig_hash


def match_signature(platform: str, tree: dict, category_filter: str = None) -> dict:
    """
    Match a screen against known signatures.

    Args:
        platform: Platform name (e.g., "coursera")
        tree: Accessibility tree dict
        category_filter: If provided, only match against signatures whose master
            category matches this value. Pass "UNCLASSIFIED" or None to match all.
            This is the V19 fix: structural_classify() determines category first,
            then Jaccard only runs within that category. Prevents EXERCISE<->TRANSITION
            false positives entirely.

    Returns:
        {"matched": True, "screen_type": ..., "sig_hash": ..., ...} or
        {"matched": False}
    """
    query_sig = extract_signature(tree)
    data = _load_platform(platform)

    if not data["screens"]:
        return {"matched": False, "signature": [list(p) for p in sorted(query_sig)]}

    common = frozenset(tuple(p) for p in data["common"])

    # With fewer than 2 signatures, common is empty -- every element looks
    # "discriminative" even though most are shared chrome. In this state,
    # only match on exact signature hash. Fuzzy matching is meaningless
    # until we have enough signatures to know what's common vs unique.
    if not common:
        query_hash = _sig_hash(query_sig)
        if query_hash in data["screens"]:
            screen = data["screens"][query_hash]
            result = {
                "matched": True,
                "screen_type": screen["screen_type"],
                "sig_hash": query_hash,
                "match_score": 1.0,
                "validated": screen.get("validated", False),
            }
            if screen.get("behavior_tree"):
                result["tree"] = screen["behavior_tree"]
            return result
        return {"matched": False, "signature": [list(p) for p in sorted(query_sig)]}

    query_markers = query_sig - common

    # V19: Import category helper for filtering
    from spark.tasks.screen_type_util import get_master_category

    best_score = 0.0
    best_hash = None
    best_screen = None
    skipped_cross_category = 0

    for sig_hash, screen in data["screens"].items():
        # V19: Category-constrained matching.
        # If we have a structural pre-classification, only match within that category.
        # This replaces the V18 structural penalty with a hard filter.
        if category_filter and category_filter != "UNCLASSIFIED":
            known_master = get_master_category(screen["screen_type"])
            if known_master != category_filter:
                skipped_cross_category += 1
                continue

        known_sig = frozenset(tuple(p) for p in screen["signature"])
        known_markers = known_sig - common
        if not known_markers:
            continue
        # Jaccard similarity: intersection / union
        # Penalizes BOTH missing markers AND extra markers.
        # A different screen with extra unique elements won't match.
        intersection = len(query_markers & known_markers)
        union = len(query_markers | known_markers)
        score = intersection / union if union > 0 else 0.0

        if score > best_score:
            best_score = score
            best_hash = sig_hash
            best_screen = screen

    if skipped_cross_category > 0:
        logger.info(
            f"  V19: Skipped {skipped_cross_category} cross-category signatures "
            f"(filter={category_filter})"
        )

    if best_score >= 0.7 and best_screen:
        result = {
            "matched": True,
            "screen_type": best_screen["screen_type"],
            "sig_hash": best_hash,
            "match_score": best_score,
            "validated": best_screen.get("validated", False),
        }
        if best_screen.get("behavior_tree"):
            result["tree"] = best_screen["behavior_tree"]
        return result

    return {"matched": False, "signature": [list(p) for p in sorted(query_sig)]}


def delete_screen(platform: str, sig_hash: str):
    """Delete a failed screen entry. Only deletes non-validated entries."""
    data = _load_platform(platform)
    screen = data["screens"].get(sig_hash)
    if not screen:
        return
    if screen.get("validated"):
        logger.warning(f"delete_screen: refusing to delete validated {sig_hash}")
        return
    del data["screens"][sig_hash]
    data["common"] = _recompute_common(data)
    _save_platform(platform, data)
    logger.info(f"delete_screen: removed {sig_hash}")


def mark_validated(platform: str, sig_hash: str):
    """Mark a screen as validated (BT proven to work)."""
    data = _load_platform(platform)
    if sig_hash in data["screens"]:
        data["screens"][sig_hash]["validated"] = True
        _save_platform(platform, data)
        logger.info(f"mark_validated: {sig_hash}")


def get_stats(platform: str = None) -> dict:
    """Get signature stats for health endpoint."""
    SIGNATURES_DIR.mkdir(parents=True, exist_ok=True)
    if platform:
        data = _load_platform(platform)
        return {"platform": platform, "screens": len(data["screens"]),
                "common_elements": len(data["common"])}
    stats = {}
    for f in SIGNATURES_DIR.glob("*.json"):
        p = f.stem
        data = json.loads(f.read_text())
        stats[p] = {"screens": len(data["screens"]),
                     "common_elements": len(data["common"])}
    return stats
