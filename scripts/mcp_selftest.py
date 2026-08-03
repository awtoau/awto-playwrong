"""mcp_selftest.py — drive engine/mcp_server.py over real stdio JSON-RPC and assert it behaves.

This is the regression test for the MCP layer AND the thing a new user runs to prove their install
works end to end (protocol -> engine autostart -> Chrome -> a real page -> tab cleanup).

    python scripts/mcp_selftest.py                  # full run: protocol + a live page fetch
    python scripts/mcp_selftest.py --offline        # protocol only; no browser, no network
    python scripts/mcp_selftest.py --port 8739 --shutdown
                                                    # isolated engine on its own port, stopped after

--shutdown stops the engine when done. Only pass it when you started an isolated one (--port): the
default engine is SHARED, and stopping it kills other agents' cleared Turnstile session.

Results: tmp/logs/mcp-selftest.log
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGDIR = os.path.join(REPO, "tmp", "logs")
LOG = os.path.join(LOGDIR, "mcp-selftest.log")
TEST_URL = "https://example.com"
# Opt-in (--cloudflare) because it hits a third party and depends on their wall still being up.
# nowsecure.nl is the community's standard bot-detection target — it exists to be probed.
CF_URL = "https://nowsecure.nl"

PASS, FAIL = [], []
_log_fh = None


def say(*a):
    line = " ".join(str(x) for x in a)
    print(line, flush=True)
    if _log_fh:
        _log_fh.write(line + "\n")
        _log_fh.flush()


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    say(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return cond


class Client:
    """Minimal MCP stdio client: one line of JSON per message, both directions."""

    def __init__(self, port):
        env = {**os.environ, "PH_PORT": str(port)}
        self.p = subprocess.Popen([sys.executable, os.path.join(REPO, "engine", "mcp_server.py")],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=open(os.path.join(LOGDIR, "mcp-server-stderr.log"), "a"),
                                  env=env, text=True, bufsize=1)
        self.n = 0

    def rpc(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self.n += 1
            msg["id"] = self.n
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()
        if notify:
            return None
        line = self.p.stdout.readline()
        if not line:
            raise RuntimeError(f"server closed stdout during {method} (see mcp-server-stderr.log)")
        return json.loads(line)

    def call(self, tool, **args):
        return self.rpc("tools/call", {"name": tool, "arguments": args})

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=10)     # it exits as soon as stdin closes; 10s is pure slack
        except Exception:
            self.p.kill()


def text_of(resp):
    """Concatenate the text blocks of a tools/call result."""
    c = (resp.get("result") or {}).get("content") or []
    return "\n".join(b.get("text", "") for b in c if b.get("type") == "text")


def blocks(resp, kind):
    return [b for b in ((resp.get("result") or {}).get("content") or []) if b.get("type") == kind]


def is_error(resp):
    return bool((resp.get("result") or {}).get("isError"))


def protocol_tests(c):
    r = c.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                             "clientInfo": {"name": "selftest", "version": "0"}})
    res = r.get("result") or {}
    ok("initialize returns a result", "result" in r, json.dumps(r)[:200])
    ok("serverInfo.name == playwrong", res.get("serverInfo", {}).get("name") == "playwrong")
    ok("protocolVersion echoed", res.get("protocolVersion") == "2025-06-18")
    ok("declares tools capability", "tools" in (res.get("capabilities") or {}))
    ok("ships instructions", len(res.get("instructions") or "") > 50)

    c.rpc("notifications/initialized", notify=True)      # must produce NO response

    r = c.rpc("tools/list")
    tools = (r.get("result") or {}).get("tools") or []
    names = [t["name"] for t in tools]
    ok("tools/list non-empty", len(tools) >= 10, f"{len(tools)} tools: {', '.join(names)}")
    ok("fetch tool present", "fetch" in names)
    shaped = all(isinstance(t.get("description"), str) and len(t["description"]) > 40
                 and (t.get("inputSchema") or {}).get("type") == "object" for t in tools)
    ok("every tool has a description + object schema", shaped)

    ok("ping answers", "result" in c.rpc("ping"))
    r = c.rpc("no/such/method")
    ok("unknown method -> JSON-RPC error", (r.get("error") or {}).get("code") == -32601)
    r = c.call("no_such_tool")
    ok("unknown tool -> isError, not a crash", "error" in r or is_error(r))
    r = c.call("fetch")                                   # required arg missing
    ok("missing required arg -> isError, not a crash", is_error(r), text_of(r)[:120])


def live_tests(c):
    r = c.call("status")
    say("    status:", text_of(r).replace("\n", " ")[:160])
    ok("status returns JSON", text_of(r).lstrip().startswith("{"))

    say(f"    fetching {TEST_URL} (first call also launches Chrome — this is the slow one)")
    t0 = time.monotonic()
    r = c.call("fetch", url=TEST_URL)
    body = text_of(r)
    dt = time.monotonic() - t0
    ok("fetch returns page text", "Example Domain" in body, f"{len(body)} chars in {dt:.1f}s")
    ok("fetch output is text, not html", "<html" not in body.lower())
    ok("fetch header carries title + url", body.startswith("# ") and "URL: " in body[:200])

    r = c.call("tabs")
    tabs = json.loads(text_of(r))
    ok("fetch left no leaked tab", tabs.get("count") == 1, f"{tabs.get('count')} tab(s) open")

    r = c.call("fetch", url=TEST_URL, mode="html")
    ok("mode=html returns markup", "<html" in text_of(r).lower())

    # 60, not 200: example.com renders to ~150 chars of text, so a 200-char cap never trips and the
    # assertion passes for the wrong reason (it did, on the first run of this file).
    r = c.call("fetch", url=TEST_URL, max_chars=60)
    ok("max_chars truncates + says so", "[truncated:" in text_of(r), f"{len(text_of(r))} chars")

    r = c.call("goto", url=TEST_URL)
    ok("goto navigates", "Example Domain" in text_of(r))
    # Regression guard: browser.tabs is a cache the engine must refresh, else every tab reports a
    # blank url and closing one is unverifiable. That bug made `fetch` leak a tab per call.
    tabs = json.loads(text_of(c.call("tabs"))).get("tabs", [])
    ok("tabs report real urls", any("example.com" in (t.get("url") or "") for t in tabs),
       json.dumps(tabs))
    r = c.call("js", expr="document.title")
    ok("js evaluates in the page", "Example Domain" in text_of(r), text_of(r)[:80])
    # Regression guards. js used to hand back a bare Promise (serialised to null) for anything
    # async, and nodriver's RemoteObject repr for anything non-primitive.
    r = c.call("js", expr="await new Promise(r=>setTimeout(()=>r('resolved'),200))")
    ok("js awaits a promise", "resolved" in text_of(r), text_of(r)[:80])
    r = c.call("js", expr="({a:1,b:[2,3]})")
    ok("js returns real objects, not a RemoteObject repr",
       json.loads(text_of(r)) == {"a": 1, "b": [2, 3]}, text_of(r)[:80])
    r = c.call("js", expr="nope.nope()")
    ok("js exception is reported, not swallowed",
       is_error(r) and "ReferenceError" in text_of(r), text_of(r)[:90])
    r = c.call("read", mode="text+links")
    ok("read mode=text+links keeps hrefs", "<https://" in text_of(r) or "<http" in text_of(r))

    r = c.call("screenshot")
    imgs = blocks(r, "image")
    ok("screenshot returns a PNG image block",
       len(imgs) == 1 and imgs[0].get("mimeType") == "image/png" and len(imgs[0].get("data", "")) > 5000,
       f"{len(imgs[0]['data']) if imgs else 0} b64 chars")

    r = c.call("cookies")
    ok("cookies returns a list", text_of(r).lstrip().startswith("["))

    r = c.call("search", query="nodriver cloudflare", max_results=5)
    body = text_of(r)
    ok("search returns unwrapped result urls",
       "http" in body and "duckduckgo.com/l/" not in body, body.splitlines()[0] if body else "")
    # Ads share the organic results' CSS class and once took the top two slots. Any duckduckgo.com
    # url in the output means ad/help links are leaking back in.
    ok("search returns no ads or DDG furniture", "duckduckgo.com" not in body,
       next((ln for ln in body.splitlines() if "duckduckgo.com" in ln), ""))

    # prefetch/collect: fire a batch, take results as they land. The point is that this is FASTER
    # than looping fetch and that a stalled url cannot hold the rest.
    t0 = time.monotonic()
    r = c.call("prefetch", urls=[TEST_URL, "https://example.org", TEST_URL], concurrency=3)
    ok("prefetch returns immediately with a job", "job" in text_of(r) and time.monotonic()-t0 < 5,
       f"{time.monotonic()-t0:.1f}s")
    job = text_of(r).split("job ")[1].split(":")[0].strip()
    r = c.call("collect", job=job, wait=30, max_chars=200)
    body = text_of(r)
    ok("collect returns prefetched pages", "Example Domain" in body, body.splitlines()[0][:60])

    c.call("close_tab", close_extra=True)
    r = c.call("tabs")
    ok("close_extra leaves only the base tab", json.loads(text_of(r)).get("count") == 1)


def cloudflare_test(c):
    """The whole reason playwrong exists: one call gets a page that plain HTTP cannot."""
    say(f"    fetching {CF_URL} — a real Turnstile wall (slow: the solve loop clicks and waits)")
    t0 = time.monotonic()
    r = c.call("fetch", url=CF_URL, tries=25)
    body = text_of(r)
    dt = time.monotonic() - t0
    walled = any(k in body.lower() for k in ("just a moment", "verify you are human",
                                             "not cleared"))
    # Assert on CONTENT, not length: the cleared page is only ~90 chars of text, so a length
    # threshold fails a run that actually worked (it did, the first time this ran).
    ok("fetch clears a live Cloudflare challenge",
       not walled and "nowsecure" in body.lower(),
       f"{len(body)} chars in {dt:.1f}s — {body[:120]!r}")
    ok("cf_clearance cookie present", "cf_clearance" in text_of(c.call("cookies")))
    # prefetch/collect: fire a batch, take results as they land. The point is that this is FASTER
    # than looping fetch and that a stalled url cannot hold the rest.
    t0 = time.monotonic()
    r = c.call("prefetch", urls=[TEST_URL, "https://example.org", TEST_URL], concurrency=3)
    ok("prefetch returns immediately with a job", "job" in text_of(r) and time.monotonic()-t0 < 5,
       f"{time.monotonic()-t0:.1f}s")
    job = text_of(r).split("job ")[1].split(":")[0].strip()
    r = c.call("collect", job=job, wait=30, max_chars=200)
    body = text_of(r)
    ok("collect returns prefetched pages", "Example Domain" in body, body.splitlines()[0][:60])

    c.call("close_tab", close_extra=True)


def main():
    global _log_fh
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PH_PORT", "8731")))
    ap.add_argument("--offline", action="store_true", help="protocol tests only; no browser")
    ap.add_argument("--cloudflare", action="store_true",
                    help=f"also fetch {CF_URL} to prove the Turnstile path end to end")
    ap.add_argument("--shutdown", action="store_true",
                    help="stop the engine afterwards (ONLY for an isolated --port)")
    a = ap.parse_args()

    os.makedirs(LOGDIR, exist_ok=True)
    _log_fh = open(LOG, "w")
    say(f"mcp selftest — repo {REPO}, engine port {a.port}, python {sys.version.split()[0]}\n")

    c = Client(a.port)
    try:
        protocol_tests(c)
        if a.offline:
            say("\n(--offline: skipped the live browser tests)")
        else:
            live_tests(c)
            if a.cloudflare:
                cloudflare_test(c)
    finally:
        c.close()
        if a.shutdown:
            try:
                urllib.request.urlopen(urllib.request.Request(
                    f"http://127.0.0.1:{a.port}/shutdown", data=b"{}", method="POST"), timeout=10)
                say("\nengine on :%d shut down" % a.port)
            except Exception as e:
                say(f"\nshutdown call: {e}")

    say(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        say("failed: " + ", ".join(FAIL))
    say(f"log: {LOG}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
