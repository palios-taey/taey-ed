"""Provenance hashing for cached BT promotion."""

from __future__ import annotations

import hashlib
import json


def compute_provenance_hash(consult_ids: list[str]) -> str:
    return hashlib.sha256(
        json.dumps(sorted(consult_ids), separators=(",", ":")).encode()
    ).hexdigest()
