"""Mira-side runtime paths and config for taey-ed.

Single source of truth for paths/ports/secrets so the server can be moved
without grepping the codebase. Persistent data paths are env-owned; tools must
fail loudly when the data root is not provided. The /var/spark/* paths from the
original Spark deployment are deprecated and not referenced anywhere else in
code.

Env overrides:
  TAEY_ED_DATA_DIR     persistent data root (required)
  TAEY_ED_SECRETS_PATH path to API secrets json (default /etc/taey-ed/secrets.json)
  TAEY_ED_PORT         server port (default 5003)
  TAEY_ED_HOST         server host used in self-referencing prompts (default 127.0.0.1)
"""

import base64
import os
from pathlib import Path


def _resolve_data_dir() -> Path:
    raw = os.environ.get("TAEY_ED_DATA_DIR")
    if raw:
        return Path(raw).expanduser()
    raise RuntimeError(
        "TAEY_ED_DATA_DIR is required; refusing to use an implicit runtime data root"
    )


# Persistent server state (signatures, variant BTs, hash index, fingerprint log).
DATA_DIR = _resolve_data_dir()

SIGNATURES_DIR = DATA_DIR / "signatures"
VARIANT_BTS_DIR = DATA_DIR / "variant_bts"
HASH_INDEX_DIR = DATA_DIR / "hash_index"
FINGERPRINT_LOG_DIR = DATA_DIR / "fingerprint_log"
CONSULTATIONS_DIR = DATA_DIR / "consultations"
ESCALATIONS_DIR = CONSULTATIONS_DIR / "ESCALATIONS"
REVIEWS_DIR = CONSULTATIONS_DIR / "REVIEWS"
UNSOLVED_LOG = CONSULTATIONS_DIR / "UNSOLVED.md"

# API secrets file (Gemini key + others). Resolved as Path so callers can .exists().
# Provide the real path via TAEY_ED_SECRETS_PATH in deployment (systemd drop-in);
# the default is a generic location, not an operator path.
SECRETS_PATH = Path(os.environ.get(
    "TAEY_ED_SECRETS_PATH",
    "/etc/taey-ed/secrets.json",
))

# Server runtime (used in prompts that tell consulting Claude where to POST back).
SERVER_HOST = os.environ.get("TAEY_ED_HOST", "127.0.0.1")
SERVER_PORT = int(os.environ.get("TAEY_ED_PORT", "5003"))
SERVER_BASE_URL = f"http://{SERVER_HOST}:{SERVER_PORT}"

# Repo root and platforms dir, derived from this file's location.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PLATFORMS_DIR = REPO_ROOT / "spark" / "platforms"


def ensure_data_dirs() -> None:
    """Create all persistent-data subdirs if they don't exist (idempotent)."""
    for d in (
        SIGNATURES_DIR,
        VARIANT_BTS_DIR,
        HASH_INDEX_DIR,
        FINGERPRINT_LOG_DIR,
        CONSULTATIONS_DIR,
        ESCALATIONS_DIR,
        REVIEWS_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)


# 8-byte PNG signature: \x89PNG\r\n\x1a\n
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def is_valid_png_b64(s: str | None) -> bool:
    """True iff `s` is a base64 string that decodes to bytes starting with PNG magic.

    Used to gate consultation creation against test/stale/empty payloads. The
    previous bogus consult had `screenshot_b64="test"` from a unit test — that
    decodes to 3 random bytes; this check rejects it.
    """
    if not s:
        return False
    try:
        decoded = base64.b64decode(s, validate=True)
    except Exception:
        return False
    return len(decoded) >= len(PNG_MAGIC) and decoded[: len(PNG_MAGIC)] == PNG_MAGIC
