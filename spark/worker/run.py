"""Entry point to run the consultation worker as a standalone process.

Usage:
    python -m spark.worker.run

In production this runs under systemd or supervisor; in DEV Jesse can run
it directly. Logs go to stdout + the same rotated file as the API server.
"""

import logging
import logging.handlers
import os

# Configure logging matching server.py format
_LOG_DIR = os.path.expanduser("~/taey-ed/logs")
os.makedirs(_LOG_DIR, exist_ok=True)
_LOG_FILE = os.path.join(_LOG_DIR, "worker.log")

_fmt = logging.Formatter(
    "%(asctime)s %(name)s %(levelname)s: %(message)s", datefmt="%H:%M:%S"
)
_fh = logging.handlers.RotatingFileHandler(
    _LOG_FILE, maxBytes=5_000_000, backupCount=3
)
_fh.setFormatter(_fmt)
_fh.setLevel(logging.INFO)
_ch = logging.StreamHandler()
_ch.setFormatter(_fmt)
_ch.setLevel(logging.INFO)
logging.basicConfig(level=logging.INFO, handlers=[_fh, _ch])


if __name__ == "__main__":
    from spark.worker.consultation_worker import run_forever
    run_forever()
