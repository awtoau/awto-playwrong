"""mcp/server.py — MCP (stdio) front-end for the playwrong capture engine.

WHY THIS EXISTS
An agent that wants one bot-walled page used to have to: read docs/AGENT-API.md, write a script with
ensure_server(), know PYTHONPATH=vendor + PH_PORT, understand lazy Chrome launch, do
goto -> sniff title -> solve -> text, then remember to close its tab — and then swallow ~500KB of raw
HTML. This file removes all of that: the ops arrive in the agent's tool list already described, the
engine is auto-started, the Turnstile dance is collapsed into ONE `fetch` call, tabs are opened and
closed inside that call so they cannot leak, and HTML comes back as readable text.

WHAT IT IS NOT
Not a rewrite. The real engine is still engine/server.py (nodriver, one shared headed Chrome, HTTP on
PH_PORT). This is a thin JSON-RPC-over-stdio proxy in front of it. Every tool here is one or a few
HTTP POSTs to that engine.

DEPENDENCIES: stdlib only. The engine itself needs `websockets` + `Deprecated` (nodriver's runtime
deps) — see scripts/doctor.py. This file needs nothing, so it starts instantly and can report a
broken engine install as a tool error instead of failing to load.

PROTOCOL: MCP over stdio = newline-delimited JSON-RPC 2.0 on stdin/stdout. STDOUT IS THE WIRE —
nothing may print to it except protocol messages (all diagnostics go to stderr, and the engine
child's output is redirected to tmp/logs/). Run manually with:
    python mcp/server.py            # then type JSON-RPC lines, or use scripts/mcp_selftest.py
"""
import base64
import html as htmllib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("PH_PORT", "8731"))
BASE = f"http://127.0.0.1:{PORT}"
LOGDIR = os.path.join(REPO, "tmp", "logs")
SERVER_LOG = os.path.join(LOGDIR, "playwrong-engine.log")
NAME, VERSION = "playwrong", "0.1.0"

# Newest protocol revision we implement. If the client asks for a different one we echo THEIRS back
# (the spec's negotiation rule) — every revision so far is wire-compatible for tools-only servers.
PROTOCOL = "2025-06-18"

# One capture = several engine ops (newtab -> goto -> text -> solve -> text -> closetab) and the
# engine has ONE globally-active tab, so two interleaved captures would steal each other's tab. This
# lock serialises captures within this process. Across processes (two agents, two MCP servers, one
# engine) it does not help — run separate PH_PORTs, or attach via /cdp. See docs/MCP.md.
_capture_lock = threading.Lock()


def elog(*a):
    """Diagnostics -> stderr. MCP clients capture stderr as the server's log; stdout is the wire."""
    print(*a, file=sys.stderr, flush=True)


# ── engine transport ────────────────────────────────────────────────────────────────────────────

class EngineError(RuntimeError):
    pass


def _reachable(timeout=2.0):
    """Is the engine's HTTP process answering? Deliberately checks reachability ONLY, never the
    `alive` field — `alive` (Chrome launched) stays false until a real op asks for a browser, so
    polling it in a loop waits forever. See docs/AGENT-API.md."""
    try:
        urllib.request.urlopen(f"{BASE}/status", timeout=timeout)
        return True
    except Exception:
        return False


def _spawn_engine():
    """Start engine/server.py as a detached child. Its stdout+stderr go to a log file, never to our
    stdout (which is the MCP wire)."""
    os.makedirs(LOGDIR, exist_ok=True)
    env = {**os.environ, "PYTHONPATH": os.path.join(REPO, "vendor"), "PH_PORT": str(PORT)}
    out = open(SERVER_LOG, "a")
    subprocess.Popen([sys.executable, os.path.join(REPO, "engine", "server.py")],
                     env=env, stdout=out, stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, start_new_session=True)
    elog(f"[{NAME}] spawned engine on :{PORT} (log: {SERVER_LOG})")


