"""
Screen recognition via set-difference discriminative markers.

Ported from V20 (commit 92d5ed0:spark/tasks/screen_signatures.py) to the
current Mira-hosted spark/ layout, 2026-05-19. Paths adapted from
/var/spark/taey-ed/signatures/ to /home/user/taey-ed-data/signatures/.
Imports remain compatible with current knowledge_loader.py
(get_knowledge_version already exists there).

Architecture (V20 era, retained intentionally):

  Walk accessibility tree, extract (role, text) pairs by role type:
    - Stable-text roles (developer-set labels): include (role, name)
    - Structural-presence roles (presence matters, text is variable): include (role, "*")
    - Variable-content roles: skip entirely

  Screen signature = frozenset of these tuples.
  Common elements = intersection of ALL known signatures for this platform
                    (platform chrome — sidebar, header, footer, breadcrumbs).
  Discriminative markers = signature - common.
  Matching = Jaccard similarity on discriminative markers, threshold 0.70.

  Cold start (< 2 known signatures): common is empty, every element looks
  discriminative. Fall back to exact signature hash match only — fuzzy
  matching is meaningless until enough signatures exist to know what's
  shared vs unique.

Why V20 deliberately removed V19's structural_classify() + category_filter:
hardcoded count thresholds (radio >= 3, checkbox >= 3, links >= 15)
misclassified edge cases like true/false questions with 2 radios. Per Jesse
2026-05-19: structural categorization via role-presence is fine; hardcoded
COUNT thresholds are dynamic-content-coupled and brittle. V20 trusts the
Jaccard over discriminative markers without an upstream category gate.
"""

import hashlib
import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SIGNATURES_DIR = Path("/home/user/taey-ed-data/signatures")

# Roles with stable developer-set text labels — text contributes to the
# signature. The same "Check" / "Submit" / "Next" button on every visit
# of a screen IS the screen's identity.
STABLE_TEXT_ROLES = {
    "AXButton", "AXTab", "AXMenuItem", "AXMenuButton",
    "AXPopUpButton", "AXComboBox", "AXDisclosureTriangle",
    "AXToolbar", "AXTabGroup",
}

# Roles where presence is structural but text is user content. The fact that
# an AXRadioButton is on the screen is the signal; its name ("Apple" /
# "Banana") is question-specific dynamic content that MUST NOT enter the
# signature.
STRUCTURAL_PRESENCE_ROLES = {
    "AXRadioButton", "AXCheckBox", "AXTextField",
    "AXTextArea", "AXForm", "AXSlider",
}


