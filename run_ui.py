#!/usr/bin/env python3
"""
Taey-Ed - Application Entry Point

CRITICAL: This file MUST be at project root for py2app accessibility to work.
Entry points inside packages (e.g., app/main.py) fail with AX error -25211.

Single-instance lock at /tmp/taey-ed.pid prevents multiple Taey-Ed processes
running in parallel and double-polling the server. On startup: if lockfile exists and PID is alive → refuse to
start. Else write current PID; atexit cleans up.
"""

import atexit
import logging
import os
import sys

LOCK_FILE = "/tmp/taey-ed.pid"

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("taey-ed")


def _process_alive(pid: int) -> bool:
    """Check if a PID is still running. Doesn't actually kill."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except Exception:
        return False


def _acquire_lock() -> bool:
    """Acquire single-instance lock. Returns True if acquired, False if another instance is alive."""
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE) as f:
                existing_pid = int(f.read().strip())
            if _process_alive(existing_pid):
                logger.error(
                    f"Another Taey-Ed instance is already running (PID {existing_pid}). "
                    f"Quit it first, or remove {LOCK_FILE} if it's stale."
                )
                return False
            else:
                logger.info(f"Removing stale lockfile (PID {existing_pid} is dead)")
                os.unlink(LOCK_FILE)
        except (ValueError, OSError) as e:
            logger.warning(f"Lockfile {LOCK_FILE} unreadable, treating as stale: {e}")
            try:
                os.unlink(LOCK_FILE)
            except OSError:
                pass

    try:
        with open(LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))
    except OSError as e:
        logger.error(f"Could not write lockfile {LOCK_FILE}: {e}")
        return False

    atexit.register(_release_lock)
    return True


def _release_lock():
    """Remove the lockfile if it points at this process."""
    try:
        if os.path.exists(LOCK_FILE):
            with open(LOCK_FILE) as f:
                pid = int(f.read().strip())
            if pid == os.getpid():
                os.unlink(LOCK_FILE)
    except Exception:
        pass


from app.ui.window import TaeyEdWindow


def main():
    """Main entry point."""
    if not _acquire_lock():
        sys.exit(1)
    window = TaeyEdWindow()
    window.run()


if __name__ == "__main__":
    main()
