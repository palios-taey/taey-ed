# STATUS: FROZEN. Verified 2026-02-19. Do not modify.
"""
Atomic file write utility.

Writes to a temp file in the same directory, then renames.
Prevents partial reads when Mac polls every 0.5s.
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Union


def atomic_write_text(path: Union[Path, str], content: str) -> None:
    """Write text atomically via temp file + rename."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        os.write(fd, content.encode())
        os.close(fd)
        os.rename(tmp, path)
    except Exception:
        os.close(fd) if not os.get_inheritable(fd) else None
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def atomic_write_json(path: Union[Path, str], data: dict, indent: int = 2) -> None:
    """Write JSON atomically."""
    atomic_write_text(path, json.dumps(data, indent=indent))
