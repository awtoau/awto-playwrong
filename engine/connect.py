"""connect.py — the ONE place that knows how to reach the capture engine, and start it if it isn't up.

Before this existed, every caller (the CLI, the MCP server, docs/AGENT-API.md's copy-paste snippet,
each new crawler) reimplemented the same four things and got at least one of them wrong: spawn the
server with the right env, wait for the port but NOT for `alive`, force the lazy Chrome launch, and
convert a 400KB document into something readable. That is why "start the server first" kept showing
up in the docs as a manual step — it was never a design requirement, just missing code.

Nothing here needs starting by hand:

    from engine import connect
    print(connect.capture("https://example.com")["text"])   # engine + Chrome + solve + cleanup

Stdlib only, so importing it costs nothing and it can report a broken engine as an error rather than
an ImportError.
"""
import base64
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
LOGDIR = os.path.join(REPO, "tmp", "logs")
SERVER_LOG = os.path.join(LOGDIR, "playwrong-engine.log")


def default_port():
    return int(os.environ.get("PH_PORT", "8731"))


def base_url(port=None):
    return f"http://127.0.0.1:{port or default_port()}"


class EngineError(RuntimeError):
    pass


# ── reaching / starting the engine ──────────────────────────────────────────────────────────────

def reachable(port=None, timeout=2.0):
    """Is the engine's HTTP process answering? Checks reachability ONLY, never the `alive` field —
    `alive` (Chrome launched) stays false until a real op asks for a browser, so polling it in a loop
    waits forever. That trap has caught several callers; it is why this is a separate function."""
    try:
        urllib.request.urlopen(f"{base_url(port)}/status", timeout=timeout)
        return True
    except Exception:
        return False


def spawn(port=None):
    """Launch engine/server.py detached, output to a log file.

    No PYTHONPATH is set: server.py puts vendor/ on sys.path itself (and does it with
    sys.path.insert(0, ...), so the vendored nodriver wins over any site-packages copy). Callers
    that export PYTHONPATH=vendor are copying a step that has not been needed for a long time.
    """
    port = port or default_port()
    os.makedirs(LOGDIR, exist_ok=True)
    subprocess.Popen([sys.executable, os.path.join(REPO, "engine", "server.py")],
                     env={**os.environ, "PH_PORT": str(port)},
                     stdout=open(SERVER_LOG, "a"), stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, start_new_session=True)


def ensure(port=None, want_browser=True, on_start=None):
    """Guarantee an engine on `port`, and by default a launched Chrome behind it. Idempotent and
    cheap when everything is already up (one /status round-trip).

    Two waits for two different things:
      1. the HTTP process binding the port — a Python import plus a bind, no browser, so 20s is
         generous; polled at 0.25s to keep first-call latency low.
      2. Chrome launching — done by POSTing /start, which BLOCKS until the browser is up, so there is
         nothing to poll. Cold start is the slow part, hence 120s.

    on_start: optional callback, invoked once if we actually had to spawn something, so a CLI can say
    "starting the browser…" instead of appearing to hang.
    """
    port = port or default_port()
    started = False
    if not reachable(port):
        if on_start:
            on_start("starting the capture engine")
        started = True
        spawn(port)
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if reachable(port, timeout=1.0):
                break
            time.sleep(0.25)
        else:
            raise EngineError(
                f"engine did not bind 127.0.0.1:{port} within 20s. Check {SERVER_LOG}; the usual "
                f"cause is a missing dependency — run: {sys.executable} {REPO}/scripts/doctor.py")
    if want_browser and not call(port=port, op="status", method="GET").get("alive"):
        if on_start and not started:
            on_start("launching Chrome")
        elif on_start:
            on_start("launching Chrome")
        call(port=port, op="start", timeout=120.0)   # blocks until up; never poll /status for this
    return port


def call(op, port=None, method="POST", timeout=60.0, body=None, **kw):
    """One engine op. Raises EngineError on transport failure or an {"error": ...} payload. Does NOT
    auto-start — use ensure() first, or op() which does both.

    Body fields are normally just keyword arguments. `body=` is the escape hatch for a payload whose
    key collides with a parameter here (an op that genuinely needs to send a field called "method",
    "timeout" or "port")."""
    body = {**(body or {}), **kw}
    url = f"{base_url(port)}/{op}"
    try:
        if method == "GET":
            raw = urllib.request.urlopen(url, timeout=timeout).read()
        else:
            req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            raw = urllib.request.urlopen(req, timeout=timeout).read()
    except (urllib.error.URLError, OSError) as e:
        raise EngineError(f"engine op {op!r} failed: {e}") from e
    try:
        out = json.loads(raw)
    except ValueError:
        raise EngineError(f"engine op {op!r} returned non-JSON ({len(raw)} bytes)")
    if isinstance(out, dict) and out.get("error"):
        raise EngineError(f"engine op {op!r}: {out['error']}")
    return out


