"""Skeleton extraction for spark_v2."""

from __future__ import annotations

import hashlib
import json


def extract_skeleton(tree: dict) -> dict:
    # TODO Phase C6: refine scaffold extraction to the canonical skeleton model.
    def visit(node: object) -> object:
        if isinstance(node, dict):
            keep = {}
            for key in ("role", "name", "title", "description", "children"):
                if key not in node:
                    continue
                if key == "children":
                    keep[key] = [visit(child) for child in node.get("children", [])]
                else:
                    keep[key] = node.get(key)
            return keep
        if isinstance(node, list):
            return [visit(item) for item in node]
        return node

    return visit(tree)


def hash_skeleton(skeleton: dict) -> str:
    # TODO Phase C6: align hash semantics with the signature/cache layer.
    blob = json.dumps(skeleton, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
