"""crawl.browser — attach to the shared awto-playwrong headed Chrome. Zero project refs.

The whole point of awto-playwrong: ONE long-running headed Chrome (Turnstile-cleared, reused by many
agents). This module attaches a nodriver Browser to it via the engine's published CDP endpoint,
starting the engine server if it isn't up. Callers open their OWN tabs on the returned browser and
close them when done — NEVER stop the shared server.
"""
import asyncio
import json
import os
import subprocess
import sys
import urllib.request as _urlreq

import nodriver as uc

# The playwrong repo root = two levels up from this file (…/awto-playwrong/crawl/browser.py).
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SERVER = os.path.join(_REPO, "engine", "server.py")
_VENDOR = os.path.join(_REPO, "vendor")
_LOG_DIR = os.path.join(_REPO, "tmp")


def _call(op, port, body=None, method="POST", timeout=60):
    url = f"http://127.0.0.1:{port}/{op}"
    data = json.dumps(body or {}).encode() if method == "POST" else None
    req = _urlreq.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    return json.loads(_urlreq.urlopen(req, timeout=timeout).read())


def is_up(port):
    """True if the engine SERVER responds. Note: the server can be up while its Chrome is DEAD —
    use browser_ok() for the stronger check."""
    try:
        _call("status", port, method="GET", timeout=3)
        return True
    except Exception:
        return False


def close_extra_tabs(port=8731):
    """MAINTENANCE ONLY — reap strays when the browser is IDLE. Asks the server to close every tab
    except its protected base tab. DANGER: this closes ALL extra tabs, so do NOT call it while a crawl
    (yours or another agent's) is running on the shared browser — it would kill their live tabs too.
    The crawl no longer calls this automatically (it reuses a fixed pool, so it doesn't leak). Run it by
    hand — `python -m crawl.browser --sweep` — only when nothing is crawling. Best-effort; never raises."""
    try:
        _call("closeextra", port, timeout=15)
    except Exception:
        pass


def _main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Shared-browser maintenance (run only when idle).")
    ap.add_argument("--sweep", action="store_true", help="Close all stray tabs (keep the base tab). IDLE ONLY.")
    ap.add_argument("--port", type=int, default=8731)
    a = ap.parse_args(argv)
    if a.sweep:
        close_extra_tabs(a.port)
        print("swept stray tabs (kept base). Only safe when no crawl is running.")
    else:
        ap.print_help()


if __name__ == "__main__":
    _main()


def browser_ok(port):
    """True only if the server is up AND its Chrome is actually reachable (a valid /cdp host:port).
    This is the check that matters — a bare status=alive can lie when the browser was closed."""
    try:
        info = _call("cdp", port, timeout=8)
        return bool(info.get("host") and info.get("port"))
    except Exception:
        return False


def _start_server(port):
    os.makedirs(_LOG_DIR, exist_ok=True)
    subprocess.Popen(
        [sys.executable, _SERVER],
        env={**os.environ, "PYTHONPATH": _VENDOR, "PH_PORT": str(port)},
        stdout=open(os.path.join(_LOG_DIR, "playwrong-server.log"), "a"),
        stderr=subprocess.STDOUT)


async def _wait_up(port, tries=60):
    for _ in range(tries):
        if is_up(port):
            return True
        await asyncio.sleep(0.5)
    return False


async def attach(port=8731):
    """Attach nodriver to the shared browser via /cdp — self-healing.

    Robust to the browser being CLOSED (Dan's case): a bare server status=alive can lie when its Chrome
    was stopped, so we (1) ensure the server is up, (2) ask it to launch/relaunch Chrome via /goto,
    (3) verify a real /cdp endpoint, and only then attach. If the server itself is wedged (goto errors
    but status lies), we shut it down cleanly (/shutdown — never pkill) and start a fresh one. Retries
    the whole sequence a few times before giving up. Open your own tabs, close them when done."""
    last_err = None
    for attempt in range(4):
        # 1) server up?
        if not is_up(port):
            _start_server(port)
            await _wait_up(port)
        # 2) ensure Chrome is (re)launched. /goto auto-launches Chrome if it isn't running.
        try:
            _call("goto", port, {"url": "about:blank"}, timeout=60)
        except Exception as e:
            last_err = e
            # server is up but wedged (its Chrome connection is dead) -> recycle the server cleanly.
            try:
                _call("shutdown", port, timeout=15)
            except Exception:
                pass
            await asyncio.sleep(1.5)
            _start_server(port)
            await _wait_up(port)
            continue
        # 3) verify a real CDP endpoint before attaching.
        if not browser_ok(port):
            last_err = "no /cdp endpoint (browser not ready)"
            await asyncio.sleep(1.5)
            continue
        info = _call("cdp", port, timeout=10)
        try:
            return await uc.start(host=info["host"], port=info["port"])  # host+port => attach
        except Exception as e:
            last_err = e
            await asyncio.sleep(1.5)
            continue
    raise RuntimeError(f"crawl.browser.attach: could not obtain a live browser on port {port} "
                       f"after retries — last error: {last_err}")
