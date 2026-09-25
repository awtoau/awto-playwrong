"""memory_test.py — prove the engine does not grow per tab (issue #23).

The shared engine reached 39.5 GB and took the host down. Every closed tab had left behind a live
websocket to Chrome, a listener task and the last 25 CDP results on that connection — whole DOM
trees from get_content() — about 20 MB each; 277 such ghosts were found for a browser with 3 tabs.
This opens, drives and closes tabs on an ISOLATED engine and checks that CDP sockets, file
descriptors and RSS come back to where they started.

    python scripts/memory_test.py                        # isolated engine on :8739, stopped after
    python scripts/memory_test.py --cycles 60 --port 8750
    python scripts/memory_test.py --keep                 # leave the engine up for engine_inspect.py

Refuses 8731: the shared engine holds everyone's cleared Turnstile session, and this test closes
tabs and stops the engine.

Against an engine running code from before #23 (no memory fields in /status) the numbers are read
from /proc instead — sudo -n if the engine's python is non-dumpable — so the same loop demonstrates
the leak on the old code: `PH_PORT=8750 python <old checkout>/engine/server.py`, then `--port 8750`.

Results: tmp/logs/memory-test.log
"""
import argparse
import os
import subprocess
import sys
import time
import urllib.parse

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine import connect  # noqa: E402

LOGDIR = os.path.join(REPO, "tmp", "logs")
LOG = os.path.join(LOGDIR, "memory-test.log")

# A page with a few thousand DOM nodes, served from the url itself so the test needs no network.
# get_content() on it pulls a DOM.getDocument tree of that size — the payload that leaked in #23.
PAGE = "data:text/html," + urllib.parse.quote("<title>memtest</title>" + "<p>node</p>" * 2000)

# Thresholds. Before the fix one cycle on this page leaked ~10 MB and one CDP socket; after it the
# socket count is flat and RSS moves by allocator noise. 1 MB per cycle is 10x below the leak and
# above the noise seen in runs of 30.
RSS_PER_CYCLE_MB = 1.0
SOCKET_SLACK = 1      # a refresh can catch Chrome mid-teardown of one target
FD_SLACK = 4

PASS, FAIL = [], []
_fh = None


def say(*a):
    line = " ".join(str(x) for x in a)
    print(line, flush=True)
    if _fh:
        _fh.write(line + "\n"); _fh.flush()


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    say(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def status(port):
    """The engine's /status, plus the memory fields read from outside when the engine predates them
    (code before #23) — so the same loop can show the leak on the old code and its absence on the new."""
    s = connect.call("status", port=port, method="GET")
    if "rss_mb" in s:
        return s
    s.update(external_status(port))
    return s


def external_status(port):
    from engine_inspect import find_pid       # sibling script: the same pid lookup
    pid = find_pid(port)
    fields = {"pid": pid}
    try:
        text = open(f"/proc/{pid}/status").read()
    except PermissionError:                   # the engine's python may carry a file capability
        text = subprocess.run(["sudo", "-n", "cat", f"/proc/{pid}/status"],
                              capture_output=True, text=True).stdout
    for line in text.splitlines():
        if line.startswith("VmRSS"):
            fields["rss_mb"] = int(line.split()[1]) // 1024
    try:
        fds = os.listdir(f"/proc/{pid}/fd")
    except PermissionError:
        fds = subprocess.run(["sudo", "-n", "ls", f"/proc/{pid}/fd"],
                             capture_output=True, text=True).stdout.split()
    fields["fds"] = len(fds)
    cdp_port = connect.call("cdp", port=port).get("port")
    n = 0
    for line in open("/proc/net/tcp").readlines()[1:]:
        f = line.split()
        if f[3] == "01" and int(f[2].split(":")[1], 16) == cdp_port:
            n += 1
    fields["cdp_sockets"] = n
    fields["tabs"] = connect.call("tabs", port=port, method="GET").get("count")
    fields["threads"] = None
    return fields


def row(label, s):
    say(f"  {label:<10} rss={s.get('rss_mb')}MB fds={s.get('fds')} cdp_sockets={s.get('cdp_sockets')} "
        f"tabs={s.get('tabs')} threads={s.get('threads')}")


def cycle(port, i):
    tag = f"memtest-{i}"
    connect.call("newtab", port=port, url="about:blank", tag=tag, owner="memory_test")
    connect.call("goto", port=port, url=PAGE, tab=tag)
    connect.call("text", port=port, tab=tag)          # get_content(): the DOM tree that leaked
    connect.call("js", port=port, expr="document.title", tab=tag)
    r = connect.call("closetab", port=port, tag=tag)
    if r.get("closed") != 1:
        say(f"  cycle {i}: closetab returned {r}")


def main():
    global _fh
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=8739)
    ap.add_argument("--cycles", type=int, default=30)
    ap.add_argument("--keep", action="store_true", help="do not stop the engine afterwards")
    a = ap.parse_args()
    if a.port == connect.default_port():
        sys.exit(f"refusing to run against the shared engine on :{a.port}; pass --port")
    os.makedirs(LOGDIR, exist_ok=True)
    _fh = open(LOG, "w")
    say(f"memory_test port={a.port} cycles={a.cycles} {time.strftime('%Y-%m-%dT%H:%M:%S%z')}")

    connect.ensure(a.port, want_browser=True)
    s0 = status(a.port)
    say(f"  engine pid={s0.get('pid')} code={s0.get('code', {}).get('sha')} "
        f"memory_cap={s0.get('memory_cap', 'n/a (old code, measured from outside)')} "
        f"rss_limit={s0.get('rss_limit_mb')}MB")
    for i in range(3):                       # warm-up: first tabs pay one-off allocator costs
        cycle(a.port, -1 - i)
    base = status(a.port)
    row("baseline", base)

    t0 = time.monotonic()
    for i in range(a.cycles):
        cycle(a.port, i)
        if (i + 1) % 10 == 0:
            row(f"after {i + 1}", status(a.port))
    end = status(a.port)
    row("final", end)
    say(f"  {a.cycles} cycles in {time.monotonic() - t0:.1f}s")

    grew = (end["rss_mb"] or 0) - (base["rss_mb"] or 0)
    per = grew / a.cycles
    ok("CDP sockets back to baseline", end["cdp_sockets"] <= base["cdp_sockets"] + SOCKET_SLACK,
       f"{base['cdp_sockets']} -> {end['cdp_sockets']}")
    ok("file descriptors back to baseline", end["fds"] <= base["fds"] + FD_SLACK,
       f"{base['fds']} -> {end['fds']}")
    ok("tabs back to baseline", end["tabs"] == base["tabs"], f"{base['tabs']} -> {end['tabs']}")
    ok(f"RSS growth under {RSS_PER_CYCLE_MB} MB per cycle", per < RSS_PER_CYCLE_MB,
       f"{grew} MB over {a.cycles} cycles = {per:.2f} MB/cycle")

    if not a.keep:
        try:
            connect.call("shutdown", port=a.port)
        except Exception as e:
            say(f"  shutdown: {str(e)[:80]}")
    say(f"\n{len(PASS)} passed, {len(FAIL)} failed" + (f": {', '.join(FAIL)}" if FAIL else ""))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
