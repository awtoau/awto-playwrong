"""cdp_surface_check.py — what can reach the engine Chrome's DevTools port, and from which origin.

Headless Chrome via engine/cdp.py, no network. Checks:
- our client (no Origin header) connects; a second client attaches to the same browser
- a websocket with a foreign web Origin (a page on another site) is REFUSED
- the DevTools frontend's own origin (http://127.0.0.1:<port>) is accepted, so a human can inspect
  the shared browser (`/json/list` -> devtoolsFrontendUrl, or chrome://inspect)
Exit 0 = all as expected. Log: tmp/logs/cdp-surface-check.log
"""
import asyncio
import json
import os
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "engine"))
import cdp  # noqa: E402

LOG = os.path.join(REPO, "tmp", "logs", "cdp-surface-check.log")
failures = []


def check(name, ok, detail=""):
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {'PASS' if ok else 'FAIL'} {name} {detail}".rstrip()
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")
    if not ok:
        failures.append(name)


async def ws_accepts(url, origin):
    """Does Chrome accept a DevTools websocket carrying this Origin header (None = no header)?"""
    from websockets.asyncio.client import connect
    try:
        async with connect(url, origin=origin, open_timeout=cdp.WS_OPEN_TIMEOUT) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Browser.getVersion"}))
            return "result" in json.loads(await asyncio.wait_for(ws.recv(), cdp.WS_OPEN_TIMEOUT))
    except Exception:
        return False


async def main():
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    open(LOG, "w").close()
    b = await cdp.launch(headless=True)
    try:
        base = f"http://{b.host}:{b.port}"
        info = json.loads(urllib.request.urlopen(f"{base}/json/version", timeout=2).read())
        ws = info["webSocketDebuggerUrl"]
        check("no wildcard --remote-allow-origins in the launch flags",
              "--remote-allow-origins=*" not in cdp.CHROME_FLAGS)
        check("our client (no Origin) is accepted", await ws_accepts(ws, None))
        b2 = await cdp.attach(b.host, b.port)
        check("a second client attaches to the same browser", bool(b2.tabs))
        await b2.aclose()
        check("a foreign web origin is refused", not await ws_accepts(ws, "https://awto.au"))
        check("the DevTools frontend's own origin is accepted", await ws_accepts(ws, base))
        pages = json.loads(urllib.request.urlopen(f"{base}/json/list", timeout=2).read())
        check("/json/list offers a DevTools frontend url per page",
              bool(pages) and all("devtoolsFrontendUrl" in p for p in pages if p["type"] == "page"))
        check("the debugging port listens on loopback only", b.host == "127.0.0.1")
    finally:
        await b.close()
    print("OK" if not failures else f"FAILED: {', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
