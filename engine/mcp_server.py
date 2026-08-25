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
from engine import connect  # noqa: E402
from engine.connect import EngineError, call, capture, is_challenge, render  # noqa: E402

PORT = connect.default_port()
NAME, VERSION = "playwrong", "0.1.0"

# Newest protocol revision we implement. If the client asks for a different one we echo THEIRS back
# (the spec's negotiation rule) — every revision so far is wire-compatible for tools-only servers.
PROTOCOL = "2025-06-18"


def elog(*a):
    """Diagnostics -> stderr. MCP clients capture stderr as the server's log; stdout is the wire."""
    print(*a, file=sys.stderr, flush=True)


def op(name, **body):
    """Every engine op goes through here: the engine and Chrome are guaranteed up first.

    An op aimed at THIS agent's tab is retried once if that tab has gone. A tab can disappear from
    under a caller — the page closes it, another agent runs close_extra, the browser is relaunched
    after dying — and the engine then raises KeyError: no such tab. That is correct for a bad
    reference and wrong for our own tag: the tab is re-openable, and every interactive op stayed
    wedged until someone guessed the close_extra workaround (issue #13).
    """
    global _have_tab
    try:
        return connect.op(name, **body)
    except EngineError as e:
        if body.get("tab") != _MY_TAB or "no such tab" not in str(e):
            raise
        _have_tab = False          # it is gone; my_tab() opens a fresh one under the same tag
        my_tab()
        return connect.op(name, **body)


# THIS agent's own tab inside the shared browser. Interactive tools (goto/read/js/click/key) drive it
# BY TAG rather than "whatever tab is active", so several agents can work in one browser at once
# without moving each other's pages out from under them. fetch/pdf/search don't need it — each opens
# and closes a tagged tab of its own per call.
_MY_TAB = f"{connect.SESSION}-mcp"
_have_tab = False


def my_tab():
    """The tag of this agent's tab, opened on first use."""
    global _have_tab
    if not _have_tab:
        # owner=, so `playwrong --tabs` can answer "who opened that one?" for MCP callers too — it
        # showed "-" for every agent tab before — and so close_extra can tell whose tab is whose.
        op("newtab", url="about:blank", tag=_MY_TAB, owner=connect.OWNER)
        _have_tab = True
    return _MY_TAB


# ── tools ───────────────────────────────────────────────────────────────────────────────────────
# Each tool is a thin shim over engine/connect.py, which is also what the `playwrong` CLI uses — one
# implementation of "start the engine, drive it, clean up", not two that drift apart.

def t_fetch(url, mode="text", solve=True, max_chars=40000, tries=20):
    r = capture(url, mode=mode, solve=solve, max_chars=max_chars, tries=tries)
    body = r["text"]
    if r.get("challenge"):
        body += f"\n\n[cloudflare challenge: {r['challenge']}]"
    return body


def t_pdf(url, path=None, max_chars=40000):
    r = connect.pdf(url, path=path)
    # The file is always written, so always say where and what — a caller keeping the document needs
    # the path, and one checking it got the whole thing needs the page count. Reporting these only in
    # the failure branches (as this used to) hands back a saved file nobody is told about.
    saved = f"Saved: {r['path']} ({r['bytes']} bytes"
    if r.get("pages"):
        saved += f", {r['pages']} pages"
    saved += ")"
    if r.get("final_url") and r["final_url"] != url:
        saved += f"\nFinal URL after redirects: {r['final_url']}"
    if r.get("warning"):
        return f"{r['warning']}\n{saved}"
    head = f"# {os.path.basename(r['path'])}\nURL: {url}\n{saved}\n"
    if r.get("text") is None:
        return (f"{head}\n`pdftotext` is not installed so the text can't be extracted here. Install "
                f"poppler-utils, or read the file yourself — it is a valid PDF and the bot wall has "
                f"already been cleared.")
    text, total = r["text"], len(r["text"])
    if max_chars and total > max_chars:
        text = text[:max_chars] + f"\n\n[truncated: {max_chars} of {total} chars; whole file on disk]"
    return f"{head}\n{text}"