def extract_signature(tree: dict) -> frozenset:
    """Walk tree, extract (role, text) pairs for discriminative matching.

    Stable-text roles contribute (role, name) tuples.
    Structural-presence roles contribute (role, "*") tuples — one per role
    type, regardless of how many instances exist on the screen.
    All other roles are skipped (variable text content, layout containers,
    chrome with non-stable labels).
    """
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
    """Recompute common elements from intersection of all signatures.

    Platform chrome (sidebar, header, footer) appears in every screen, so it
    falls out of discriminative markers automatically as more screens are
    learned. With fewer than 2 signatures, common stays empty and we use
    exact-hash fallback in match_signature.
    """
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
                 behavior_tree: Optional[dict] = None,
                 extract: Optional[dict] = None,
                 source: str = "classification") -> str:
    """Store a new screen signature. Returns the signature hash.

    Recomputes common elements after every addition.
    Stores knowledge_version (from knowledge_loader) so cached BTs are
    deterministically invalidated when knowledge.json changes.

    `source` records how this entry was learned — values in V20 production:
        "classification" — Gemini classified + BT built (now Claude CLI)
        "claude_diagnosis" — claude-primary defined it after escalation
        "manual"          — operator hand-added
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
    """Check if a stored BT should be invalidated due to knowledge.json changes.

    Returns True if the stored BT was invalidated (caller should skip it).
    When knowledge changes, cached BTs from before the change may reflect
    out-of-date operational guidance; safer to rebuild via the worker.
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
    """Match a screen against known signatures.

    Per Jesse 2026-05-19: strict set-equality on discriminative markers (no Jaccard).
    A screen either matches an existing signature exactly (after filtering platform
    chrome) or it doesn't. Fuzzy matching let new variants get silently absorbed
    into wrong existing ones.

    Returns:
        Unique match (exactly 1 entry has identical discriminative markers):
            {
                "matched": True,
                "screen_type": <stored screen_type>,
                "sig_hash": <stored hash>,
                "match_score": 1.0,
                "validated": bool,
                "tree": <stored BT if present>,
                "extract": <stored extract config if present>,
            }
        Ambiguous match (2+ entries have identical discriminative markers):
            {
                "matched": False,
                "ambiguous": True,
                "candidates": [
                    {"sig_hash": ..., "screen_type": ..., "validated": ...},
                    ...
                ],
                "signature": [<pairs>],
            }
        No match:
            {"matched": False, "signature": [<pairs>]}
    """
    query_sig = extract_signature(tree)
    data = _load_platform(platform)

    if not data["screens"]:
        return {"matched": False, "signature": [list(p) for p in sorted(query_sig)]}

    common = frozenset(tuple(p) for p in data["common"])

    # Cold-start fallback: with fewer than 2 signatures, common is empty and
    # discriminative-marker matching is meaningless. Fall back to exact
    # signature-hash equality (still strict, just on the full signature).
    if not common:
        query_hash = _sig_hash(query_sig)
        if query_hash in data["screens"]:
            screen = data["screens"][query_hash]
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

    # Strict set-equality match: collect every entry whose discriminative
    # markers exactly equal the query's. Then decide on 0 / 1 / 2+.
    matches = []
    for sig_hash, screen in data["screens"].items():
        known_sig = frozenset(tuple(p) for p in screen["signature"])
        known_markers = known_sig - common
        if known_markers == query_markers:
            matches.append((sig_hash, screen))

    if len(matches) == 1:
        sig_hash, screen = matches[0]
        _check_knowledge_invalidation(platform, screen, sig_hash, data)
        result = {
            "matched": True,
            "screen_type": screen["screen_type"],
            "sig_hash": sig_hash,
            "match_score": 1.0,
            "validated": screen.get("validated", False),
        }
        if screen.get("behavior_tree"):
            result["tree"] = screen["behavior_tree"]
        if screen.get("extract"):
            result["extract"] = screen["extract"]
        return result

    if len(matches) >= 2:
        # Ambiguity: 2+ registered signatures share the same discriminative
        # markers. Surface to claude-primary so a discriminator can be added.
        logger.warning(
            f"match_signature: AMBIGUOUS — {len(matches)} candidates share "
            f"the same discriminative markers on {platform}: "
            f"{[s['screen_type'] for _, s in matches]}"
        )
        return {
            "matched": False,
            "ambiguous": True,
            "candidates": [
                {
                    "sig_hash": h,
                    "screen_type": s["screen_type"],
                    "validated": s.get("validated", False),
                }
                for h, s in matches
            ],
            "signature": [list(p) for p in sorted(query_sig)],
            "shared_markers": [list(p) for p in sorted(query_markers)],
        }

    return {"matched": False, "signature": [list(p) for p in sorted(query_sig)]}


def delete_screen(platform: str, sig_hash: str):
    """Delete a failed screen entry. Only deletes non-validated entries.

    Validated entries (BT proven to work) are protected from deletion to
    prevent recurring failures from wiping known-good signatures.
    """
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
    """Mark a screen as validated (BT proven to work on real Mac execution)."""
    data = _load_platform(platform)
    if sig_hash in data["screens"]:
        data["screens"][sig_hash]["validated"] = True
        _save_platform(platform, data)
        logger.info(f"mark_validated: {sig_hash}")


def get_stats(platform: Optional[str] = None) -> dict:
    """Get signature stats for health endpoint / debugging."""
    SIGNATURES_DIR.mkdir(parents=True, exist_ok=True)
    if platform:
        data = _load_platform(platform)
        return {
            "platform": platform,
            "screens": len(data["screens"]),
            "common_elements": len(data["common"]),
        }
    stats = {}
    for f in SIGNATURES_DIR.glob("*.json"):
        p = f.stem
        data = json.loads(f.read_text())
        stats[p] = {
            "screens": len(data["screens"]),
            "common_elements": len(data["common"]),
        }
    return stats
