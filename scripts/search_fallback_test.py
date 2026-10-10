"""search_fallback_test.py — a DuckDuckGo block falls back to Brave Search, and is logged (#34).

- A local server answers as DDG's anti-bot page; connect.DDG_LITE is pointed at it in-process.
- search() must retry DDG once, then return live Brave results tagged engine="brave".
- Every attempt must land in the search log.

    python scripts/search_fallback_test.py          # isolated engine on :8739 (own Xvfb), stopped after

Results: tmp/logs/search-fallback-test.log
"""
import argparse
import http.server
import json
import os
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from engine import connect  # noqa: E402

LOGDIR = os.path.join(REPO, "tmp", "logs")
LOG = os.path.join(LOGDIR, "search-fallback-test.log")
BLOCK_PAGE = (b"<html><title>DuckDuckGo</title><body>Unfortunately, bots use DuckDuckGo too. "
              b"Please complete the following challenge: select all squares containing a duck."
              b"</body></html>")

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


class Blocked(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(BLOCK_PAGE)))
        self.end_headers()
        self.wfile.write(BLOCK_PAGE)


def main():
    global _fh
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8739, help="isolated engine port (never 8731)")
    ap.add_argument("--headed", action="store_true",
                    help="show the test browser on your desktop (default: its own Xvfb, PH_DISPLAY=xvfb)")
    a = ap.parse_args()
    if a.port == 8731:
        sys.exit("refusing to run on 8731: use an isolated engine")
    if not a.headed:
        os.environ["PH_DISPLAY"] = "xvfb"   # headed Chrome off the desktop; --headed to watch
    os.makedirs(LOGDIR, exist_ok=True)
    _fh = open(LOG, "w")

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Blocked)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    connect.DDG_LITE = f"http://127.0.0.1:{srv.server_address[1]}/lite/?q={{}}"
    seen = os.path.getsize(connect.SEARCH_LOG) if os.path.exists(connect.SEARCH_LOG) else 0
    query = "nodriver cloudflare"
    try:
        t0 = time.monotonic()
        hits = connect.search(query, max_results=5, port=a.port)
        dt = time.monotonic() - t0
        ok("a DDG block falls back to Brave", getattr(hits, "engine", None) == "brave",
           f"engine={getattr(hits, 'engine', None)} in {dt:.1f}s")
        ok("Brave returns real results", len(hits) > 0 and all(h["url"].startswith("http")
                                                               for h in hits),
           hits[0]["url"] if hits else "no hits")
        with open(connect.SEARCH_LOG) as f:
            f.seek(seen)
            lines = [json.loads(ln) for ln in f if ln.strip()]
        mine = [(x["engine"], x["outcome"]) for x in lines if x["q"] == query]
        ok("every attempt is logged: block, retry, Brave",
           mine == [("duckduckgo", "blocked"), ("duckduckgo", "retry_blocked"),
                    ("brave", "results")], str(mine))
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
