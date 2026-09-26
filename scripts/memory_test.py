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

The three guards are exercised on their own, each with a fresh engine on the port whose limit is
set so low that one tab cycle trips it:

    python scripts/memory_test.py --retire     # PH_RSS_LIMIT=64M: rss_limit logged, engine exits,
                                               # the next call is served by a new pid
    python scripts/memory_test.py --recycle    # PH_CHROME_MAX_FETCHES=3: chrome_recycle logged,
                                               # a different chrome_pid, the caller's tab still works
    python scripts/memory_test.py --cap        # PH_MEMORY_MAX=200M: the kernel kills the scope
                                               # (Chrome alone exceeds it), the next call respawns

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
    from engine_inspect import find_pid  # sibling script: the same pid lookup
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


ENGINE_LOG = os.path.join(REPO, "tmp", "nd-server.log")


def log_lines(pid, event):
    """Lines the engine with `pid` wrote for `event`."""
    try:
        return [line for line in open(ENGINE_LOG) if f" pid={pid} " in line and f" {event} " in line]
    except OSError:
        return []


def alive(pid):
    """A zombie counts as dead: the engine we spawned is our child until reaped, so /proc/<pid>
    outlives the process itself."""
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    try:
        return "State:\tZ" not in open(f"/proc/{pid}/status").read()
    except OSError:
        return False


def wait_gone(port, pid, budget):
    """True once nothing answers on `port` and `pid` is gone. `budget` is the caller's: the
    retire path drains in-flight ops for up to 40s, so its caller passes 50s (1.25x)."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < budget:
        if not connect.reachable(port, timeout=1.0) and not alive(pid):
            return True
        time.sleep(0.5)     # exits are seconds apart; 0.5s keeps the reported time close to real
    return False


def fresh_engine(port, **env):
    """Start a new engine on `port` with these extra environment variables, and return its status.
    Refuses if one is already there: the variables only take effect at spawn."""
    if connect.reachable(port):
        sys.exit(f"an engine is already on :{port}; stop it first (its limits were fixed at spawn)")
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        connect.ensure(port, want_browser=True)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return status(port)


def guard_retire(port):
    """Step 1 of #23: RSS over the limit retires the engine; the next call is served by a new one."""
    s = fresh_engine(port, PH_RSS_LIMIT="64M")
    pid = s["pid"]
    say(f"  engine pid={pid} rss={s['rss_mb']}MB rss_limit={s['rss_limit_mb']}MB")
    try:
        cycle(port, 0)                      # `text` adds ~10 MB, taking a ~62 MB engine past 64M
    except connect.EngineError as e:
        say(f"  op after the trigger: {str(e)[:100]}")
    gone = wait_gone(port, pid, 50.0)
    ok("engine retired itself", gone, f"pid {pid}" + ("" if gone else " still there after 50s"))
    lines = log_lines(pid, "rss_limit")
    ok("rss_limit logged with rss and limit", any("rss_mb=" in x and "limit_mb=" in x for x in lines),
       (lines[-1].strip()[:140] if lines else "no rss_limit line"))
    s2 = status_after_respawn(port)
    ok("next call is served by a new engine", s2.get("pid") not in (None, pid),
       f"pid {pid} -> {s2.get('pid')}")


def guard_recycle(port):
    """Step 2 of #23: past the fetch count Chrome is replaced, and the caller's tab still works."""
    s = fresh_engine(port, PH_CHROME_MAX_FETCHES="3")
    pid, chrome = s["pid"], s["chrome_pid"]
    say(f"  engine pid={pid} chrome_pid={chrome}")
    for i in range(3):
        cycle(port, i)
    tag = "memtest-recycled"
    connect.call("newtab", port=port, url="about:blank", tag=tag, owner="memory_test")   # 4th: recycle
    r = connect.call("goto", port=port, url=PAGE, tab=tag)
    s2 = status(port)
    ok("chrome_recycle logged", bool(log_lines(pid, "chrome_recycle")),
       (log_lines(pid, "chrome_recycle") or ["no line"])[-1].strip()[:140])
    ok("a different Chrome serves the caller", s2.get("chrome_pid") not in (None, chrome),
       f"chrome {chrome} -> {s2.get('chrome_pid')}")
    ok("the caller's tab works in the new Chrome", r.get("title") == "memtest", str(r)[:100])
    ok("same engine process throughout", s2.get("pid") == pid)
    connect.call("closetab", port=port, tag=tag)


