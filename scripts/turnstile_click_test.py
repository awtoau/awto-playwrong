"""turnstile_click_test.py — the engine's checkbox click clears a REAL interactive Turnstile (#45).

- Local page embedding Cloudflare's widget with test sitekey 3x00000000000000000000FF ("forces an
  interactive challenge", Cloudflare testing docs). Real widget, real cross-origin iframe.
- Title starts as "Just a moment..." so the engine's `solve` op sees a challenge; the widget's
  callback retitles the page and records the token (dummy: XXXX.DUMMY.TOKEN.XXXX).
- Isolated engine, headed on its own Xvfb (`--headed` for the desktop). Stopped afterwards.

    python scripts/turnstile_click_test.py

Results: tmp/logs/turnstile-click-test.log
"""
import argparse
import http.server
import os
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from engine import connect  # noqa: E402

LOG = os.path.join(REPO, "tmp", "logs", "turnstile-click-test.log")
SITEKEY = "3x00000000000000000000FF"
DUMMY_TOKEN = "XXXX.DUMMY.TOKEN.XXXX"
PAGE = f"""<!doctype html><title>Just a moment...</title>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
<div class="cf-turnstile" data-sitekey="{SITEKEY}" data-callback="done"></div>
<script>function done(t){{ window.__token = t; document.title = "cleared"; }}</script>""".encode()

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
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)


def engine_lines(since, *events):
    """This run's engine log lines for the given events."""
    path = os.path.join(connect.DATA, "nd-server.log")
    out = []
    try:
        for line in open(path):
            if line[:23] >= since and any(f" {e} " in line or line.rstrip().endswith(f" {e}")
                                          for e in events):
                out.append(line.strip())
    except OSError:
        pass
    return out


def diagnose(port, tag):
    """On failure: a screenshot, and what the pierced DOM holds (frames, text) — why find missed."""
    import asyncio
    import base64

    from engine import cdp
    shot = os.path.join(os.path.dirname(LOG), "turnstile-click-test.png")
    with open(shot, "wb") as f:
        f.write(base64.b64decode(connect.call("shot", port=port, tab=tag)["b64"]))
    say(f"  screenshot: {shot}")
    tid = next(t["target_id"] for t in connect.call("tabs", port=port, method="GET")["tabs"]
               if t.get("tag") == tag)
    ep = connect.call("cdp", port=port)

    async def go():
        b = await cdp.attach(ep["host"], ep["port"])
        try:
            say("  targets:", [(t.type, t.url[:60]) for t in b.tabs])
            info = await b.send("Target.getTargets")
            say("  all targets:", [(i["type"], i["url"][:60]) for i in info["targetInfos"]])
            t = next(t for t in b.tabs if t.target_id == tid)
            root = (await t.send("DOM.getDocument", {"depth": -1, "pierce": True}))["root"]
            frames, texts, stack = [], [], [root]
            while stack:
                n = stack.pop()
                if n.get("nodeName") == "IFRAME":
                    frames.append((n.get("attributes", [])[1::2][:2], "contentDocument" in n))
                if n.get("nodeType") == 3 and (n.get("nodeValue") or "").strip():
                    texts.append(n["nodeValue"].strip()[:50])
                stack.extend(cdp._children(n))
            say("  iframes (attrs, has contentDocument):", frames)
            say("  text nodes:", texts[:30])
        finally:
            await b.aclose()
    asyncio.run(go())


def main():
    global _fh
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8739, help="isolated engine port (never 8731)")
    ap.add_argument("--headed", action="store_true", help="show the browser on your desktop")
    a = ap.parse_args()
    if a.port == 8731:
        sys.exit("refusing to run on 8731: use an isolated engine")
    if not a.headed:
        os.environ["PH_DISPLAY"] = "xvfb"
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    _fh = open(LOG, "w")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    tag = "turnstile-click-test"
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    try:
        connect.ensure(a.port, want_browser=True)
        connect.call("newtab", port=a.port, url="about:blank", tag=tag)
        connect.call("goto", port=a.port, url=url, tab=tag, timeout=60)
        t0 = time.monotonic()
        r = connect.call("solve", port=a.port, tries=10, tab=tag, timeout=120)
        dt = time.monotonic() - t0
        clicks = engine_lines(since, "clicked")
        ok("the solve loop clicked the checkbox", bool(clicks), clicks[-1][:120] if clicks else "no 'clicked' line")
        ok("solve reports the challenge passed", r.get("passed") is True, f"{r} in {dt:.1f}s")
        tok = connect.call("js", port=a.port, expr="window.__token || null", tab=tag).get("result")
        ok("Turnstile issued its token", tok == DUMMY_TOKEN, repr(tok))
        errs = engine_lines(since, "click_err")
        ok("no click errors", not errs, errs[-1][:120] if errs else "")
        if FAIL:
            diagnose(a.port, tag)
    finally:
        try:
            connect.call("closetab", port=a.port, tag=tag)
        except Exception:
            pass
        try:
            connect.call("shutdown", port=a.port, timeout=15.0)
        except Exception as e:
            say(f"  shutdown: {str(e)[:80]}")
        srv.shutdown()
        say(f"\n{len(PASS)} passed, {len(FAIL)} failed")
        say(f"log: {LOG}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
