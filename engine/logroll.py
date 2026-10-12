"""logroll.py — size-capped logs: past the cap, `name.log` becomes `name.log.1` (#39). Stdlib only.

Cap: 20 MB, one backup. The engine log reached 16 MB over three months of heavy test use
(2026-10-12), so the current file plus `.1` keep months of history; disk use is bounded at 40 MB.
"""
import os

MAX_BYTES = 20 << 20


def roll(path, max_bytes=None):
    """Move `path` to `path.1` (replacing the old backup) once it is past `max_bytes` (default
    MAX_BYTES, read per call). One stat under the cap: cheap enough before every write. True if
    it rolled."""
    try:
        if os.path.getsize(path) < (max_bytes or MAX_BYTES):
            return False
        os.replace(path, f"{path}.1")
        return True
    except OSError:            # missing (nothing to roll) or a racing writer already rolled it
        return False
