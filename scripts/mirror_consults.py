#!/usr/bin/env python3
import shutil
import signal
import sys
import time
from pathlib import Path


SRC_ROOT = Path("/tmp/taey-ed-consult")
DEST_ROOT = Path("/home/user/taey-ed/runtime/consults")
POLL_INTERVAL_S = 2.0
running = True


def _handle_signal(signum, _frame):
    global running
    running = False
    print(f"mirror_consults: stopping on signal {signum}", file=sys.stderr)


def _latest_mtime(path: Path) -> float:
    latest = path.stat().st_mtime
    for child in path.rglob("*"):
        try:
            child_mtime = child.stat().st_mtime
        except OSError:
            continue
        if child_mtime > latest:
            latest = child_mtime
    return latest


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    seen_mtimes: dict[str, float] = {}
    DEST_ROOT.mkdir(parents=True, exist_ok=True)

    while running:
        if SRC_ROOT.exists():
            current_ids = set()
            for consult_dir in sorted(SRC_ROOT.iterdir()):
                if not consult_dir.is_dir() or not consult_dir.name.startswith("consult_"):
                    continue
                consult_id = consult_dir.name
                current_ids.add(consult_id)
                try:
                    mtime = _latest_mtime(consult_dir)
                except OSError as e:
                    print(f"mirror_consults: stat failed for {consult_id}: {e}", file=sys.stderr)
                    continue
                if seen_mtimes.get(consult_id) == mtime:
                    continue
                dest_dir = DEST_ROOT / consult_id
                try:
                    shutil.copytree(consult_dir, dest_dir, dirs_exist_ok=True)
                    seen_mtimes[consult_id] = mtime
                    print(f"mirror_consults: copied {consult_id} -> {dest_dir}", file=sys.stderr)
                except OSError as e:
                    print(f"mirror_consults: copy failed for {consult_id}: {e}", file=sys.stderr)
            for consult_id in list(seen_mtimes):
                if consult_id not in current_ids:
                    del seen_mtimes[consult_id]
        time.sleep(POLL_INTERVAL_S)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
