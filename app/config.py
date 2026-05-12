"""
Taey-Ed Configuration - Externalized settings for distribution.

Config precedence (highest to lowest):
  1. Environment variables (TAEY_ED_API_KEY, TAEY_ED_SPARK_URL)
  2. User config file (~/.taey-ed/config.json)
  3. Built-in defaults (development values)

For distributed builds, users create ~/.taey-ed/config.json:
  {
    "spark_url": "http://your-spark-server:5002",
    "api_key": "your-api-key"
  }
"""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("taey-ed")

# User config directory
CONFIG_DIR = Path.home() / ".taey-ed"
CONFIG_FILE = CONFIG_DIR / "config.json"

# Defaults — production endpoint via Cloudflare Tunnel.
# A fresh install with no ~/.taey-ed/config.json hits the public URL.
# Auth is Bearer JWT obtained via the in-app login flow; api_key is no
# longer required on the user path (kept as empty fallback for transitional
# non-user endpoints).
_DEFAULTS = {
    "spark_url": "https://taey-ed-api.taey.ai",
    "api_key": "",
}

_config_cache = None


def _load_config() -> dict:
    """Load config from file, merge with defaults."""
    global _config_cache
    if _config_cache is not None:
        return _config_cache

    config = dict(_DEFAULTS)

    # Layer 2: User config file
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f:
                user_config = json.load(f)
            config.update({k: v for k, v in user_config.items() if v})
            logger.info(f"Loaded config from {CONFIG_FILE}")
        except Exception as e:
            logger.warning(f"Could not read {CONFIG_FILE}: {e}")

    # Layer 1: Environment variables (highest priority)
    env_url = os.environ.get("TAEY_ED_SPARK_URL")
    if env_url:
        config["spark_url"] = env_url

    env_key = os.environ.get("TAEY_ED_API_KEY")
    if env_key:
        config["api_key"] = env_key

    _config_cache = config
    return config


def get_spark_url() -> str:
    """Get Spark server URL."""
    return _load_config()["spark_url"]


def get_api_key() -> str:
    """Get API key."""
    return _load_config()["api_key"]


def ensure_config_dir():
    """Create config directory if it doesn't exist."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def save_config(spark_url: str = None, api_key: str = None):
    """Save config to user config file."""
    ensure_config_dir()

    # Load existing
    existing = {}
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f:
                existing = json.load(f)
        except Exception:
            pass

    # Update
    if spark_url is not None:
        existing["spark_url"] = spark_url
    if api_key is not None:
        existing["api_key"] = api_key

    with open(CONFIG_FILE, "w") as f:
        json.dump(existing, f, indent=2)

    # Invalidate cache
    global _config_cache
    _config_cache = None
    logger.info(f"Config saved to {CONFIG_FILE}")


def is_configured() -> bool:
    """Check if the app has a valid configuration (API key set)."""
    config = _load_config()
    return bool(config.get("api_key"))