def t_download(url, path=None, expect_sha256=None, expect_size=None):
    r = connect.download(url, path=path, expect_sha256=expect_sha256, expect_size=expect_size)
    lines = [f"Saved: {r['path']}",
             f"Bytes: {r['bytes']:,}",
             f"SHA256: {r['sha256']}"]
    if r.get("content_type"):
        lines.append(f"Content-Type: {r['content_type']}")
    if r.get("final_url") and r["final_url"] != url:
        lines.append(f"Final URL after redirects: {r['final_url']}")
    if expect_sha256 or expect_size:
        lines.append("Verified against the expected value you passed.")
    else:
        lines.append("NOT verified — pass expect_sha256 if the publisher states one. A block page "
                     "or a truncated transfer saves without error and looks like a real file.")
    return "\n".join(lines)


def t_prefetch(urls, concurrency=8, timeout=30):
    job = connect.prefetch(urls, concurrency=concurrency, timeout=timeout)
    return (f"started job {job}: {len(urls)} urls loading, {concurrency} at a time.\n"
            f"Go do something else, then call `collect` with job=\"{job}\" to take the pages that "
            f"are ready. Call it again for the rest.")


def t_collect(job, wait=0, max_chars=40000):
    r = connect.collect(job, max_chars=max_chars, wait=wait, want=1)
    if not r["results"]:
        return (f"nothing ready yet for {job} ({r.get('remaining', '?')} still loading). "
                f"Call collect again, or pass wait=10 to block for up to 10s.")
    parts = []
    for item in r["results"]:
        if item.get("error"):
            parts.append(f"## {item['url']}\nFAILED: {item['error']}")
        else:
            parts.append(item["text"])
    return (f"[{len(r['results'])} ready, {r.get('remaining', 0)} still loading]\n\n"
            + "\n\n———\n\n".join(parts))


def _numbered(hits):
    return "\n".join(f"{i:2}. {h['title']}\n    {h['url']}" for i, h in enumerate(hits, 1))


def t_search(query, max_results=10):
    hits = connect.search(query, max_results=max_results)
    if hits:
        return _numbered(hits)
    # An empty result set is a RESULT. This used to say "no results parsed — DuckDuckGo may have
    # changed its markup", which reads as "the search did not run" and had agents recording "no such
    # thing exists" for queries that simply matched nothing (#10, #12). A genuine parse failure
    # raises from connect.search() instead, and says what arrived.
    head = f"No results. The search ran; DuckDuckGo matched nothing for: {query}"
    loose = connect.relax(query)
    if not loose:
        return (f"{head}\nNothing to relax — no quotes or operators to drop. The terms themselves "
                f"are what matched nothing; try different or fewer words.")
    # Run the relaxation here rather than telling the caller to. Zero results is exactly the moment
    # an agent concludes "this does not exist" and stops, and the retry it should have made is
    # mechanical. Kept LOUDLY separate: answering a question nobody asked, unlabelled, is the same
    # silent-wrong-answer failure this project exists to prevent.
    loose_hits = connect.search(loose, max_results=max_results)
    if not loose_hits:
        return (f"{head}\nAlso retried relaxed (quotes and operators dropped): {loose}\n"
                f"That matched nothing either — the terms are likely not indexed at all, rather "
                f"than the phrasing being too strict.")
    return (f"{head}\n\nRetried relaxed (quotes and operators dropped): {loose}\n\n"
            f"{_numbered(loose_hits)}\n\n"
            f"⚠ These answer the RELAXED query, NOT yours. Your exact phrase matched nothing — do "
            f"not record these as exact-phrase hits. Open one with `fetch` to check whether it "
            f"actually contains what you were looking for.")


def t_screenshot(url=None, solve=True):
    if url is None:
        return [{"type": "image", "data": op("shot", tab=my_tab(), timeout=90.0)["b64"],
                 "mimeType": "image/png"}]
    r = capture(url, solve=solve, max_chars=200, shot=True)
    return [{"type": "text", "text": f"{r.get('title') or ''} — {r.get('url') or ''}"},
            {"type": "image", "data": r["b64"], "mimeType": "image/png"}]


def t_goto(url, solve=True, mode="text", max_chars=8000):
    tab = my_tab()
    op("goto", url=url, tab=tab, timeout=90.0)
    page = op("text", tab=tab)
    note = ""
    if solve and is_challenge(page):
        r = op("solve", tries=20, tab=tab, timeout=connect.solve_timeout(20))
        page = op("text", tab=tab)
        note = f"\n\n[challenge {'cleared' if r.get('passed') else 'NOT cleared'}]"
    return render(page, mode, max_chars) + note


def t_read(mode="text", max_chars=40000):
    return render(op("text", tab=my_tab()), mode, max_chars)


