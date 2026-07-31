"""engine/mcp_server.py — MCP (stdio) front-end for the playwrong capture engine.

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
    python engine/mcp_server.py            # then type JSON-RPC lines, or use scripts/mcp_selftest.py
"""
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from engine import connect                                                    # noqa: E402
from engine.connect import EngineError, call, capture, render, is_challenge   # noqa: E402

PORT = connect.default_port()
NAME, VERSION = "playwrong", "0.1.0"

# Newest protocol revision we implement. If the client asks for a different one we echo THEIRS back
# (the spec's negotiation rule) — every revision so far is wire-compatible for tools-only servers.
PROTOCOL = "2025-06-18"


def elog(*a):
    """Diagnostics -> stderr. MCP clients capture stderr as the server's log; stdout is the wire."""
    print(*a, file=sys.stderr, flush=True)


def op(name, **body):
    """Every engine op goes through here: the engine and Chrome are guaranteed up first."""
    return connect.op(name, **body)


# ── tools ───────────────────────────────────────────────────────────────────────────────────────
# Each tool is a thin shim over engine/connect.py, which is also what the `playwrong` CLI uses — one
# implementation of "start the engine, drive it, clean up", not two that drift apart.

def t_fetch(url, mode="text", solve=True, max_chars=40000, tries=20):
    r = capture(url, mode=mode, solve=solve, max_chars=max_chars, tries=tries)
    body = r["text"]
    if r.get("challenge"):
        body += f"\n\n[cloudflare challenge: {r['challenge']}]"
    return body


def t_search(query, max_results=10):
    hits = connect.search(query, max_results=max_results)
    if not hits:
        return "no results parsed — DuckDuckGo may have changed its markup"
    return "\n".join(f"{i:2}. {h['title']}\n    {h['url']}" for i, h in enumerate(hits, 1))


def t_screenshot(url=None, solve=True):
    if url is None:
        return [{"type": "image", "data": op("shot", timeout=90.0)["b64"], "mimeType": "image/png"}]
    r = capture(url, solve=solve, max_chars=200, shot=True)
    return [{"type": "text", "text": f"{r.get('title') or ''} — {r.get('url') or ''}"},
            {"type": "image", "data": r["b64"], "mimeType": "image/png"}]


def t_goto(url, solve=True, mode="text", max_chars=8000):
    op("goto", url=url, timeout=90.0)
    page = op("text")
    note = ""
    if solve and is_challenge(page):
        r = op("solve", tries=20, timeout=connect.solve_timeout(20))
        page = op("text")
        note = f"\n\n[challenge {'cleared' if r.get('passed') else 'NOT cleared'}]"
    return render(page, mode, max_chars) + note


def t_read(mode="text", max_chars=40000):
    return render(op("text"), mode, max_chars)


def t_js(expr):
    return json.dumps(op("js", expr=expr).get("result"), indent=2, default=str)


def t_click(x, y):
    return json.dumps(op("click", x=x, y=y))


def t_key(key):
    return json.dumps(op("key", key=key))


def t_solve(tries=20):
    return json.dumps(op("solve", tries=tries, timeout=connect.solve_timeout(tries)))


def t_cookies(domain=None):
    cks = op("cookies").get("cookies", [])
    if domain:
        cks = [c for c in cks if domain in (c.get("domain") or "")]
    return json.dumps(cks, indent=2)


def t_tabs():
    return json.dumps(op("tabs", method="GET"), indent=2)


def t_close_tab(index=None, url=None, close_extra=False):
    if close_extra:
        return json.dumps(op("closeextra"))
    if index is None and url is None:
        raise ValueError("give index or url, or set close_extra:true")
    return json.dumps(op("closetab", index=index, url=url))


def t_status():
    """The one tool that must NOT auto-start anything — its job is to report what is true now."""
    if not connect.reachable(PORT):
        return json.dumps({"server": False, "alive": False, "port": PORT, "repo": REPO,
                           "hint": "engine not running; any other tool will auto-start it"})
    st = call("status", port=PORT, method="GET")
    st["port"], st["repo"] = PORT, REPO
    if st.get("alive"):
        try:
            st["tabs"] = call("tabs", port=PORT, method="GET").get("count")
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

    dict(name="search", fn=t_search,
         description=(
             "Web search via DuckDuckGo, through the real browser — returns numbered title + url "
             "results. Use this when a plain HTTP search request gets blocked: DuckDuckGo now "
             "answers curl-like clients with an image CAPTCHA (HTTP 202 and a 'select all squares "
             "containing a duck' page) instead of results, on both the lite and html endpoints. A "
             "real browser is not challenged. Follow up with `fetch` on whichever result you want."),
         schema={"type": "object", "required": ["query"], "properties": {
             "query": {"type": "string", "description": "Search terms."},
             "max_results": {"type": "integer", "default": 10}}}),

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