def ensure_engine(want_browser=True):
    """Guarantee an engine on PORT, and (by default) a launched Chrome behind it.

    Two separate waits, for two separate things:
      1. the HTTP process binding the port — no browser involved, just a Python import + bind, so a
         20s ceiling is generous; polled every 0.25s to keep first-call latency low.
      2. Chrome actually launching — done by POSTing /start, which BLOCKS until the browser is up, so
         there is nothing to poll. Cold Chrome start is the slow part; 120s ceiling.
    """
    if not _reachable():
        _spawn_engine()
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if _reachable(timeout=1.0):
                break
            time.sleep(0.25)
        else:
            raise EngineError(
                f"engine did not bind 127.0.0.1:{PORT} within 20s. Check {SERVER_LOG}; the usual "
                f"cause is a missing dependency — run: python {REPO}/scripts/doctor.py")
    if want_browser:
        st = call_raw("status", method="GET")
        if not st.get("alive"):
            call_raw("start", timeout=120.0)   # blocks until Chrome is up; do not poll /status


def call_raw(op, method="POST", timeout=60.0, **body):
    """One engine op. Raises EngineError on transport failure or an {"error": ...} payload."""
    try:
        if method == "GET":
            raw = urllib.request.urlopen(f"{BASE}/{op}", timeout=timeout).read()
        else:
            req = urllib.request.Request(f"{BASE}/{op}", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            raw = urllib.request.urlopen(req, timeout=timeout).read()
    except urllib.error.URLError as e:
        raise EngineError(f"engine op {op!r} failed: {e}") from e
    except OSError as e:
        raise EngineError(f"engine op {op!r} failed: {e}") from e
    try:
        out = json.loads(raw)
    except ValueError:
        raise EngineError(f"engine op {op!r} returned non-JSON ({len(raw)} bytes)")
    if isinstance(out, dict) and out.get("error"):
        raise EngineError(f"engine op {op!r}: {out['error']}")
    return out


def call(op, **body):
    ensure_engine()
    return call_raw(op, **body)


# ── html -> readable text ───────────────────────────────────────────────────────────────────────

_SKIP = {"script", "style", "noscript", "svg", "template", "head", "iframe", "canvas"}
_BLOCK = {"p", "div", "section", "article", "header", "footer", "nav", "main", "aside", "br",
          "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "table", "ul", "ol", "dl", "dt", "dd",
          "blockquote", "pre", "form", "figure", "figcaption", "hr"}


class _Text(HTMLParser):
    """HTML -> plain text. Drops script/style/etc, keeps block boundaries as newlines, and can keep
    links as `text <href>` so an agent can navigate without a second HTML round-trip."""

    def __init__(self, links=False):
        super().__init__(convert_charrefs=True)
        self.out, self.skip, self.links, self._href = [], 0, links, None

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self.skip += 1
            return
        if tag in _BLOCK:
            self.out.append("\n")
        if tag == "a" and self.links:
            self._href = dict(attrs).get("href")
        if tag == "img":
            alt = dict(attrs).get("alt")
            if alt:
                self.out.append(f"[img: {alt}]")

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if tag in _BLOCK:
            self.out.append("\n")
        if tag == "a" and self.links and self._href:
            if not self._href.startswith(("javascript:", "#")):
                self.out.append(f" <{self._href}>")
            self._href = None

    def handle_data(self, d):
        if not self.skip and d.strip():
            self.out.append(d.strip() + " ")

    def text(self):
        s = "".join(self.out)
        lines, prev_blank = [], False
        for ln in s.split("\n"):
            ln = " ".join(ln.split())
            if not ln:
                if prev_blank:
                    continue
                prev_blank = True
            else:
                prev_blank = False
            lines.append(ln)
        return "\n".join(lines).strip()


def html_to_text(h, links=False):
    p = _Text(links=links)
    try:
        p.feed(h or "")
    except Exception as e:                      # malformed markup must degrade, never fail a fetch
        elog(f"[{NAME}] html parse warning: {e}")
    return p.text()


def render(page, mode, max_chars):
    """page = {html,title,url} from the engine -> the string the agent sees."""
    if mode == "html":
        body = page.get("html") or ""
    else:
        body = html_to_text(page.get("html"), links=(mode == "text+links"))
    head = f"# {page.get('title') or '(no title)'}\nURL: {page.get('url') or ''}\n\n"
    total = len(body)
    if max_chars and total > max_chars:
        body = (body[:max_chars]
                + f"\n\n[truncated: showing {max_chars} of {total} chars. Re-call with a larger "
                  f"max_chars, or use the `js` tool to extract just the part you need.]")
    return head + body


CHALLENGE = ("just a moment", "verify you are human", "checking your browser",
             "cf-chl", "challenge-platform")


def is_challenge(page):
    t = (page.get("title") or "").lower()
    h = (page.get("html") or "").lower()
    return any(k in t for k in CHALLENGE) or "verify you are human" in h


def _open_tab():
    """Open a scratch tab and return its index. The engine makes the new tab the ACTIVE one, so the
    ops that follow apply to it."""
    return call("newtab", url="about:blank").get("index", -1)


def _close_tab(index, url=None):
    """Close the tab we opened. Indices are positional and shift when a LOWER-indexed tab closes, so
    prefer an exact url match and fall back to the index we were given. Never touches tab 0."""
    try:
        tabs = call_raw("tabs", method="GET").get("tabs", [])
        if url:
            for t in tabs:
                if t["index"] != 0 and t.get("url") == url:
                    return call("closetab", index=t["index"])
        if index and index > 0:
            return call("closetab", index=index)
    except EngineError as e:
        elog(f"[{NAME}] tab cleanup failed (leaked tab {index}): {e}")
    return {"closed": 0}


def _solve_timeout(tries):
    """Each solve iteration is a find(timeout=3) + up to 5s of settle sleeps, so ~8s worst case per
    try, plus 30s of slack for the final page load."""
    return tries * 8 + 30


# ── tools ───────────────────────────────────────────────────────────────────────────────────────

def t_fetch(url, mode="text", solve=True, max_chars=40000, tries=20):
    with _capture_lock:
        idx = _open_tab()
        final_url = None
        try:
            call("goto", url=url, timeout=90.0)
            page = call("text")
            solved = None
            if solve and is_challenge(page):
                solved = call("solve", tries=tries, timeout=_solve_timeout(tries))
                page = call("text")
            final_url = page.get("url")
            body = render(page, mode, max_chars)
            if solved is not None:
                body += (f"\n\n[cloudflare challenge: "
                         f"{'cleared' if solved.get('passed') else 'NOT cleared'} after "
                         f"{solved.get('iter')} attempts]")
            return body
        finally:
            _close_tab(idx, final_url)


def t_screenshot(url=None, solve=True):
    if url is None:
        b64 = call("shot", timeout=90.0)["b64"]
        return [{"type": "image", "data": b64, "mimeType": "image/png"}]
    with _capture_lock:
        idx = _open_tab()
        final_url = None
        try:
            call("goto", url=url, timeout=90.0)
            page = call("text")
            if solve and is_challenge(page):
                call("solve", tries=20, timeout=_solve_timeout(20))
                page = call("text")
            final_url = page.get("url")
            b64 = call("shot", timeout=90.0)["b64"]
            return [{"type": "text", "text": f"{page.get('title') or ''} — {final_url}"},
                    {"type": "image", "data": b64, "mimeType": "image/png"}]
        finally:
            _close_tab(idx, final_url)


def t_goto(url, solve=True, mode="text", max_chars=8000):
    call("goto", url=url, timeout=90.0)
    page = call("text")
    note = ""
    if solve and is_challenge(page):
        r = call("solve", tries=20, timeout=_solve_timeout(20))
        page = call("text")
        note = f"\n\n[challenge {'cleared' if r.get('passed') else 'NOT cleared'}]"
    return render(page, mode, max_chars) + note


def t_read(mode="text", max_chars=40000):
    return render(call("text"), mode, max_chars)


def t_js(expr):
    return json.dumps(call("js", expr=expr).get("result"), indent=2, default=str)


def t_click(x, y):
    return json.dumps(call("click", x=x, y=y))


def t_key(key):
    return json.dumps(call("key", key=key))


def t_solve(tries=20):
    return json.dumps(call("solve", tries=tries, timeout=_solve_timeout(tries)))


def t_cookies(domain=None):
    cks = call("cookies").get("cookies", [])
    if domain:
        cks = [c for c in cks if domain in (c.get("domain") or "")]
    return json.dumps(cks, indent=2)


def t_tabs():
    return json.dumps(call_raw("tabs", method="GET"), indent=2)


def t_close_tab(index=None, url=None, close_extra=False):
    if close_extra:
        return json.dumps(call("closeextra"))
    if index is None and url is None:
        raise ValueError("give index or url, or set close_extra:true")
    return json.dumps(call("closetab", index=index, url=url))


def t_status():
    try:
        st = call_raw("status", method="GET")
    except EngineError:
        return json.dumps({"server": False, "alive": False, "port": PORT,
                           "hint": "engine not running; any other tool will auto-start it"})
    st["port"] = PORT
    st["repo"] = REPO
    if st.get("alive"):
        try:
            st["tabs"] = call_raw("tabs", method="GET").get("count")
        except EngineError:
            pass
    return json.dumps(st, indent=2)


_TEXT_MODE = {"type": "string", "enum": ["text", "text+links", "html"], "default": "text",
              "description": "text = readable text (default, ~10x smaller than html); text+links "
                             "keeps hrefs as `anchor <url>` so you can navigate; html = raw source "
                             "(only when you need markup/attributes)."}

TOOLS = [
    dict(name="fetch", fn=t_fetch,
         description=(
             "Fetch ONE web page through a real headed Chrome and return it as readable text. Use "
             "this for any page that plain HTTP (curl/WebFetch) can't get: Cloudflare/Turnstile "
             "walls, bot detection, JS-rendered content, or pages needing a logged-in session. This "
             "is the tool you want 90% of the time — it opens its own tab, navigates, auto-detects "
             "and clears a Cloudflare challenge, extracts the content, and closes the tab, all in "
             "one call. Do NOT use for PDFs (curl the file and run `pdftotext -layout`)."),
         schema={"type": "object", "required": ["url"], "properties": {
             "url": {"type": "string", "description": "Absolute URL, including scheme."},
             "mode": _TEXT_MODE,
             "solve": {"type": "boolean", "default": True,
                       "description": "Auto-clear a Cloudflare challenge if one is detected."},
             "max_chars": {"type": "integer", "default": 40000,
                           "description": "Truncate the body at this many characters."},
             "tries": {"type": "integer", "default": 20,
                       "description": "Max challenge-solve iterations."}}}),

    dict(name="screenshot", fn=t_screenshot,
         description=(
             "Screenshot a page as a PNG you can actually look at. With `url`, it loads that page in "
             "its own tab (clearing a challenge if needed) and closes the tab after. With no url, it "
             "shoots the browser's current page — use that to see the result of goto/click/js."),
         schema={"type": "object", "properties": {
             "url": {"type": "string", "description": "Optional; omit to shoot the current page."},
             "solve": {"type": "boolean", "default": True}}}),

    dict(name="goto", fn=t_goto,
         description=(
             "Navigate the browser's CURRENT tab to a url and return a short text preview. This "
             "starts an interactive session: the page stays open for click/key/js/read/screenshot, "
             "and cookies persist. For a one-shot capture use `fetch` instead — it cleans up after "
             "itself, this does not."),
         schema={"type": "object", "required": ["url"], "properties": {
             "url": {"type": "string"},
             "solve": {"type": "boolean", "default": True},
             "mode": _TEXT_MODE,
             "max_chars": {"type": "integer", "default": 8000}}}),

    dict(name="read", fn=t_read,
         description="Re-read the browser's current page (after click/key/js changed it).",
         schema={"type": "object", "properties": {
             "mode": _TEXT_MODE, "max_chars": {"type": "integer", "default": 40000}}}),

    dict(name="js", fn=t_js,
         description=(
             "Evaluate a JavaScript expression in the current page and return its value. The precise "
             "tool: use it to extract one field, fill an input, submit a form, or scroll — instead of "
             "pulling the whole page into context. Example: "
             "`[...document.querySelectorAll('h2')].map(e=>e.textContent)`."),
         schema={"type": "object", "required": ["expr"], "properties": {
             "expr": {"type": "string", "description": "A JS expression (its value is returned)."}}}),

    dict(name="click", fn=t_click,
         description=("Synthetic CDP mouse click at viewport CSS coordinates (does not move the "
                      "real cursor). Get coordinates from `screenshot`, or prefer `js` with "
                      ".click() when you can select the element."),
         schema={"type": "object", "required": ["x", "y"], "properties": {
             "x": {"type": "number"}, "y": {"type": "number"}}}),

    dict(name="key", fn=t_key,
         description=("Send one key to the page: a named key (Enter, Tab, Escape, Backspace, "
                      "ArrowDown/Up/Left/Right, PageUp, PageDown, Home, End, Space, Delete) or a "
                      "single printable character. To type a string, send it via `js` instead."),
         schema={"type": "object", "required": ["key"], "properties": {"key": {"type": "string"}}}),

    dict(name="solve", fn=t_solve,
         description=("Manually clear a Cloudflare Turnstile challenge on the current page. `fetch` "
                      "and `goto` already do this automatically — only reach for this if a wall "
                      "appears mid-session."),
         schema={"type": "object", "properties": {"tries": {"type": "integer", "default": 20}}}),

    dict(name="cookies", fn=t_cookies,
         description="List the shared browser's cookies (including a cleared cf_clearance).",
         schema={"type": "object", "properties": {
             "domain": {"type": "string", "description": "Optional domain substring filter."}}}),

    dict(name="tabs", fn=t_tabs,
         description="List every open tab (index, url, title, active). Use it to find leaked tabs.",
         schema={"type": "object", "properties": {}}),

    dict(name="close_tab", fn=t_close_tab,
         description=("Close a tab by index or url-substring, or set close_extra to close every tab "
                      "except the base one (the cleanup button after an aborted run). `fetch` and "
                      "`screenshot` close their own tabs, so this is only for tabs `goto` opened or "
                      "tabs another agent leaked. Tab 0 is protected."),
         schema={"type": "object", "properties": {
             "index": {"type": "integer"}, "url": {"type": "string"},
             "close_extra": {"type": "boolean", "default": False}}}),

    dict(name="status", fn=t_status,
         description=("Is the engine up, is Chrome launched, how many tabs, which port. Diagnostic "
                      "only — you never need to call this first, every other tool auto-starts the "
                      "engine."),
         schema={"type": "object", "properties": {}}),
]
BY_NAME = {t["name"]: t for t in TOOLS}

INSTRUCTIONS = """playwrong drives ONE shared, long-running, headed Chrome that beats Cloudflare
Turnstile. Reach for it when plain HTTP fetching fails: bot walls, JS-rendered pages, or anything
needing a persistent session. `fetch` is the one-call answer for a single page. Rules that matter:
never launch your own browser or kill Chrome (it is shared, and killing it loses the cleared
Turnstile session); close any tab `goto` opened; PDFs should be curl'd and read with pdftotext, not
opened here."""


# ── JSON-RPC / MCP plumbing ─────────────────────────────────────────────────────────────────────

def tool_result(value):
    """Normalise a handler's return into MCP content blocks."""
    if isinstance(value, list):            # already content blocks (screenshot)
        return {"content": value}
    return {"content": [{"type": "text", "text": value if isinstance(value, str) else str(value)}]}


def handle(msg):
    """Returns a response dict, or None for notifications (which must NEVER get a reply)."""
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}

    def ok(result):
        return None if mid is None else {"jsonrpc": "2.0", "id": mid, "result": result}

    def err(code, message):
        return None if mid is None else {"jsonrpc": "2.0", "id": mid,
                                         "error": {"code": code, "message": message}}

    if method == "initialize":
        # Echo the client's protocol revision when it sends one — the spec's negotiation rule.
        want = params.get("protocolVersion")
        return ok({"protocolVersion": want if isinstance(want, str) else PROTOCOL,
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": NAME, "version": VERSION},
                   "instructions": INSTRUCTIONS})
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [{"name": t["name"], "description": t["description"],
                              "inputSchema": t["schema"]} for t in TOOLS]})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = BY_NAME.get(name)
        if not tool:
            return err(-32602, f"unknown tool {name!r}; have: {', '.join(BY_NAME)}")
        try:
            return ok(tool_result(tool["fn"](**args)))
        except TypeError as e:                       # bad/missing arguments from the model
            return ok({"content": [{"type": "text", "text": f"bad arguments for {name}: {e}"}],
                       "isError": True})
        except (EngineError, ValueError) as e:
            return ok({"content": [{"type": "text", "text": str(e)}], "isError": True})
        except Exception as e:                        # never let one bad call kill the server
            elog(f"[{NAME}] tool {name} crashed: {e!r}")
            return ok({"content": [{"type": "text", "text": f"{name} failed: {e!r}"}],
                       "isError": True})
    return err(-32601, f"method not found: {method}")


def main():
    elog(f"[{NAME}] mcp stdio server up — repo={REPO} engine_port={PORT}")
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError as e:
            out.write(json.dumps({"jsonrpc": "2.0", "id": None,
                                  "error": {"code": -32700, "message": f"parse error: {e}"}}) + "\n")
            out.flush()
            continue
        resp = handle(msg)
        if resp is not None:
            out.write(json.dumps(resp) + "\n")
            out.flush()
    elog(f"[{NAME}] stdin closed — exiting (the engine keeps running; it is shared)")


if __name__ == "__main__":
    main()
