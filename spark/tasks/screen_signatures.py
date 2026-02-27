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

V20 fix (2026-02-27): Removed structural_classify() and category_filter.
structural_classify() used analyze_tree() which had hardcoded count thresholds
(radio >= 3, checkbox >= 3, links >= 15) that caused misclassification of
true/false questions and other edge cases. Per REQUIREMENTS.md: "Gemini sees
the screen. Gemini decides. The code just routes." Jaccard matching now runs
against ALL signatures without category filtering. Classification of unmatched
screens is always done by Gemini.
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

# V20: structural_classify() REMOVED. It used analyze_tree() which had
# hardcoded count thresholds that misclassified screens (e.g., true/false
# questions with 2 radio buttons). All classification of unmatched screens
# is now done by Gemini. The code just routes.


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
                 behavior_tree: dict = None, extract: dict = None,
                 source: str = "classification") -> str:
    """
    Store a new screen signature. Returns signature hash.

    Recomputes common elements after every addition.
    Stores knowledge_version for deterministic BT cache invalidation.
    """
    from spark.tasks.knowledge_loader import get_knowledge_version

    sig = extract_signature(tree)
    sig_hash = _sig_hash(sig)
    data = _load_platform(platform)

    if sig_hash in data["screens"]:
        existing = data["screens"][sig_hash]
        updated = False
        if behavior_tree and not existing.get("behavior_tree"):
            existing["behavior_tree"] = behavior_tree
            existing["source"] = source
            existing["knowledge_version"] = get_knowledge_version(platform)
            updated = True
        if extract and not existing.get("extract"):
            existing["extract"] = extract
            updated = True
        if updated:
            _save_platform(platform, data)
            logger.info(f"learn_screen: updated entry for {screen_type} ({sig_hash})")
        return sig_hash

    data["screens"][sig_hash] = {
        "screen_type": screen_type,
        "signature": [list(p) for p in sorted(sig)],
        "behavior_tree": behavior_tree,
        "extract": extract,
        "validated": False,
        "source": source,
        "knowledge_version": get_knowledge_version(platform),
    }
    data["common"] = _recompute_common(data)
    _save_platform(platform, data)
    logger.info(f"learn_screen: stored {screen_type} ({sig_hash}), "
                f"{len(sig)} pairs, {len(data['common'])} common")
    return sig_hash


def _check_knowledge_invalidation(platform: str, screen: dict, sig_hash: str,
                                   data: dict) -> bool:
    """
    Check if a stored BT should be invalidated due to knowledge.json changes.
    Returns True if the stored BT was invalidated (caller should skip it).
    """
    stored_version = screen.get("knowledge_version")
    if not stored_version:
        return False
    if not screen.get("behavior_tree"):
        return False

    from spark.tasks.knowledge_loader import get_knowledge_version
    current_version = get_knowledge_version(platform)
    if not current_version:
        return False

    if stored_version != current_version:
        logger.info(
            f"Invalidating cached BT for {sig_hash[:12]}: "
            f"knowledge changed ({stored_version} → {current_version})"
        )
        screen["behavior_tree"] = None
        screen["knowledge_version"] = None
        _save_platform(platform, data)
        return True

    return False


def match_signature(platform: str, tree: dict) -> dict:
    """
    Match a screen against known signatures.

    V20: Removed category_filter. Jaccard matching runs against ALL known
    signatures. The structural elements (radio buttons, checkboxes) are
    already part of the signature set and contribute to Jaccard scoring,
    so screens with different structural elements naturally score lower.
    Classification of unmatched screens is done by Gemini, not code.

    Args:
        platform: Platform name (e.g., "coursera")
        tree: Accessibility tree dict

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
            # Invalidate cached BT if knowledge changed
            _check_knowledge_invalidation(platform, screen, query_hash, data)
            result = {
                "matched": True,
                "screen_type": screen["screen_type"],
                "sig_hash": query_hash,
                "match_score": 1.0,
                "validated": screen.get("validated", False),
            }
            if screen.get("behavior_tree"):
                result["tree"] = screen["behavior_tree"]
            if screen.get("extract"):
                result["extract"] = screen["extract"]
            return result
        return {"matched": False, "signature": [list(p) for p in sorted(query_sig)]}

    query_markers = query_sig - common

    best_score = 0.0
    best_hash = None
    best_screen = None

    for sig_hash, screen in data["screens"].items():
        known_sig = frozenset(tuple(p) for p in screen["signature"])
        known_markers = known_sig - common
        if not known_markers:
            continue
        # Jaccard similarity: intersection / union
        intersection = len(query_markers & known_markers)
        union = len(query_markers | known_markers)
        score = intersection / union if union > 0 else 0.0

        if score > best_score:
            best_score = score
            best_hash = sig_hash
            best_screen = screen

    if best_score >= 0.7 and best_screen:
        # Invalidate cached BT if knowledge changed
        _check_knowledge_invalidation(platform, best_screen, best_hash, data)
        result = {
            "matched": True,
            "screen_type": best_screen["screen_type"],
            "sig_hash": best_hash,
            "match_score": best_score,
            "validated": best_screen.get("validated", False),
        }
        if best_screen.get("behavior_tree"):
            result["tree"] = best_screen["behavior_tree"]
        if best_screen.get("extract"):
            result["extract"] = best_screen["extract"]
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