def guard_cap(port):
    """Step 3 of #23: the cgroup cap kills the scope when Chrome + python exceed it."""
    if not connect.reachable(port):
        # ensure() would raise when Chrome dies mid-launch; spawn and wait for the port instead.
        saved = os.environ.get("PH_MEMORY_MAX")
        os.environ["PH_MEMORY_MAX"] = "200M"
        try:
            connect.spawn(port)
        finally:
            os.environ.pop("PH_MEMORY_MAX", None) if saved is None else os.environ.__setitem__("PH_MEMORY_MAX", saved)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 30.0 and not connect.reachable(port, timeout=1.0):
            time.sleep(0.25)   # connect.ensure()'s own bind wait, same numbers
    s = status(port)
    pid = s.get("pid")
    ok("engine reports the cap", s.get("memory_cap") == "200M", str(s.get("memory_cap")))
    try:
        connect.call("start", port=port, timeout=120.0)      # Chrome alone exceeds 200M
        cycle(port, 0)
        say("  ops completed under the cap")
    except connect.EngineError as e:
        say(f"  op under the cap: {str(e)[:100]}")
    gone = wait_gone(port, pid, 30.0)
    ok("scope was killed by the cap", gone, f"pid {pid}" + ("" if gone else " survived 30s"))
    r = subprocess.run(["sudo", "-n", "journalctl", "-k", "--since", "-3min", "--no-pager",
                        "-o", "short-iso", "--grep", "Memory cgroup out of memory|oom-kill"],
                       capture_output=True, text=True)
    kern = [x for x in r.stdout.splitlines() if "oom" in x.lower()]
    say("  kernel: " + (kern[-1][:160] if kern else "no oom line readable (journalctl needs sudo -n)"))
    s2 = status_after_respawn(port)
    ok("next call respawns an engine", s2.get("pid") not in (None, pid), f"pid {pid} -> {s2.get('pid')}")


def status_after_respawn(port):
    """connect.ensure() on a dead port spawns a new engine; return its status (no browser needed)."""
    try:
        connect.ensure(port, want_browser=False)
    except connect.EngineError as e:
        say(f"  respawn: {str(e)[:100]}")
        return {}
    return status(port)


def main():
    global _fh
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=8739)
    ap.add_argument("--cycles", type=int, default=30)
    ap.add_argument("--keep", action="store_true", help="do not stop the engine afterwards")
    ap.add_argument("--retire", action="store_true", help="exercise the RSS self-restart only")
    ap.add_argument("--recycle", action="store_true", help="exercise the Chrome recycle only")
    ap.add_argument("--cap", action="store_true", help="exercise the cgroup memory cap only")
    a = ap.parse_args()
    if a.port == connect.default_port():
        sys.exit(f"refusing to run against the shared engine on :{a.port}; pass --port")
    os.makedirs(LOGDIR, exist_ok=True)
    _fh = open(LOG, "w")
    say(f"memory_test port={a.port} cycles={a.cycles} {time.strftime('%Y-%m-%dT%H:%M:%S%z')}")
    if a.retire or a.recycle or a.cap:
        if a.retire:
            guard_retire(a.port)
        if a.recycle:
            guard_recycle(a.port)
        if a.cap:
            guard_cap(a.port)
        if not a.keep:
            try:
                connect.call("shutdown", port=a.port)
            except Exception as e:
                say(f"  shutdown: {str(e)[:80]}")
        say(f"\n{len(PASS)} passed, {len(FAIL)} failed" + (f": {', '.join(FAIL)}" if FAIL else ""))
        return 1 if FAIL else 0

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
