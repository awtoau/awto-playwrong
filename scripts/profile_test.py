"""profile_test.py — a persistent profile (PH_PROFILE_DIR) survives restarts and crashes (#41).

- Clean restart: a persistent cookie is still there, the profile dir is kept.
- Crash: SIGKILL engine + Chrome (pids from this engine's own /status, never pgrep), then the next
  launch heals the stale SingletonLock and comes up.
- Isolated engine on :8739, headed on its own Xvfb (`--headed` for the desktop).

    python scripts/profile_test.py

Results: tmp/logs/profile-test.log
"""
import argparse
import http.server
import os
import shutil
import signal
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from engine import connect  # noqa: E402

LOG = os.path.join(REPO, "tmp", "logs", "profile-test.log")
PROFILE = os.path.join(REPO, "tmp", "profile-test", "profile")
# Engine exit after SIGKILL / shutdown: ms locally; 5 s covers Chrome's own teardown on /shutdown.
GONE_TIMEOUT = 5.0

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
    """Sets a persistent cookie (Max-Age), so it must survive a browser restart."""
    def log_message(self, *a):
        pass

    def do_GET(self):
        b = b"<title>profile</title><p>cookie set</p>"
        self.send_response(200)
        self.send_header("Set-Cookie", "pw_profile_test=kept; Max-Age=3600; Path=/")
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


def has_cookie(port):
    return any(c["name"] == "pw_profile_test" and c["value"] == "kept"
               for c in connect.call("cookies", port=port)["cookies"])


def wait_down(port, pids):
    t0 = time.monotonic()
    while time.monotonic() - t0 < GONE_TIMEOUT:
        alive = [p for p in pids if p and _alive(p)]
        if not alive and not connect.reachable(port, timeout=0.5):
            return True
        time.sleep(0.05)
    return False


def _alive(pid):
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    try:
        return "State:\tZ" not in open(f"/proc/{pid}/status").read()
    except OSError:
        return False


def main():
    global _fh
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8739, help="isolated engine port (never 8731)")
    ap.add_argument("--headed", action="store_true", help="show the browser on your desktop")
    a = ap.parse_args()
    if a.port == 8731:
        sys.exit("refusing to run on 8731: use an isolated engine")
    if connect.reachable(a.port):
        sys.exit(f"an engine is already on :{a.port}; stop it first (the profile is fixed at spawn)")
    if not a.headed:
        os.environ["PH_DISPLAY"] = "xvfb"
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    _fh = open(LOG, "w")
    shutil.rmtree(PROFILE, ignore_errors=True)
    os.makedirs(PROFILE)
    os.environ["PH_PROFILE_DIR"] = PROFILE
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    p = a.port
    try:
        # 1. first run: set the cookie
        connect.ensure(p, want_browser=True)
        s1 = connect.call("status", port=p, method="GET")
        connect.call("goto", port=p, url=url, timeout=60)
        ok("cookie set in the first run", has_cookie(p))
        ok("Chrome runs on the persistent profile",
           os.path.exists(os.path.join(PROFILE, "Local State")), PROFILE)
        # Chrome writes cookies to disk lazily; a clean shutdown flushes them.
        connect.call("shutdown", port=p, timeout=15.0)
        ok("clean shutdown ends engine and Chrome", wait_down(p, [s1["pid"], s1["chrome_pid"]]))
        ok("the profile dir is kept after stop", os.path.isdir(os.path.join(PROFILE, "Default")))
        # 2. clean restart
        connect.ensure(p, want_browser=True)
        s2 = connect.call("status", port=p, method="GET")
        ok("relaunch on the same profile comes up", s2.get("alive") is True,
           f"chrome {s1['chrome_pid']} -> {s2.get('chrome_pid')}")
        ok("the persistent cookie survived the restart", has_cookie(p))
        # 3. crash: kill engine and Chrome hard, leaving SingletonLock behind
        os.kill(s2["pid"], signal.SIGKILL)
        os.killpg(s2["chrome_pid"], signal.SIGKILL)     # Chrome has its own process group
        ok("engine and Chrome are dead", wait_down(p, [s2["pid"], s2["chrome_pid"]]))
        ok("the crash left a stale SingletonLock",
           os.path.lexists(os.path.join(PROFILE, "SingletonLock")))
        t0 = time.monotonic()
        connect.ensure(p, want_browser=True)
        s3 = connect.call("status", port=p, method="GET")
        ok("launch after a crash heals the lock and comes up", s3.get("alive") is True,
           f"{time.monotonic() - t0:.1f}s")
        r = connect.call("goto", port=p, url=url, timeout=60)
        ok("the profile still serves pages", r.get("title") == "profile", r.get("title"))
    finally:
        try:
            connect.call("shutdown", port=p, timeout=15.0)
        except Exception as e:
            say(f"  shutdown: {str(e)[:80]}")
        srv.shutdown()
        say(f"\n{len(PASS)} passed, {len(FAIL)} failed")
        say(f"log: {LOG}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
