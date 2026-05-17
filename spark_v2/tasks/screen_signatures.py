"""Signature helpers for spark_v2."""

from __future__ import annotations

import hashlib
import json

from spark_v2.tasks.skeleton import extract_skeleton


def compute_signature(tree: dict) -> str:
    skeleton = extract_skeleton(tree)
    blob = json.dumps(skeleton, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def match_signature(signature: str, stored: dict[str, dict]) -> dict | None:
    return stored.get(signature)
