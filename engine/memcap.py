"""memcap.py — run this process under a hard systemd memory cap, so a leak kills it, not the host.

Used by the engine (engine/server.py) and by crawl.run, which attaches to the shared Chrome with its
own nodriver and leaked the same way (#23, #26). Stdlib only.

- The process re-execs itself as `systemd-run --user --scope -p MemoryMax=… -p MemorySwapMax=0
  -p OOMPolicy=kill <same argv>`.
- MemorySwapMax=0 is the half that matters: MemoryMax alone makes the cgroup swap instead of die,
  which was the hour of thrash before the host OOM in #23.
- Each ROLE gets its own scope. The marker env var names the role, so an engine spawned by a capped
  crawl re-execs into a scope of its own instead of sharing the crawl's cap and dying with it.
- No systemd user manager -> runs uncapped and says so; it never refuses to start.
"""
import os
import shutil
import subprocess
import sys

MARKER = "PH_MEMCAP_ROLE"


def parse_size(s):
    """'8G' / '512M' / '1234' -> bytes; '' or '0' -> None (no cap)."""
    s = str(s or "").strip().upper()
    if not s or s == "0":
        return None
    mult = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}.get(s[-1])
    return int(float(s[:-1]) * mult) if mult else int(s)


def cgroup_memory_max():
    """This process's cgroup memory.max in bytes; None when unlimited or unknown."""
    try:
        path = open("/proc/self/cgroup").read().split("::", 1)[1].strip()
        v = open(f"/sys/fs/cgroup{path}/memory.max").read().strip()
        return None if v == "max" else int(v)
    except (OSError, IndexError, ValueError):
        return None


def enter(role, size, log=lambda *a, **k: None):
    """Re-exec under a capped scope for `role` unless already there. Returns a one-line summary of
    the cap in force (for /status and logs). `size` like '8G'; '0' or '' disables."""
    want = parse_size(size)
    if want is None:
        return "off (0)"
    if os.environ.get(MARKER) == role:
        have = cgroup_memory_max()
        if have is not None and have <= want:
            return f"{have >> 20}M"
        log("memory_cap_failed", role=role, want=size, cgroup_max=have)
        return "none (systemd-run ran but applied no cap)"
    if not shutil.which("systemd-run"):
        log("memory_cap_unavailable", role=role, why="no systemd-run on PATH")
        return "none (no systemd-run)"
    base = ["systemd-run", "--user", "--scope", "--quiet", "-p", f"MemoryMax={want}",
            "-p", "MemorySwapMax=0", "-p", "OOMPolicy=kill"]
    try:
        # Creating a scope is one D-Bus round trip to the user manager, ~50ms measured. 5s is
        # 100x; on expiry we run uncapped and log it — a stuck manager must not turn start into hang.
        r = subprocess.run([*base, "true"], capture_output=True, text=True, timeout=5)
        why = r.stderr.strip()[:120] if r.returncode else None
    except (OSError, subprocess.SubprocessError) as e:
        why = repr(e)[:120]
    if why is not None:
        log("memory_cap_unavailable", role=role, why=why)
        return "none (systemd-run refused)"
    log("memory_cap_exec", role=role, max=size)
    # orig_argv, not argv: `python -m crawl.run` must come back as -m, or its relative imports break.
    os.execvpe("systemd-run", [*base, sys.executable, *sys.orig_argv[1:]],
               {**os.environ, MARKER: role})
