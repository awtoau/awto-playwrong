"""cdp_smoke_test.py — engine/cdp.py against a real headless Chrome, no network (#41).

Launch, tabs, navigate, evaluate, content, screenshot, find_text through an iframe + shadow root,
click, cookies, events, close, stop. Fetch/Page enables survive a navigation (#43).
Exit 0 = all passed. Log: tmp/logs/cdp-smoke-test.log
"""
import asyncio
import http.server
import os
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "engine"))
import cdp  # noqa: E402

LOG = os.path.join(REPO, "tmp", "logs", "cdp-smoke-test.log")

# The needle sits in a shadow root inside a same-process iframe: the Turnstile layout.
PAGE = ("data:text/html,<title>smoke</title><p id=p>hello</p>"
        "<iframe style='width:400px;height:200px' srcdoc=\"<div id=h></div><script>"
        "const r=document.getElementById('h').attachShadow({mode:'closed'});"
        "r.innerHTML='<label>Verify you are human</label>';"
        "r.querySelector('label').onclick=()=>parent.document.title='clicked';"
        "</script>\"></iframe>")

failures = []


class Site(http.server.BaseHTTPRequestHandler):
    """/img: a page with an image; /alert: a page that raises a dialog."""
    PAGES = {"/img": b"<title>img</title><img src=/x.png>", "/alert": b"<script>alert(1)</script>",
             "/x.png": b"\x89PNG\r\n\x1a\n"}

    def log_message(self, *a):
        pass

    def do_GET(self):
        path, _, q = self.path.partition("?")
        b = self.PAGES.get(path, b"")
        if path == "/img":                   # a distinct image url per load: no cache in the way
            b = f"<title>img</title><img src=/x.png?{q}>".encode()
        self.send_response(200 if b else 404)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


async def enables_survive_navigation(b, base):
    """#43: nodriver re-attached a new session per navigate, losing Fetch/Page enables."""
    t = await b.new_tab("about:blank")
    paused, dialogs = [], []

    async def fail(p):
        paused.append(p["request"]["url"])
        await t.send("Fetch.failRequest", {"requestId": p["requestId"], "errorReason": "BlockedByClient"})

    async def accept(p):
        dialogs.append(p.get("message"))
        await t.send("Page.handleJavaScriptDialog", {"accept": True})
    t.on("Fetch.requestPaused", fail)
    t.on("Page.javascriptDialogOpening", accept)
    await t.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "resourceType": "Image"}]})
    await t.send("Page.enable")
    for q in ("1", "2"):                    # the second load is the one #43 lost
        await t.navigate(f"{base}/img?{q}")
        await asyncio.sleep(0.5)
    check("Fetch blocking survives navigation (#43)",
          [u.rsplit("/", 1)[1] for u in paused] == ["x.png?1", "x.png?2"], str(paused))
    await t.navigate(f"{base}/alert")
    await asyncio.sleep(0.5)
    check("dialog handling survives navigation (#43)", dialogs == ["1"], str(dialogs))
    check("one session for the tab's life", len([s for s in b._sessions.values() if s is t]) == 1)
    await t.close()


def log(msg):
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def check(name, ok, detail=""):
    log(f"{'PASS' if ok else 'FAIL'} {name} {detail}".rstrip())
    if not ok:
        failures.append(name)


