"""crawl_stall_test.py — crawl.run must finish when a page's renderer dies mid-fetch (#27).

- Local site: an index linking two ordinary pages and a "hog" that allocates ~1 GB/s.
- Isolated engine under PH_MEMORY_MAX=1500M, so the kernel kills the hog's renderer (#28).
- Before the fix the stall-abandon cleanup awaited the dead tab forever and the crawl never exited.

    python scripts/crawl_stall_test.py              # isolated engine on :8739, stopped after

Results: tmp/logs/crawl-stall-test.log
"""
import argparse
import asyncio
import http.server
import os
import shutil
import subprocess
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from crawl import run  # noqa: E402
from engine import connect  # noqa: E402

LOGDIR = os.path.join(REPO, "tmp", "logs")
LOG = os.path.join(LOGDIR, "crawl-stall-test.log")
WORK = os.path.join(REPO, "tmp", "crawl-stall-test")
STALL_CEILING = 6.0
# ~1.25x the fixed run (9.9 s on 2026-10-10, issue #27). Expiry = the hang this test exists for.
RUN_BUDGET = 13.0

PAGES = {
    "/": '<title>idx</title><a href="/ok1">1</a> <a href="/hog">h</a> <a href="/ok2">2</a>',
    "/ok1": "<title>ok1</title><p>one</p>",
    "/ok2": "<title>ok2</title><p>two</p>",
    "/hog": ("<title>hog</title><script>const a=[];setInterval(()=>{const b=new Float64Array"
             "(6.5e6);b.fill(1);a.push(b)},50)</script>"),
}

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


class Site(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = PAGES.get(self.path.split("?")[0])
        if body is None:
            self.send_response(404); self.end_headers(); return
        b = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


def watchdog_check():
    """The no-progress watchdog cuts a batch whose workers never finish. No browser needed."""
    run.WATCH_POLL = 0.05                  # the real poll is 5 s; the logic is what is under test

    async def go():
        stuck = asyncio.ensure_future(asyncio.sleep(3600))
        t0 = time.monotonic()
        cut = await run._watch([stuck], {"ok": 0, "fail": 0}, no_progress=0.2)
        return cut, stuck.cancelled(), time.monotonic() - t0
    cut, cancelled, dt = asyncio.run(go())
    ok("watchdog cuts a batch that makes no progress", cut and cancelled, f"{dt:.2f}s")


def main():
    global _fh
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--headed", action="store_true",
                    help="show the test browser on your desktop (default: its own Xvfb, PH_DISPLAY=xvfb)")
    ap.add_argument("--port", type=int, default=8739, help="isolated engine port (never 8731)")
    a = ap.parse_args()
    if not a.headed:
        os.environ["PH_DISPLAY"] = "xvfb"   # headed Chrome off the desktop; --headed to watch
    if a.port == 8731:
        sys.exit("refusing to run on 8731: this test runs the engine under a small memory cap")
    os.makedirs(LOGDIR, exist_ok=True)
    _fh = open(LOG, "w")
    if connect.reachable(a.port):
        sys.exit(f"an engine is already on :{a.port}; stop it first (the cap is fixed at spawn)")
    watchdog_check()
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    os.environ["PH_MEMORY_MAX"] = "1500M"
    try:
        connect.ensure(a.port, want_browser=True)
        os.environ.pop("PH_MEMORY_MAX")
        cmd = [sys.executable, "-m", "crawl.run", "--seed", f"{base}/", "--db",
               os.path.join(WORK, "c.sqlite"), "--store", os.path.join(WORK, "pages"),
               "--max", "4", "--tabs", "2", "--depth", "1", "--rate-delay", "0",
               "--stall-ceiling", str(STALL_CEILING), "--port", str(a.port)]
        before = connect.call("tabs", port=a.port, method="GET").get("count")
        t0 = time.monotonic()
        try:
            r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=RUN_BUDGET)
            out, done = r.stdout + r.stderr, True
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            done = False
        dt = time.monotonic() - t0
        for line in out.splitlines():
            if any(k in line for k in ("STALL", "DONE", "TIMEOUT", "NO PROGRESS", "Traceback", "Error")):
                say("   ", line[:160])
        ok("crawl.run exits instead of hanging", done, f"{dt:.1f}s of a {RUN_BUDGET:.0f}s budget")
        after = connect.call("tabs", port=a.port, method="GET").get("count")
        ok("no crawl tab left open, the dead one included", after == before, f"{before} -> {after}")
        ok("the ordinary pages were crawled", "DONE: 3 ok" in out or "DONE: 4 ok" in out,
           next((ln for ln in out.splitlines() if ln.startswith("DONE")), "no DONE line"))
        if FAIL:
            say("--- crawl.run output ---\n" + out[-4000:])
    finally:
        srv.shutdown()
        try:
            connect.call("shutdown", port=a.port, timeout=15.0)
        except Exception as e:
            say(f"  shutdown: {str(e)[:80]}")
        say(f"\n{len(PASS)} passed, {len(FAIL)} failed")
        say(f"log: {LOG}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
