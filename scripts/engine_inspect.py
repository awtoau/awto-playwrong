"""engine_inspect.py — look inside a RUNNING engine without restarting it (issue #23).

The engine reached 39.5 GB and was OOM-killed with nothing in the log saying what it held. This
injects a read-only probe into the live process with sys.remote_exec (PEP 768, CPython 3.14+) and
reports what the heap is made of, so the next growth is attributed instead of guessed at.

    python scripts/engine_inspect.py                  # engine on :8731 (pid from /status)
    python scripts/engine_inspect.py --port 8744      # another engine
    python scripts/engine_inspect.py --pid 12345
    python scripts/engine_inspect.py --tracemalloc start      # begin attributing allocations
    python scripts/engine_inspect.py --tracemalloc snapshot   # top allocation sites since start
    python scripts/engine_inspect.py --tracemalloc stop

Reports: RSS, threads and their stacks, asyncio tasks by coroutine, object counts by type, the
largest single objects, nodriver per-connection counters (handlers, transactions, pending futures,
websocket receive buffers), prefetch jobs and the html bytes they still hold.

The probe runs on the engine's main thread (the HTTP accept loop, which wakes every 0.5s), reads
state, appends one JSON line to tmp/logs/engine-inspect.log, and returns. It changes nothing unless
--tracemalloc is given, which turns allocation tracing on or off in the engine.

Permissions: the engine's Python may carry a file capability (cap_net_bind_service), which makes the
process non-dumpable and ptrace-protected even from its own user. When remote_exec is refused the
one injecting call is retried under `sudo -n`; the report is still written by the engine itself, as
its own user.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = os.path.join(REPO, "tmp")
LOGDIR = os.path.join(TMP, "logs")
REPORT = os.path.join(LOGDIR, "engine-inspect.log")
PAYLOAD = os.path.join(TMP, "engine-inspect-payload.py")

# The probe runs on the next wake of the accept loop (0.5s poll) and then walks the heap. Walking a
# multi-GB heap is the slow part; 15s allows ~12s of walk (1.25x). On expiry we say the probe did
# not report and point at the log, rather than waiting on a main thread that may be blocked.
REPORT_BUDGET = 15.0

PROBE = r'''
import collections, gc, json, os, sys, threading, time, traceback
OUT, NONCE, TOP, TM = %(out)r, %(nonce)r, %(top)d, %(tm)r
rep = {"nonce": NONCE, "ts": time.strftime("%%Y-%%m-%%dT%%H:%%M:%%S%%z"), "pid": os.getpid()}
def _mb(key):
    try:
        for line in open("/proc/self/status"):
            if line.startswith(key):
                return int(line.split()[1]) // 1024
    except Exception:
        return None
rep["rss_mb"] = _mb("VmRSS"); rep["swap_mb"] = _mb("VmSwap"); rep["threads"] = threading.active_count()
try:
    frames = sys._current_frames()
    rep["stacks"] = {t.name: traceback.format_stack(frames[t.ident])[-3:]
                     for t in threading.enumerate() if t.ident in frames}
except Exception as e:
    rep["stacks"] = repr(e)
try:
    import tracemalloc
    if TM == "start" and not tracemalloc.is_tracing():
        tracemalloc.start(12)
        rep["tracemalloc"] = "started"
    elif TM == "stop":
        tracemalloc.stop(); rep["tracemalloc"] = "stopped"
    elif TM == "snapshot" and tracemalloc.is_tracing():
        snap = tracemalloc.take_snapshot()
        prev = globals().get("_pw_inspect_prev")
        stats = snap.compare_to(prev, "lineno") if prev else snap.statistics("lineno")
        rep["tracemalloc"] = {"traced_mb": tracemalloc.get_traced_memory()[0] // 2**20,
                              "top": [str(s)[:200] for s in stats[:TOP]]}
        globals()["_pw_inspect_prev"] = snap
    elif TM == "snapshot":
        rep["tracemalloc"] = "not tracing; run with --tracemalloc start first"
except Exception as e:
    rep["tracemalloc"] = repr(e)
try:
    objs = gc.get_objects()
    cnt = collections.Counter(type(o).__qualname__ for o in objs)
    rep["objects"] = len(objs)
    rep["by_type"] = cnt.most_common(TOP)
    big = []
    for o in objs:
        if isinstance(o, (bytes, bytearray, str, list, dict, tuple, set, collections.deque)):
            try: n = sys.getsizeof(o)
            except Exception: continue
            if n > 1 << 20:
                big.append((n, type(o).__qualname__, repr(o)[:80]))
    big.sort(reverse=True)
    rep["big_objects"] = [{"mb": n // 2**20, "type": t, "head": h} for n, t, h in big[:TOP]]
    del objs
except Exception as e:
    rep["objects"] = repr(e)
try:
    import __main__ as M
    B = M.B
    loop = B.loop
    rep["loop"] = {"ready": len(getattr(loop, "_ready", ())),
                   "scheduled": len(getattr(loop, "_scheduled", ()))}
    import asyncio
    tasks = collections.Counter()
    for t in list(asyncio.all_tasks(loop)):
        try: tasks[t.get_coro().__qualname__] += 1
        except Exception: tasks["?"] += 1
    rep["tasks"] = tasks.most_common(TOP)
    rep["tags"] = len(B.tags); rep["owners"] = len(B.owners)
    rep["jobs"] = [{"job": j, "total": d.get("total"), "delivered": d.get("delivered"),
                    "slots": len(d.get("slots", {})),
                    "html_mb": sum(len(v.get("html") or "") for v in d.get("slots", {}).values()) // 2**20}
                   for j, d in B.jobs.items()]
    # Connection objects anywhere in the heap, not just the ones the browser still lists — the
    # ones it has forgotten are the leak (#23).
    from nodriver.core.connection import Connection
    conns = []
    listed = set(id(c) for c in [B.browser] + list(getattr(B.browser, "_targets", []))) if B.browser else set()
    for c in [o for o in gc.get_objects() if isinstance(o, Connection)]:
        sock = getattr(c, "socket", None)
        rm = getattr(sock, "recv_messages", None)
        rd = getattr(getattr(sock, "protocol", None), "reader", None)
        tx = list(getattr(c, "_transactions", ()))
        conns.append({
            "type": type(c).__qualname__, "listed": id(c) in listed,
            "url": (getattr(getattr(c, "target", None), "url", "") or "")[:60],
            "socket_open": bool(sock) and not getattr(sock, "close_code", None),
            "handlers": sum(len(v) for v in getattr(c, "handlers", {}).values()),
            "transactions": len(tx),
            "tx_result_mb": sum(sys.getsizeof(getattr(t, "result", None) or 0) for t in tx) // 2**20,
            "pending": len(getattr(c, "_mapper", ())),
            "children": len(getattr(c, "_targets", ())),
            "dom": getattr(c, "_dom", None) is not None,
            "ws_frames": len(getattr(rm, "frames", ())),
            "ws_reader_buf": len(getattr(rd, "buffer", b"")),
        })
    rep["connections"] = {"total": len(conns), "listed": sum(1 for c in conns if c["listed"]),
                          "unlisted_sockets_open": sum(1 for c in conns if not c["listed"] and c["socket_open"]),
                          "sample": conns[:TOP]}
except Exception as e:
    rep["engine"] = repr(e) + " " + traceback.format_exc()[-300:]
with open(OUT, "a") as f:
    f.write(json.dumps(rep, default=str) + "\n")
'''


def status(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=3) as r:
            return json.loads(r.read())
    except Exception:
        return {}


def proc_read(path):
    """A /proc file of another process. The engine's python may carry a file capability, which makes
    the process non-dumpable and its /proc entries unreadable even by its own user: sudo -n then."""
    try:
        return open(path, "rb").read()
    except PermissionError:
        r = subprocess.run(["sudo", "-n", "cat", path], capture_output=True)
        return r.stdout if r.returncode == 0 else b""
    except OSError:
        return b""


def find_pid(port):
    st = status(port)
    if st.get("pid"):
        return st["pid"]
    # Older engines do not report their pid: find the server.py process whose PH_PORT is this port
    # (the port is in the environment, not the argv; unset means the default port).
    hits, matched = [], []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        cmd = proc_read(f"/proc/{d}/cmdline").split(b"\0")
        if not any(c.endswith(b"engine/server.py") for c in cmd):
            continue
        hits.append(int(d))
        env = dict(kv.split(b"=", 1) for kv in proc_read(f"/proc/{d}/environ").split(b"\0") if b"=" in kv)
        if int(env.get(b"PH_PORT", b"8731")) == port:
            matched.append(int(d))
    if len(matched) == 1:
        return matched[0]
    sys.exit(f"cannot pick the engine pid: /status on :{port} has no pid, {len(hits)} server.py "
             f"processes are running {hits} and {len(matched)} match PH_PORT={port}; pass --pid")


def inject(pid, path):
    try:
        sys.remote_exec(pid, path)
        return "direct"
    except PermissionError:
        pass
    code = f"import sys; sys.remote_exec({pid}, {path!r})"
    r = subprocess.run(["sudo", "-n", sys.executable, "-c", code], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"remote_exec refused directly and under sudo -n: {r.stderr.strip()[:300]}")
    return "sudo"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=int(os.environ.get("PH_PORT", "8731")))
    ap.add_argument("--pid", type=int)
    ap.add_argument("--top", type=int, default=25, help="rows per table (default 25)")
    ap.add_argument("--tracemalloc", choices=["start", "snapshot", "stop"],
                    help="control allocation tracing inside the engine")
    a = ap.parse_args()
    if not hasattr(sys, "remote_exec"):
        sys.exit(f"sys.remote_exec needs CPython 3.14+; this is {sys.version.split()[0]}")
    os.makedirs(LOGDIR, exist_ok=True)
    pid = a.pid or find_pid(a.port)
    nonce = f"{pid}-{time.time_ns()}"
    with open(PAYLOAD, "w") as f:
        f.write(PROBE % {"out": REPORT, "nonce": nonce, "top": a.top, "tm": a.tracemalloc or ""})
    how = inject(pid, PAYLOAD)
    t0 = time.monotonic()
    rep = None
    while time.monotonic() - t0 < REPORT_BUDGET and rep is None:
        if os.path.exists(REPORT):
            for line in open(REPORT):
                if nonce in line:
                    rep = json.loads(line)
                    break
        if rep is None:
            time.sleep(0.2)     # the accept loop wakes every 0.5s; 0.2s polls catch it within one wake
    if rep is None:
        sys.exit(f"probe injected ({how}) into pid {pid} but no report after {REPORT_BUDGET}s — "
                 f"the main thread may be blocked; see {REPORT}")
    print(f"pid {pid} injected via {how}; report appended to {os.path.relpath(REPORT, REPO)}")
    print(json.dumps(rep, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
