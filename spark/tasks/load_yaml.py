# STATUS: FROZEN - Proven in v7. Verified 2026-02-19. Do not modify.
"""
Load platform YAML configuration with file-mtime caching.
Single-purpose file - FREEZE once working.
"""

import logging
from pathlib import Path
import yaml

from spark.tasks.validate_config import validate_config

logger = logging.getLogger(__name__)


# Platforms directory relative to this file
PLATFORMS_DIR = Path(__file__).parent.parent / "platforms"

# Cache: {platform: (mtime, config_dict)}
_cache: dict[str, tuple[float, dict]] = {}


def load_yaml(platform: str) -> dict:
    """
    Load config for a platform. Caches by file mtime to avoid
    re-parsing YAML on every /match call (~0.5s poll interval).

    Args:
        platform: Platform name (e.g., "acellus")

    Returns:
        Config dict with screens, or empty dict if not found
    """
    config_path = PLATFORMS_DIR / platform / "config.yaml"

    if not config_path.exists():
        _cache.pop(platform, None)
        return {}

    mtime = config_path.stat().st_mtime

    cached = _cache.get(platform)
    if cached and cached[0] == mtime:
        return cached[1]

    with open(config_path) as f:
        config = yaml.safe_load(f) or {}

    # Validate on load - log errors but don't block (agents may be mid-write)
    errors = validate_config(config)
    if errors:
        logger.warning(f"Config validation errors for {platform}: {errors}")

    _cache[platform] = (mtime, config)
    return config