async def main():
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    t0 = time.monotonic()
    b = await cdp.launch(headless=True, log_path=os.path.join(REPO, "tmp", "logs", "cdp-smoke-chrome.log"))
    check("launch", b.process is not None and not b.stopped, f"{time.monotonic() - t0:.2f}s port={b.port}")
    # The endpoint fallback for a Chrome whose stderr is not readable (Windows): /json/version.
    url = cdp._version_url(b.port)
    check("/json/version fallback names this browser", bool(url) and f":{b.port}/devtools/browser/" in url,
          url)
    check("/json/version fallback: nothing listening -> None", cdp._version_url(cdp._free_port()) is None)
    try:
        check("initial tab", len(b.tabs) >= 1, f"tabs={len(b.tabs)}")
        t = await b.new_tab("about:blank")
        check("new_tab listed", t in b.tabs)
        await t.navigate(PAGE)
        for _ in range(50):
            if await t.evaluate("document.readyState") == "complete":
                break
            await asyncio.sleep(0.05)
        check("evaluate value", await t.evaluate("1+1") == 2)
        check("evaluate promise", await t.evaluate("Promise.resolve('x')", await_promise=True) == "x")
        try:
            await t.evaluate("(()=>{throw new Error('boom')})()")
            check("evaluate raises", False)
        except cdp.JSError as e:
            check("evaluate raises", "boom" in str(e))
        html = await t.content()
        check("content", html.startswith("<!DOCTYPE") or "<p id=\"p\">hello</p>" in html, html[:40])
        png = await t.screenshot()
        check("screenshot png", png[:8] == b"\x89PNG\r\n\x1a\n", f"{len(png)} bytes")
        hit = await t.find_text("verify you are human", timeout=3)
        check("find_text through iframe+shadow", hit is not None, hit and f"({hit.x}, {hit.y})")
        if hit:
            await hit.click()
            await asyncio.sleep(0.2)
            check("click reaches the label", await t.evaluate("document.title") == "clicked")
        # #49: a page hooking DOM methods must not see isolated-world calls.
        await t.navigate("data:text/html,<title>hook</title><p id=p>x</p><script>window.__hits=0;"
                         "const g=document.getElementById.bind(document);"
                         "document.getElementById=(i)=>{window.__hits++;return g(i)}</script>")
        await asyncio.sleep(0.3)
        iso = await t.evaluate("document.getElementById('p').textContent", isolated=True)
        check("isolated world reads the DOM", iso == "x", repr(iso))
        check("isolated call is invisible to the page's hook", await t.evaluate("__hits") == 0)
        await t.evaluate("document.getElementById('p')")
        check("main-world call is seen by the hook (control)", await t.evaluate("__hits") == 1)
        await t.navigate("data:text/html,<title>after</title>")
        await asyncio.sleep(0.3)
        check("isolated world survives a navigation", await t.evaluate(
            "document.title", isolated=True) == "after")
        seen = []
        t.on("Page.frameNavigated", lambda p: seen.append(p))
        await t.send("Page.enable")
        await t.navigate("about:blank")
        await asyncio.sleep(0.3)
        check("session event", bool(seen), f"{len(seen)} events")
        await t.send("Network.setCookie", {"name": "k", "value": "v", "domain": "awto.au", "path": "/"})
        check("cookies", any(c["name"] == "k" for c in await b.cookies()))
        await b.clear_cookies()
        check("clear_cookies", not await b.cookies())
        await t.close()
        for _ in range(40):
            if t not in b.tabs:
                break
            await asyncio.sleep(0.05)
        check("close drops tab", t not in b.tabs)
        try:
            await t.evaluate("1")
            check("closed tab raises", False)
        except cdp.TargetGone:
            check("closed tab raises", True)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            await enables_survive_navigation(b, f"http://127.0.0.1:{srv.server_address[1]}")
        finally:
            srv.shutdown()
        b2 = await cdp.attach(b.host, b.port)
        check("attach second client", len(b2.tabs) >= 1)
        await b2.aclose()
        check("aclose leaves chrome", not b.stopped)
    finally:
        udd = b.user_data_dir
        await b.close()
        left = os.listdir(udd) if os.path.exists(udd) else None
        check("stop", b.stopped and left is None, f"stopped={b.stopped} profile_left={left}")
    try:
        await b.send("Browser.getVersion")
        check("send after stop raises", False)
    except cdp.ConnectionLost:
        check("send after stop raises", True)
    log(f"{'OK' if not failures else 'FAILED: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
