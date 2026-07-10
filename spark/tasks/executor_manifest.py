"""Versioned Mac executor capability manifest.

This is the server-side data form of dispatches/2026-07-10_executor_manifest.md,
extracted from the running Taey-Ed.app bundle by ccm read-only. The Mac bundle is
the contract source; repo code is only a local approximation.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


MANIFEST_SCHEMA_VERSION = 1
MANIFEST_SOURCE = "dispatches/2026-07-10_executor_manifest.md"
EXTRACTION_DATE = "2026-07-10"
EXTRACTOR = "ccm-readonly-running-bundle"
BUNDLE_SOURCE = (
    "/Users/jesselarose/taey-ed/dist/Taey-Ed.app/Contents/Resources/"
    "lib/python3.12/app/tasks/"
)
BUNDLE_FILES = {
    "bt_core.py": {"mtime": "2026-06-11 11:18"},
    "bt_handlers.py": {"mtime": "2026-06-11 14:27"},
}
PINNED_BUNDLE_HASH = "sha256:3fdf2102343c213dd7e0d05d002054dd953ae8468e164df4769bea6180e7f26f"

NODE_TYPES = ("sequence", "fallback", "action")
COMPOSABLE_ACTIONS = ("conditional", "for_each")
REGISTERED_ACTIONS = (
    "click",
    "click_at",
    "click_element",
    "discover_menu",
    "drag",
    "extract_question",
    "find_all",
    "find_and_click",
    "find_and_type",
    "lookup_match",
    "press_escape",
    "press_key",
    "scroll",
    "select_dropdown_option",
    "send_to_llm",
    "solve_assessment_page",
    "store_qa",
    "type_keys",
    "video_poll",
    "wait",
    "wait_for_element",
)
ACTION_ALIASES = {"click_element": "click"}
ALL_ACTIONS = tuple(sorted(set(COMPOSABLE_ACTIONS) | set(REGISTERED_ACTIONS)))

HANDLER_PARAM_KEYS = {
    "click_at": ("x", "y"),
    "click": ("element", "match_mode", "role", "strategy", "target"),
    "click_element": ("element", "match_mode", "role", "strategy", "target"),
    "discover_menu": ("role",),
    "drag": ("start", "end", "steps", "step_delay", "press_hold", "release_hold"),
    "extract_question": ("question", "options", "text"),
    "find_all": ("role", "description_contains"),
    "find_and_click": ("target", "role", "match_mode", "strategy"),
    "find_and_type": ("target", "element", "role", "focus_strategy", "text"),
    "lookup_match": ("key", "matches", "default"),
    "press_escape": (),
    "press_key": ("key", "modifiers"),
    "scroll": ("direction", "amount"),
    "select_dropdown_option": (),
    "send_to_llm": (
        "question",
        "question_type",
        "options",
        "items",
        "context",
        "image_descriptions",
        "has_text_field",
    ),
    "solve_assessment_page": (),
    "store_qa": ("question", "answer", "question_type"),
    "type_keys": ("text", "per_char_delay"),
    "video_poll": (),
    "wait": ("seconds",),
    "wait_for_element": ("target", "role", "max_wait"),
}

SERVER_EMISSION_CHECKLIST = (
    {
        "id": "M8.1",
        "text": "Every node type is sequence, fallback, action, or omitted.",
    },
    {
        "id": "M8.2",
        "text": "Composables are action-typed, never type=for_each/conditional.",
    },
    {
        "id": "M8.3",
        "text": "Every regular action name is registered in the bundle manifest.",
    },
    {
        "id": "M8.4",
        "text": "Every $ref in params/items/condition conforms to blackboard syntax.",
    },
    {
        "id": "M8.5",
        "text": "store/store_to_current appear only on regular action nodes.",
    },
    {
        "id": "M8.6",
        "text": "for_each.do is one node dict, not a list.",
    },
    {
        "id": "M8.7",
        "text": "conditional.then/else are each one node dict, not lists.",
    },
)


def manifest_payload() -> dict[str, Any]:
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "manifest_source": MANIFEST_SOURCE,
        "extraction_date": EXTRACTION_DATE,
        "extractor": EXTRACTOR,
        "bundle_source": BUNDLE_SOURCE,
        "bundle_files": BUNDLE_FILES,
        "node_types": NODE_TYPES,
        "composable_actions": COMPOSABLE_ACTIONS,
        "registered_actions": REGISTERED_ACTIONS,
        "action_aliases": ACTION_ALIASES,
        "handler_param_keys": HANDLER_PARAM_KEYS,
        "server_emission_checklist": SERVER_EMISSION_CHECKLIST,
    }


def manifest_hash() -> str:
    encoded = json.dumps(
        manifest_payload(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def assert_manifest_integrity() -> None:
    observed = manifest_hash()
    if observed != PINNED_BUNDLE_HASH:
        raise RuntimeError(
            f"executor manifest drift: observed {observed}, pinned {PINNED_BUNDLE_HASH}"
        )


assert_manifest_integrity()

EXECUTOR_MANIFEST = {
    **manifest_payload(),
    "bundle_hash": PINNED_BUNDLE_HASH,
    "bundle_hash_basis": (
        "sha256 over the ccm-extracted executor capability payload; bundle bytes "
        "are not present in this repo, so this pins the observed bundle contract "
        "and mtimes."
    ),
}
