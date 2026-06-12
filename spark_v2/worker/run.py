"""Entry point for the spark_v2 consultation worker."""

from __future__ import annotations

import logging
import logging.handlers
import os

LOG_DIR = os.path.expanduser("~/taey-ed/logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "spark_v2_worker.log")

FORMATTER = logging.Formatter(
    "%(asctime)s %(name)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
file_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE,
    maxBytes=5_000_000,
    backupCount=3,
)
file_handler.setFormatter(FORMATTER)
file_handler.setLevel(logging.INFO)

console_handler = logging.StreamHandler()
console_handler.setFormatter(FORMATTER)
console_handler.setLevel(logging.INFO)

logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler])


if __name__ == "__main__":
    from spark_v2.worker.consultation_worker import run_forever

    run_forever()