def op(name, port=None, on_start=None, **kw):
    """call() with the engine guaranteed up first. This is what callers should use."""
    port = ensure(port, on_start=on_start)
    return call(name, port=port, **kw)


# ── html -> readable text ───────────────────────────────────────────────────────────────────────

_SKIP = {"script", "style", "noscript", "svg", "template", "head", "iframe", "canvas"}
_BLOCK = {"p", "div", "section", "article", "header", "footer", "nav", "main", "aside", "br",
          "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "table", "ul", "ol", "dl", "dt", "dd",
          "blockquote", "pre", "form", "figure", "figcaption", "hr"}


class _Text(HTMLParser):
    """HTML -> plain text. Drops script/style/etc, keeps block boundaries as newlines, and can keep
    links as `text <href>` so a caller can navigate without a second HTML round-trip."""

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
        lines, prev_blank = [], False
        for ln in "".join(self.out).split("\n"):
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
    except Exception:              # malformed markup must degrade, never fail a capture
        pass
    return p.text()


def render(page, mode="text", max_chars=40000):
    """page = {html,title,url} -> the string a human or model reads."""
    body = (page.get("html") or "") if mode == "html" else html_to_text(
        page.get("html"), links=(mode == "text+links"))
    total = len(body)
    if max_chars and total > max_chars:
        body = body[:max_chars] + (f"\n\n[truncated: showing {max_chars} of {total} chars]")
    return f"# {page.get('title') or '(no title)'}\nURL: {page.get('url') or ''}\n\n{body}"


CHALLENGE = ("just a moment", "verify you are human", "checking your browser",
             "cf-chl", "challenge-platform")


def is_challenge(page):
    t = (page.get("title") or "").lower()
    h = (page.get("html") or "").lower()
    return any(k in t for k in CHALLENGE) or "verify you are human" in h


def solve_timeout(tries):
    """Each solve iteration is a find(timeout=3) plus up to 5s of settle sleeps — ~8s worst case per
    try — plus 30s of slack for the final page load."""
    return tries * 8 + 30


# ── the one-shot capture ────────────────────────────────────────────────────────────────────────

# The engine has ONE globally-active tab, and a capture is several ops against it, so two interleaved
# captures would steal each other's tab. This serialises them within a process. Across processes it
# does not help — use a second PH_PORT, or attach via /cdp (see docs/AGENT-API.md).
_lock = threading.Lock()


def capture(url, mode="text", solve=True, max_chars=40000, tries=20, port=None, shot=False,
            on_start=None):
    """Everything needed to get one page, in one call: start the engine and Chrome if they are down,
    open OUR OWN tab, navigate, clear a Cloudflare challenge if one appears, extract, close the tab.

    The tab is opened and closed inside this function, so it cannot leak — that is the whole reason
    this is a function and not a documented sequence of ops.

    Returns {text, title, url, challenge, b64?}.
    """
    port = ensure(port, on_start=on_start)
    with _lock:
        idx = call("newtab", port=port, url="about:blank").get("index", -1)
        final_url, out = None, {}
        try:
            call("goto", port=port, url=url, timeout=90.0)
            page = call("text", port=port)
            challenge = None
            if solve and is_challenge(page):
                if on_start:
                    on_start("clearing a Cloudflare challenge")
                r = call("solve", port=port, tries=tries, timeout=solve_timeout(tries))
                challenge = "cleared" if r.get("passed") else "NOT cleared"
                page = call("text", port=port)
            final_url = page.get("url")
            out = {"text": render(page, mode, max_chars), "title": page.get("title"),
                   "url": final_url, "challenge": challenge}
            if shot:
                out["b64"] = call("shot", port=port, timeout=90.0)["b64"]
            return out
        finally:
            _close_tab(idx, final_url, port)


def _close_tab(index, url=None, port=None):
    """Close the tab we opened, and VERIFY it went. Indices shift when a LOWER-indexed tab closes, so
    an exact url match is preferred with the index as fallback. Tab 0 is protected by the engine."""
    try:
        before = call("tabs", port=port, method="GET")
        target = None
        if url:
            for t in before.get("tabs", []):
                if t["index"] != 0 and t.get("url") == url:
                    target = t["index"]
                    break
        if target is None and index and index > 0:
            target = index
        if target is None:
            return {"closed": 0}
        r = call("closetab", port=port, index=target)
        if r.get("remaining", 0) >= before.get("count", 0):
            print(f"warning: leaked tab {target} (count still {r.get('remaining')})",
                  file=sys.stderr)
        return r
    except EngineError as e:
        print(f"warning: tab cleanup failed, leaked tab {index}: {e}", file=sys.stderr)
        return {"closed": 0}


def save_shot(path, port=None, on_start=None):
    b64 = op("shot", port=port, on_start=on_start, timeout=90.0)["b64"]
    with open(path, "wb") as f:
        f.write(base64.b64decode(b64))
    return path
