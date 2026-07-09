"""
Filter an accessibility tree down to the relevant info for LLM prompts.

Two problems with sending the raw tree (Jesse 2026-06-13): it is huge (Khan
trees run ~450KB / ~110K tokens — they blow the classifier's cost budget, which
errors out and silently degrades to screen_type=UNKNOWN) AND, paradoxically, it
used to drop `name` — which is exactly where the content-bearing labels live
("Select an answer", "Check", choice text, headings). So the prompt was both
bloated and content-poor.

This filter keeps only the relevant info:
  - KEEP role + name/title/description/value (the labels + text the LLM reads)
    + a few answer-widget state fields (value/selected/enabled).
  - DROP coordinates / ids (element_id, position, size, visible_bbox) — the Mac
    resolves actions by name+role, never coordinates.
  - COLLAPSE contentless structural wrappers (AXGroup/AXGeneric/... with no
    name/title/description/value) by hoisting their children. Khan trees are
    ~50% empty wrapper groups; flattening them is the bulk of the size win and
    loses nothing the LLM needs.

The original tree is NOT modified — returns a new dict. NOT truncation: no
content is dropped, only coordinate noise and empty structural nesting.

Platform-agnostic — capture_tree.py produces identical field sets regardless
of platform.
"""

import logging

logger = logging.getLogger("taey-ed")

# Id / redundant-coordinate fields the LLM never uses. visible_bbox is KEPT:
# the dropdown solver distinguishes identical "Select an answer" comboboxes by
# POSITION (bbox-center click_at) + preceding label — dropping it (my 2026-06-13
# mistake) forced by-name targeting, which can't tell the boxes apart. position
# and size are redundant with visible_bbox.
_DROP_FIELDS = {"element_id", "position", "size"}

# CLASSIFIER-ONLY tight allowlist (Jesse 2026-06-15, "something very broken").
# The classifier path (prune_tree_for_prompt) is a DENYLIST that leaked ~27
# AX-internal noise fields per node (startTextMarker, endTextMarker,
# selectedTextRange, visibleCharacterRange, frame, visible_bbox, ChromeAXNodeId,
# dOMIdentifier, insertionPointLineNumber, numberOfCharacters, ...). On a Khan
# sorter that bloated 119 nodes / 3.3KB of real content to 138KB / ~34.5K tokens
# -> the classifier STARVED its cost budget -> silent screen_type=UNKNOWN ->
# worker freelance. The classifier reads role+name+structure (never coordinates),
# so it gets ONLY these fields. An allowlist cannot be bypassed by a new noise
# field. This is CLASSIFICATION-only — the WORKER path (filter_tree_base) keeps
# the denylist because its solver needs visible_bbox for click_at/drag.
_CLASSIFIER_KEEP_FIELDS = {
    "role",            # the structural signal
    "name", "title", "description", "value",  # content: questions, labels, choice text
    "dOMClassList",    # widget identity (e.g. 'perseus-sortable') — strong classify signal
    "roleDescription", # e.g. 'image' / 'button' — small, useful
    "selected", "enabled",  # answer-widget state
}

# Fields whose non-empty presence means a node carries content worth keeping.
_CONTENT_FIELDS = ("name", "title", "description", "value")

# Roles that are pure structural containers — collapse them when they carry no
# content of their own (hoist their children into the parent).
_STRUCTURAL_ROLES = {
    "AXGroup", "AXGeneric", "AXScrollArea", "AXSplitGroup", "AXSplitter",
    "AXLayoutArea", "AXLayoutItem", "AXUnknown", "AXEmptyGroup",
}


def _has_content(node: dict) -> bool:
    for key in _CONTENT_FIELDS:
        if str(node.get(key) or "").strip():
            return True
    return False


# CLASSIFIER-ONLY sibling-run collapse (2026-07-09, live hs-bio baseline run):
# even the allowlist output of a Khan COURSE DASHBOARD is ~281K chars / ~70K
# tokens — the mastery map is 57 near-identical sibling AXLists (AXImage+AXLink
# rows, 3-6K chars each) plus 9-17-long AXLink runs inside unit lists. That
# blew the classify budget (error_max_budget_usd at ~149K cache tokens) ->
# empty result -> silent UNKNOWN -> guide-mode freelance click_at -> App Store
# badge -> wrong-window loop. Classification needs SIGNALS (which roles are
# present, exemplar names, how many), never every skill cell. So: consecutive
# siblings with the SAME structural fingerprint (role + child-role multiset)
# in runs longer than _RUN_COLLAPSE_THRESHOLD keep the first
# _RUN_KEEP_HEAD + last _RUN_KEEP_TAIL exemplars and the elided middle is
# replaced by ONE explicit annotation node stating exactly what was filtered
# (filtering with a receipt — never a silent cut; REQUIREMENTS.md C9/R7.7).
# The WORKER path (filter_tree_base) is UNTOUCHED — navigate needs every link.
_RUN_COLLAPSE_THRESHOLD = 6
_RUN_KEEP_HEAD = 3
_RUN_KEEP_TAIL = 1