def t_js(expr):
    return json.dumps(op("js", expr=expr, tab=my_tab()).get("result"), indent=2, default=str)


def t_click(x, y):
    return json.dumps(op("click", x=x, y=y, tab=my_tab()))


def t_key(key):
    return json.dumps(op("key", key=key, tab=my_tab()))


def t_solve(tries=20):
    return json.dumps(op("solve", tries=tries, tab=my_tab(),
                         timeout=connect.solve_timeout(tries)))


def t_cookies(domain=None):
    cks = op("cookies").get("cookies", [])
    if domain:
        cks = [c for c in cks if domain in (c.get("domain") or "")]
    return json.dumps(cks, indent=2)


def t_tabs():
    return json.dumps(op("tabs", method="GET"), indent=2)


def t_close_tab(index=None, url=None, close_extra=False, force=False):
    global _have_tab
    if close_extra:
        _have_tab = False
        return json.dumps(op("closeextra", owner=connect.OWNER, force=force))
    if index is None and url is None:
        # No target given: close YOUR OWN tab. That is the common case for an agent finishing up,
        # and it can never take out another agent's page by accident.
        if not _have_tab:
            return json.dumps({"closed": 0, "reason": "you have no open tab"})
        _have_tab = False
        return json.dumps(op("closetab", tag=_MY_TAB))
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
             "one call. Do NOT use for PDFs — use the `pdf` tool, which handles them properly."),
         schema={"type": "object", "required": ["url"], "properties": {
             "url": {"type": "string", "description": "Absolute URL, including scheme."},
             "mode": _TEXT_MODE,
             "solve": {"type": "boolean", "default": True,
                       "description": "Auto-clear a Cloudflare challenge if one is detected."},
             "max_chars": {"type": "integer", "default": 40000,
                           "description": "Truncate the body at this many characters."},
             "tries": {"type": "integer", "default": 20,
                       "description": "Max challenge-solve iterations."}}}),

    dict(name="pdf", fn=t_pdf,
         description=(
             "Get a PDF — as text, and as a file kept on disk. Use this for ANY pdf url, in place of "
             "`fetch` and in place of curl. It clears the challenge in the real browser, downloads "
             "through that cleared session (cookies + matching User-Agent), saves the file, and "
             "extracts the text; it reports the path, byte count, page count and post-redirect url. "
             "Pass `path` to keep the document somewhere permanent (`sources/foo.pdf`) rather than "
             "scratch. Prefer this over curl even for a pdf that looks unprotected: whether a url is "
             "protected is not knowable in advance, and the failures are silent — a 200 whose body "
             "is a login page, or a 45-page datasheet that arrives as 10 pages. Both save without "
             "error and neither looks wrong. This tool checks the file really is a PDF and tells you "
             "the page count, so a short or substituted document is visible."),
         schema={"type": "object", "required": ["url"], "properties": {
             "url": {"type": "string", "description": "Direct url to the PDF."},
             "path": {"type": "string",
                      "description": "Where to write the file. Relative paths resolve against the "
                                     "engine's working directory, so prefer an absolute path. "
                                     "Missing parent directories are created; an existing file at "
                                     "this path is overwritten. Default: scratch (tmp/)."},
             "max_chars": {"type": "integer", "default": 40000,
                           "description": "Truncate the extracted TEXT. The saved file is always "
                                          "complete."}}}),

    dict(name="download", fn=t_download,
         description=(
             "Download ANY file through the cleared browser session and keep it: firmware images, "
             "archives, installers, release tarballs, anything that is not a page. Use this instead "
             "of curl/wget for every binary — the same silent failures apply (a 200 whose body is a "
             "login page saves happily as firmware.bin). Streams to disk in chunks, so a "
             "hundreds-of-MB file does not go through memory. Reports the path, byte count, sha256, "
             "content-type and post-redirect url. Pass expect_sha256 (or expect_size) when the "
             "publisher states one and it is checked for you — that is the binary equivalent of the "
             "page count on `pdf`. For a PDF prefer `pdf`, which also extracts the text."),
         schema={"type": "object", "required": ["url"], "properties": {
             "url": {"type": "string", "description": "Direct url to the file."},
             "path": {"type": "string",
                      "description": "Where to write it. Relative paths resolve against the engine's "
                                     "working directory, so prefer an absolute path. Missing parent "
                                     "directories are created; an existing file is overwritten. "
                                     "Default: scratch (tmp/) under the url's basename."},
             "expect_sha256": {"type": "string",
                               "description": "Publisher-stated sha256. Mismatch raises, and the "
                                              "file is kept as evidence."},
             "expect_size": {"type": "integer", "description": "Expected size in bytes."}}}),

    dict(name="prefetch", fn=t_prefetch,
         description=(
             "Start loading MANY urls at once and return immediately with a job id — then call "
             "`collect` for the pages as they finish. Use this instead of looping `fetch` whenever "
             "you have several urls: a page load is almost entirely waiting, so loading 8 at a time "
             "overlaps the waiting and you read results while the rest are still loading. Each url "
             "gets its own tab, and a url that stalls is timed out (with whatever rendered salvaged) "
             "rather than holding up the others."),
         schema={"type": "object", "required": ["urls"], "properties": {
             "urls": {"type": "array", "items": {"type": "string"},
                      "description": "Absolute URLs to load."},
             "concurrency": {"type": "integer", "default": 8,
                             "description": "How many tabs load at once."},
             "timeout": {"type": "integer", "default": 30,
                         "description": "Per-url seconds before it is timed out. Raise it for "
                                        "pages behind a Cloudflare challenge."}}}),

    dict(name="collect", fn=t_collect,
         description=(
             "Take the pages from a `prefetch` job that are READY, as readable text. Returns "
             "immediately with whatever has finished and tells you how many are still loading — "
             "call it again for the rest. Results are handed over once and then forgotten, so each "
             "page comes back exactly once."),
         schema={"type": "object", "required": ["job"], "properties": {
             "job": {"type": "string", "description": "The job id from `prefetch`."},
             "wait": {"type": "integer", "default": 0,
                      "description": "Seconds to wait for at least one result before returning. 0 = "
                                     "take what is ready right now."},
             "max_chars": {"type": "integer", "default": 40000}}}),

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
             "itself, this does not. THIS IS THE LOGIN PATH: the browser is headed and the user is "
             "sitting in front of it, so for a page needing an account, `goto` it, then stop and ask "
             "the user to sign in to that tab (it is labelled with your agent name) — do not sleep "
             "or poll, just end your turn. `read` when they confirm. Never take the credentials "
             "yourself: no password in chat, none typed via js/key, none read from a file."),
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
         description=("Close a tab by index or url-substring, or set close_extra to clean up. "
                      "`fetch`, `pdf` and `screenshot` close their own tabs, so this is only for "
                      "tabs `goto` opened. Tab 0 is protected. close_extra closes YOUR tabs plus "
                      "any left by an agent whose process has exited — other agents' live tabs are "
                      "left alone and reported back, because this browser is shared and their page "
                      "is mid-use. Set force only when you mean to close other agents' pages too."),
         schema={"type": "object", "properties": {
             "index": {"type": "integer"}, "url": {"type": "string"},
             "close_extra": {"type": "boolean", "default": False},
             "force": {"type": "boolean", "default": False,
                       "description": "With close_extra: also close other agents' live tabs."}}}),

    dict(name="status", fn=t_status,
         description=("Is the engine up, is Chrome launched, how many tabs, which port. Diagnostic "
                      "only — you never need to call this first, every other tool auto-starts the "
                      "engine."),
         schema={"type": "object", "properties": {}}),
]
BY_NAME = {t["name"]: t for t in TOOLS}

INSTRUCTIONS = """playwrong drives ONE shared, long-running, headed Chrome that beats Cloudflare
Turnstile. `fetch` is the one-call answer for a single page.

USE IT FOR EVERY URL — including a url you already have, a raw file, or one that looks simple. Not
curl, not wget, not urllib, not your own web-fetch tool. Fetching any other way silently loses what
this exists for, and the failures return success: a 200 whose body is a login page, a 45-page
datasheet that arrives as 10, a search that answers HTTP 202 + a CAPTCHA, a JS-rendered page that
arrives empty. A status code cannot tell you any of this. If a tool here misbehaves, that is a bug to
report, not a reason to reach for curl.

Rules that matter: never launch your own browser or kill Chrome (it is shared, and killing it loses
the cleared Turnstile session); close any tab `goto` opened; PDFs go through `pdf`, never `fetch` and
never curl — it returns the text AND keeps the file (pass `path`), and reports the page count that
reveals a truncated document. For a page behind a login, `goto` it and ask the USER to sign in to
that tab, then `read` — don't poll, and never handle their credentials yourself."""


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
