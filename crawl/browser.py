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
    try:
        _call("status", port, method="GET", timeout=3)
        return True
    except Exception:
        return False


async def attach(port=8731):
    """Attach nodriver to the shared browser via /cdp; start the engine server first if needed.
    Returns the nodriver Browser bound to that same Chrome. Open your own tabs, close them when done."""
    if not is_up(port):
        os.makedirs(_LOG_DIR, exist_ok=True)
        subprocess.Popen(
            [sys.executable, _SERVER],
            env={**os.environ, "PYTHONPATH": _VENDOR, "PH_PORT": str(port)},
            stdout=open(os.path.join(_LOG_DIR, "playwrong-server.log"), "a"),
            stderr=subprocess.STDOUT)
        for _ in range(60):
            if is_up(port):
                break
            await asyncio.sleep(0.5)
    # ensure Chrome is launched, then read its CDP endpoint and attach.
    _call("goto", port, {"url": "about:blank"}, timeout=60)
    info = _call("cdp", port, timeout=10)
    host, cport = info["host"], info["port"]
    browser = await uc.start(host=host, port=cport)   # host+port => attach, don't launch
    return browser
