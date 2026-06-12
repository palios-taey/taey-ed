"""Atomic JSON writes for spark_v2."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def atomic_write_json(path: str | Path, data: dict, indent: int = 2) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        dir=str(destination.parent),
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=indent)
            handle.write("\n")
        os.replace(temp_path, destination)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