def _run_fingerprint(node: dict):
    # Role + the SET of child roles (not counts — a 6-row and an 11-row
    # mastery list are the same structural class). A node with a unique
    # structure is a singleton class and can NEVER be elided.
    child_roles = frozenset(
        str(c.get("role")) for c in node.get("children", []) if isinstance(c, dict)
    )
    return (node.get("role"), child_roles)


def _collapse_sibling_runs(children: list) -> list:
    """Class-based sibling collapse: group siblings by structural fingerprint
    (role + child-role set); for any class with more than the threshold of
    members among these siblings, keep the first _RUN_KEEP_HEAD and last
    _RUN_KEEP_TAIL occurrences IN PLACE (order preserved, wherever they occur —
    Khan interleaves heading/list/heading/list so consecutive-run detection
    misses the repetition) and replace the first elided member with ONE
    explicit annotation node for that class. Never elides annotation nodes,
    never elides singleton/unique structures."""
    from collections import Counter

    fps = [_run_fingerprint(c) for c in children]
    counts = Counter(fps)
    big = {fp for fp, n in counts.items() if n > _RUN_COLLAPSE_THRESHOLD}
    if not big:
        return children

    keep_idx: dict = {}   # fp -> set of kept indices
    for fp in big:
        idxs = [i for i, f in enumerate(fps) if f == fp]
        keep_idx[fp] = set(idxs[:_RUN_KEEP_HEAD] + idxs[-_RUN_KEEP_TAIL:])

    out: list = []
    annotated: set = set()
    for i, (child, fp) in enumerate(zip(children, fps)):
        if fp not in big or i in keep_idx[fp]:
            out.append(child)
            continue
        if fp not in annotated:
            annotated.add(fp)
            elided = counts[fp] - _RUN_KEEP_HEAD - _RUN_KEEP_TAIL
            out.append({
                "role": child.get("role"),
                "name": (
                    f"[FILTERED FOR CLASSIFICATION: {elided} more "
                    f"{child.get('role')} siblings of this same structural "
                    f"class ({counts[fp]} total among these siblings) elided; "
                    f"first {_RUN_KEEP_HEAD} and last {_RUN_KEEP_TAIL} kept in "
                    f"place as exemplars. The solver path receives the full "
                    f"tree.]"
                ),
            })
    return out


def prune_tree_for_prompt(tree: dict) -> dict:
    """Return a filtered copy of the tree for LLM prompt inclusion.

    Keeps content (role + name/title/description/value), drops coordinate noise,
    collapses contentless structural wrappers, and collapses long runs of
    structurally-identical siblings behind explicit annotations. The root node
    itself is never collapsed. Uses the CLASSIFIER tight allowlist — coordinates
    and AX-internal noise are not needed to classify (the LLM reads
    role+name+structure). CLASSIFIER-ONLY: the worker path is filter_tree_base.
    """
    return _prune_node(tree, keep_only=_CLASSIFIER_KEEP_FIELDS, collapse_runs=True)


def filter_tree_base(tree: dict) -> dict:
    """THE default base filter for EVERY tree sent to an LLM — classifier AND
    worker. Two-stage design (Jesse, discussed many times):

      1. Base (here, always): scope to the page content (AXWebArea), dropping
         ALL browser chrome (toolbar, tabs, extensions, address bar, "View
         progress"/"Share"/"Tab Search" popups...), then keep content + collapse
         empty structural wrappers + drop coordinate noise.
      2. Per-screen: each screen-type YAML narrows further to just that screen's
         relevant elements.

    Chrome chrome is NEVER relevant to solving a course screen and is pure
    bloat/confusion. This is filtering, NOT truncation — no content is dropped,
    and (unlike the old _sanitize_tree_for_worker) values are never clipped.
    """
    from spark.tasks.prompt_codex import _find_web_area

    web = _find_web_area(tree) or tree
    return _prune_node(web)


def _prune_node(node: dict, keep_only: set | None = None, collapse_runs: bool = False) -> dict:
    """Prune a single node recursively, collapsing contentless wrappers among
    its children. If keep_only is given, keep ONLY those fields (allowlist, used
    by the classifier path); otherwise drop _DROP_FIELDS (denylist, worker path
    which still needs visible_bbox for click_at/drag). collapse_runs additionally
    collapses long structurally-identical sibling runs (classifier path only)."""
    pruned = {}

    for key, val in node.items():
        if keep_only is not None:
            if key not in keep_only and key != "children":
                continue
        elif key in _DROP_FIELDS:
            continue

        if key == "children" and isinstance(val, list):
            collapsed: list = []
            for child in val:
                if not isinstance(child, dict):
                    continue
                pruned_child = _prune_node(child, keep_only=keep_only, collapse_runs=collapse_runs)
                # Collapse a contentless structural wrapper: splice its (already
                # pruned) children in place of the wrapper itself.
                if (
                    child.get("role") in _STRUCTURAL_ROLES
                    and not _has_content(child)
                ):
                    collapsed.extend(pruned_child.get("children", []))
                else:
                    collapsed.append(pruned_child)
            if collapsed and collapse_runs:
                collapsed = _collapse_sibling_runs(collapsed)
            if collapsed:
                pruned["children"] = collapsed
            continue

        if isinstance(val, str) and val == "":
            continue

        pruned[key] = val

    return pruned
