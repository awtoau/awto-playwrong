"""cleanup_orphans.py — find and close browsers left behind by a dead engine.

An engine that exits without closing its browser leaves a full Chrome running with nothing driving
it. They are invisible (headed but easily lost behind other windows), they never exit on their own,
and they are expensive: 14 of them were holding 15.7 GB here.

Safety: this ONLY touches Chrome processes whose `--user-data-dir` is a nodriver temp profile
(`/tmp/uc_*`) or a playwrong profile dir, AND only when no engine is currently serving. Your own
browser uses a real profile directory and can never match. Nothing is killed without saying so.

    python scripts/cleanup_orphans.py            # report only
    python scripts/cleanup_orphans.py --kill     # close them
"""
import argparse
import os
import re
import signal
import subprocess
import sys
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from engine import connect  # noqa: E402

# nodriver's throwaway profiles, plus playwrong's named ones. Anything else is somebody's real
# browser and is never a candidate.
PROFILE_RE = re.compile(r"--user-data-dir=(/tmp/uc_[^\s]*|[^\s]*/playwrong/profiles/[^\s]*)")
PORTS = [8731] + [8739, 8741, 8799] + list(range(8740, 8790))


def live_engines():
    """Ports currently served by an engine. A browser belonging to one of these is NOT an orphan."""
    up = []
    for p in sorted(set(PORTS)):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{p}/status", timeout=0.3)
            up.append(p)
        except Exception:
            pass
    return up


def orphan_groups():
    """{profile_dir: [pids]} for every Chrome running on a playwrong-style profile."""
    try:
        out = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True).stdout
    except OSError:
        return {}
    groups = {}
    for line in out.splitlines():
        line = line.strip()
        if "chrome" not in line and "chromium" not in line:
            continue
        m = PROFILE_RE.search(line)
        if not m:
            continue
        pid = int(line.split(None, 1)[0])
        groups.setdefault(m.group(1), []).append(pid)
    return groups


def rss_mb(pids):
    try:
        out = subprocess.run(["ps", "-o", "rss=", *[str(p) for p in pids]],
                             capture_output=True, text=True).stdout
        return int(sum(int(x) for x in out.split()) / 1024)
    except (OSError, ValueError):
        return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kill", action="store_true", help="actually close them (default: report only)")
    a = ap.parse_args()

    up = live_engines()
    if up:
        print(f"engines still serving on {up} — stop them first with `playwrong --stop`, so this "
              f"cannot close a browser that is genuinely in use.")
        return 1

    groups = orphan_groups()
    if not groups:
        print("no orphaned browsers")
        return 0

    total = sum(len(v) for v in groups.values())
    print(f"{len(groups)} orphaned browser(s), {total} processes, ~{rss_mb(sum(groups.values(), []))} MB")
    for prof, pids in sorted(groups.items()):
        print(f"  {prof}  ({len(pids)} procs)")
    if not a.kill:
        print("\nre-run with --kill to close them")
        return 0

    closed = 0
    for prof, pids in groups.items():
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)   # TERM, not KILL: let Chrome tear down its own tree
                closed += 1
            except (ProcessLookupError, PermissionError):
                pass
        if prof.startswith("/tmp/uc_") and os.path.isdir(prof):
            subprocess.run(["rm", "-rf", prof], capture_output=True)
    print(f"closed {closed} processes across {len(groups)} browser(s)")
    print(f"cache/logs live in {connect.DATA}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
