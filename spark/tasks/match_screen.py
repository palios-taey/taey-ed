"""
Match accessibility tree against screen definitions.

V17: Set-difference discriminative matching. No Weaviate.
V20: Removed structural pre-classification. Gemini classifies unmatched screens.

Extract (role, text) signatures -> subtract common chrome -> match on
discriminative markers. JSON file storage per platform.

V20 change: Removed structural_classify() and category_filter entirely.
structural_classify() used hardcoded count thresholds in analyze_tree()
(radio >= 3, checkbox >= 3, links >= 15) that misclassified screens like
true/false questions. Per REQUIREMENTS.md: "Gemini sees the screen.
Gemini decides. The code just routes." Jaccard matching now runs against
ALL known signatures without filtering.
"""

import logging

logger = logging.getLogger(__name__)


def match_screen(tree: dict, config: dict) -> dict:
    """
    Match tree against known screen signatures using set-difference.

    V20: No structural pre-classification. Jaccard matching runs against
    ALL known signatures. Classification of unmatched screens is always
    done by Gemini via classify_screen().

    Args:
        tree: Accessibility tree dict from Mac
        config: Platform config dict (must have 'platform' key)

    Returns:
        {"matched": True, "screen": name, "screen_type": ..., "tree": {...}, ...} or
        {"matched": False, "needs_consultation": True}
    """
    platform = config.get("platform", "")
    if not platform:
        logger.error("match_screen: config missing 'platform' key -- cannot route")
        return {"matched": False, "needs_consultation": True, "error": "missing_platform"}

    from spark.tasks.screen_signatures import match_signature
    result = match_signature(platform, tree)

    if result.get("matched"):
        logger.info(f"Signature match: {result['screen_type']} "
                     f"(score={result['match_score']:.2f}, hash={result['sig_hash']})")
        return {
            "matched": True,
            "screen": result["screen_type"],
            "screen_type": result["screen_type"],
            "match_source": "signature",
            "sig_hash": result["sig_hash"],
            "match_score": result["match_score"],
            "validated": result.get("validated", False),
            "tree": result.get("tree"),
        }

    logger.info(f"No signature match for {platform}")
    return {
        "matched": False,
        "needs_consultation": True,
    }
