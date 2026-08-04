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
import html as htmlmod
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from html.parser import HTMLParser

# This process's identity inside a SHARED browser. Every tab this process opens is tagged with it,
# and every op it issues names the tab it means — so a second agent driving the same engine cannot
# move the page out from under us. One browser, many agents, no contention.
SESSION = f"pw-{os.getpid()}-{zlib.crc32(os.urandom(8)):x}"
_seq = itertools.count()


def _repo_of(path):
    """The git repo a path sits in, by name. Walking up for .git beats using the bare directory
    name: an agent started in a subdirectory should still report the project, not `src`."""
    d = os.path.abspath(path)
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return os.path.basename(d)
        parent = os.path.dirname(d)
        if parent == d:
            return os.path.basename(os.path.abspath(path))
        d = parent


def owner_label():
    """Who is driving, in a form a human can read off the tab strip.

    One shared browser plus several agents means the obvious question, looking at a window full of
    tabs, is "who opened THAT one?" — so every tab carries this. It is derived automatically because
    an identity you have to remember to pass is an identity that will be missing exactly when you
    need it; PLAYWRONG_AGENT overrides the guessed name when you want something specific.
    """
    agent = (os.environ.get("PLAYWRONG_AGENT") or "").strip()
    if not agent:
        argv0 = os.path.basename(sys.argv[0] or "")
        # argv[0] is "-c" for `python -c` and "" for a REPL — neither identifies anything.
        if argv0 and not argv0.startswith("-"):
            agent = argv0.removesuffix(".py")
        if agent == "mcp_server":
            agent = "mcp"               # what the caller IS, not the file that implements it
    if not agent:
        try:                            # the parent tells you more than "python" does
            with open(f"/proc/{os.getppid()}/comm") as f:
                agent = f.read().strip()
        except OSError:
            agent = "python"
    return f"{agent}@{_repo_of(os.getcwd())}:{os.getpid()}"


OWNER = owner_label()

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_dir():
    """Scratch/log directory. The checkout's tmp/ when that is genuinely ours, otherwise an XDG cache
    dir — an installed package must not write into site-packages (the old unconditional REPO/tmp did
    exactly that, and the engine log landed in site-packages/tmp/logs/)."""
    cand = os.path.join(REPO, "tmp")
    if "site-packages" not in cand and "dist-packages" not in cand:
        try:
            os.makedirs(cand, exist_ok=True)
            if os.access(cand, os.W_OK):
                return cand
        except OSError:
            pass
    d = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "playwrong")
    os.makedirs(d, exist_ok=True)
    return d


DATA = data_dir()
LOGDIR = os.path.join(DATA, "logs")
SERVER_LOG = os.path.join(LOGDIR, "playwrong-engine.log")


def default_port():
    return int(os.environ.get("PH_PORT", "8731"))


def profile_port(name):
    """A stable port per named profile, derived from the name.

    A Chrome profile is fixed when the browser launches, so one engine serves exactly one profile.
    Rather than making you track "which port did I start `work` on", the name picks the port: the
    same --profile always finds the same warm engine, and two profiles never collide on one browser.
    zlib.crc32 because Python's hash() is randomised per process and would pick a different port
    every run.
    """
    return 8740 + (zlib.crc32(name.encode()) % 50)


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


def spawn(port=None, profile=None):
    """Launch engine/server.py detached, output to a log file.

    No PYTHONPATH is set: server.py puts vendor/ on sys.path itself (and does it with
    sys.path.insert(0, ...), so the vendored nodriver wins over any site-packages copy). Callers
    that export PYTHONPATH=vendor are copying a step that has not been needed for a long time.
    """
    port = port or default_port()
    os.makedirs(LOGDIR, exist_ok=True)
    env = {**os.environ, "PH_PORT": str(port)}
    if profile:
        env["PH_PROFILE"] = profile      # engine/server.py turns the NAME into a user-data-dir
    subprocess.Popen([sys.executable, os.path.join(REPO, "engine", "server.py")],
                     env=env,
                     stdout=open(SERVER_LOG, "a"), stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, start_new_session=True)


def _confirm_absent(port):
    """Decide 'there is really no engine here' CAREFULLY.

    A single 2s probe is not evidence of absence. Under load — say a dozen agents starting at once —
    a live engine can miss that deadline, every caller concludes it must start one, and each new
    browser loads the machine further so the next probe is even slower. That feedback loop is not
    theoretical: one 12-process run spawned 1204 servers and 255 Chrome instances before it was
    stopped. So: probe again, patiently, before spawning anything.
    """
    return not reachable(port, timeout=8.0)


def ensure(port=None, want_browser=True, on_start=None, profile=None):
    """Guarantee an engine on `port`, and by default a launched Chrome behind it. Idempotent and
    cheap when everything is already up (one /status round-trip).

    Concurrency-safe: the check-and-spawn is held under a file lock, so when N processes start at
    once exactly ONE launches the engine and the rest wait for it. Without the lock they all spawn,
    which is how you end up with a browser per caller instead of a shared one.

    Two waits for two different things:
      1. the HTTP process binding the port — a Python import plus a bind, no browser, so 30s is
         generous even on a loaded machine; polled at 0.25s to keep first-call latency low.
      2. Chrome launching — done by POSTing /start, which BLOCKS until the browser is up, so there is
         nothing to poll. Cold start is the slow part, hence 120s.
    """
    port = port or (profile_port(profile) if profile else default_port())
    if reachable(port):                       # fast path: already up, no lock needed
        if want_browser:
            _wake(port, on_start)
        return port

    with _spawn_lock(port):
        # Re-check INSIDE the lock: while we queued, the winner probably started it already.
        if _confirm_absent(port):
            if on_start:
                on_start("starting the capture engine")
            spawn(port, profile=profile)
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                if reachable(port, timeout=1.0):
                    break
                time.sleep(0.25)
            else:
                raise EngineError(
                    f"engine did not bind 127.0.0.1:{port} within 30s. Check {SERVER_LOG}; the "
                    f"usual cause is a missing dependency — run: "
                    f"{sys.executable} {REPO}/scripts/doctor.py")
    if want_browser:
        _wake(port, on_start)
    return port


def _wake(port, on_start=None):
    """Make sure Chrome itself is up. /start blocks until it is, so there is nothing to poll."""
    if call(port=port, op="status", method="GET").get("alive"):
        return
    if on_start:
        on_start("launching Chrome")
    call(port=port, op="start", timeout=120.0)


def _spawn_lock(port):
    """An exclusive, cross-process lock for 'am I the one who starts the engine'.

    flock is advisory and released automatically if the holder dies, so a crashed starter cannot
    wedge everyone else. Where flock does not exist (Windows) this degrades to no locking, which is
    the behaviour everything had before — correct for a single caller, racy for many.
    """
    os.makedirs(DATA, exist_ok=True)
    path = os.path.join(DATA, f"spawn-{port}.lock")
    try:
        import fcntl
    except ImportError:
        import contextlib
        return contextlib.nullcontext()

    class _Lock:
        def __enter__(self):
            self.f = open(path, "w")
            fcntl.flock(self.f, fcntl.LOCK_EX)
            return self
        def __exit__(self, *a):
            try:
                fcntl.flock(self.f, fcntl.LOCK_UN)
                self.f.close()
            except Exception:
                pass
    return _Lock()


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
    except ValueError as e:
        raise EngineError(f"engine op {op!r} returned non-JSON ({len(raw)} bytes)") from e
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


def _settled_text(port, page=None, tab=None):
    """Read the page, waiting for it to have actually rendered.

    /goto settles for a fixed 2s, which is a guess, and on a JS-rendered page it is sometimes short:
    an MCP fetch of a heavy reviews page returned an EMPTY body once and 44k chars on the immediate
    retry. So rather than trusting a constant, re-read until the document has visible text. Poll at
    250ms for up to 5s beyond goto's own wait; past that, empty is the honest answer (a genuinely
    blank page, or a hard block) and not something more waiting fixes.
    """
    page = page if page is not None else call("text", port=port, tab=tab)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if html_to_text(page.get("html")).strip():
            return page
        time.sleep(0.25)
        page = call("text", port=port, tab=tab)
    return page


def capture(url, mode="text", solve=True, max_chars=40000, tries=20, port=None, shot=False,
            on_start=None, profile=None):
    """Everything needed to get one page, in one call: start the engine and Chrome if they are down,
    open OUR OWN tab, navigate, clear a Cloudflare challenge if one appears, extract, close the tab.

    The tab is opened and closed inside this function, so it cannot leak — that is the whole reason
    this is a function and not a documented sequence of ops.

    Returns {text, title, url, challenge, b64?}.
    """
    port = ensure(port, on_start=on_start, profile=profile)
    with _lock:
        tag = f"{SESSION}-{next(_seq)}"
        idx = call("newtab", port=port, url="about:blank", tag=tag, owner=OWNER).get("index", -1)
        final_url, out = None, {}
        try:
            landed = call("goto", port=port, url=url, tab=tag, timeout=90.0)
            # Chrome's first-run/profile page can occupy a brand-new tab, so the navigation lands
            # somewhere internal and we would return THAT page's text as if it were the url asked
            # for — a silently wrong answer, seen once on the very first launch of a fresh profile.
            # Only an internal url counts as wrong here; a cross-host redirect is legitimate.
            if str(landed.get("url", "")).startswith(("chrome://", "about:")):
                call("goto", port=port, url=url, tab=tag, timeout=90.0)
            page = _settled_text(port, tab=tag)
            challenge = None
            if solve and is_challenge(page):
                if on_start:
                    on_start("clearing a Cloudflare challenge")
                r = call("solve", port=port, tries=tries, tab=tag, timeout=solve_timeout(tries))
                challenge = "cleared" if r.get("passed") else "NOT cleared"
                page = call("text", port=port, tab=tag)
            page = _settled_text(port, page, tab=tag)  # a solve navigates again; re-settle after it
            final_url = page.get("url")
            out = {"text": render(page, mode, max_chars), "title": page.get("title"),
                   "url": final_url, "challenge": challenge}
            if shot:
                out["b64"] = call("shot", port=port, tab=tag, timeout=90.0)["b64"]
            return out
        finally:
            _close_tab(idx, final_url, port, tag=tag)


def _close_tab(index, url=None, port=None, tag=None):
    """Close the tab we opened, and VERIFY it went. Indices shift when a LOWER-indexed tab closes, so
    an exact url match is preferred with the index as fallback. Tab 0 is protected by the engine."""
    try:
        if tag:
            # Closing by tag is exact and race-free; index/url are the fallbacks for tabs opened
            # without one.
            r = call("closetab", port=port, tag=tag)
            if r.get("closed"):
                return r
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


# ── search ──────────────────────────────────────────────────────────────────────────────────────

DDG_LITE = "https://lite.duckduckgo.com/lite/?q={}"
# DDG wraps every result in a redirector: //duckduckgo.com/l/?uddg=<urlencoded real url>&rut=...
_RESULT = re.compile(r'<a[^>]+href="([^"]+)"[^>]*class="result-link"[^>]*>(.*?)</a>', re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")
# DDG's anomaly/CAPTCHA page, which a real browser also gets under rapid repeat querying.
DDG_BLOCKED = re.compile(r"bots use DuckDuckGo too|squares containing|anomaly", re.I)


def parse_ddg(html):
    """Pull {title, url} out of a DuckDuckGo lite results page, unwrapping the redirector so callers
    get the real destination rather than a duckduckgo.com/l/ link.

    ADS carry the same `class="result-link"` as organic results, so matching on the class alone put a
    "Fix Windows Driver Update" ad at position 1 for a search about `nodriver`. They are told apart
    by their redirector: organic results go through `/l/?uddg=<real url>`, ads through `y.js?ad_domain=…`.
    Requiring the `uddg` parameter drops every ad, and anything still pointing at duckduckgo.com is
    the help/settings furniture rather than a result.
    """
    out, seen = [], set()
    for href, title in _RESULT.findall(html or ""):
        href = htmlmod.unescape(href)
        real = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [None])[0]
        if not real:
            continue                       # an ad (y.js) or some other non-result link
        host = urllib.parse.urlparse(real).netloc.lower()
        if host.endswith("duckduckgo.com") or real in seen:
            continue
        seen.add(real)
        title = " ".join(htmlmod.unescape(_TAGS.sub(" ", title)).split())
        out.append({"title": title, "url": real})
    return out


def search(query, max_results=20, port=None, on_start=None, profile=None):
    """DuckDuckGo results, through the real browser.

    DDG now answers curl with an image CAPTCHA ("select all squares containing a duck") on both the
    lite and html endpoints — HTTP 202 and a challenge page instead of results, which silently breaks
    the usual `curl lite.duckduckgo.com/lite/?q=` recipe. A real headed Chrome is not challenged at
    all: nothing is being solved or bypassed here, the browser simply looks like a browser.
    """
    page = capture(DDG_LITE.format(urllib.parse.quote_plus(query)), mode="html", max_chars=0,
                   port=port, on_start=on_start, profile=profile)
    hits = parse_ddg(page["text"])[:max_results]
    if not hits and DDG_BLOCKED.search(page["text"] or ""):
        # Distinguish "they challenged us" from "their markup changed". Even a real browser gets the
        # anomaly page under rapid repeat querying, and it is transient — blaming the parser sends
        # you off debugging code that is fine.
        raise EngineError("DuckDuckGo served an anti-bot challenge instead of results (usually rate "
                          "limiting from rapid repeat queries) — retry in a minute")
    return hits


# ── downloads: PDFs and other files behind a bot wall ───────────────────────────────────────────

def _origin(url):
    p = urllib.parse.urlparse(url)
    return f"{p.scheme}://{p.netloc}/"


def _cookie_applies(domain, host):
    """Cookie-domain match, the ordinary way: a leading dot means "and all subdomains"."""
    if not domain:
        return False
    d = domain.lstrip(".")
    return host == d or host.endswith("." + d)


def session_headers(url, port=None, on_start=None, solve=True, tries=20, profile=None):
    """Clear the wall for this url's origin in the real browser, then hand back the headers that let
    a plain HTTP client through as that same cleared session: Cookie (incl. cf_clearance) + the
    browser's exact User-Agent.

    This is the missing half of "don't open PDFs in the browser". Curl alone fails on a bot-walled
    file, and Chrome's built-in PDF viewer cannot be driven reliably — so neither tool works on its
    own. Clearing in the browser and downloading with the cleared session works, and is what
    download() does.
    """
    port = ensure(port, on_start=on_start, profile=profile)
    with _lock:
        tag = f"{SESSION}-{next(_seq)}"
        idx = call("newtab", port=port, url="about:blank", tag=tag, owner=OWNER).get("index", -1)
        final_url = None
        try:
            # Navigate to the ORIGIN, not the file: a challenge is served per-origin, and going
            # straight at a PDF drops us in Chrome's PDF viewer where evaluate() is unreliable.
            call("goto", port=port, url=_origin(url), tab=tag, timeout=90.0)
            page = call("text", port=port, tab=tag)
            if solve and is_challenge(page):
                if on_start:
                    on_start("clearing a Cloudflare challenge")
                call("solve", port=port, tries=tries, tab=tag, timeout=solve_timeout(tries))
            final_url = call("text", port=port, tab=tag).get("url")
            ua = call("js", port=port, expr="navigator.userAgent", tab=tag).get("result") or ""
            cookies = call("cookies", port=port).get("cookies", [])
        finally:
            _close_tab(idx, final_url, port, tag=tag)
    host = urllib.parse.urlparse(url).netloc.split(":")[0]
    jar = "; ".join(f"{c['name']}={c['value']}" for c in cookies
                    if _cookie_applies(c.get("domain"), host))
    h = {"Referer": _origin(url)}
    if ua:
        h["User-Agent"] = ua
    if jar:
        h["Cookie"] = jar
    return h


def download(url, path=None, port=None, on_start=None, solve=True, tries=20, profile=None):
    """Fetch a file (PDF, zip, image, …) from behind a bot wall and write it to disk.

    Returns {path, bytes, content_type, final_url}. `path` defaults to tmp/ + the url's basename;
    give one to keep the file somewhere real (`sources/foo.pdf`) instead of scratch.

    `final_url` is the url after redirects. A manifest wants that rather than the url you asked
    for — they differ exactly when a download went somewhere you did not expect.
    """
    headers = session_headers(url, port=port, on_start=on_start, solve=solve, tries=tries,
                              profile=profile)
    if on_start:
        on_start(f"downloading with the cleared session ({len(headers.get('Cookie',''))} B of cookies)")
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:   # 3 min: documents can be tens of MB
            data = r.read()
            ctype = r.headers.get("Content-Type", "")
            final = r.geturl()
    except urllib.error.HTTPError as e:
        raise EngineError(f"download failed: HTTP {e.code} {e.reason} for {url}") from e
    except (urllib.error.URLError, OSError) as e:
        raise EngineError(f"download failed: {e}") from e
    if path is None:
        name = os.path.basename(urllib.parse.urlparse(url).path) or "download"
        path = os.path.join(DATA, name)
    else:
        path = os.path.abspath(os.path.expanduser(path))
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)   # sources/ may not exist yet; a missing dir is not
    with open(path, "wb") as f:                  # a reason to throw away a download that worked
        f.write(data)
    return {"path": path, "bytes": len(data), "content_type": ctype, "final_url": final}


def pdf_text(path):
    """Extract text with pdftotext if it's installed. Returns None when it isn't, so callers can say
    'saved, but install poppler-utils to read it' rather than failing."""
    exe = shutil.which("pdftotext")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "-layout", path, "-"], capture_output=True, text=True, timeout=120)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def pdf_pages(path):
    """Page count via pdfinfo, or None when poppler-utils isn't installed.

    Worth the extra process: a truncated document is the failure that doesn't announce itself. A
    45-page datasheet that arrives as 10 pages reads as complete — the missing section looks like a
    feature the part lacks rather than a document you only half have. Comparing this against what
    the document should have is the check that catches it.
    """
    exe = shutil.which("pdfinfo")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, path], capture_output=True, text=True, timeout=30)
        for line in r.stdout.splitlines():
            if line.startswith("Pages:"):
                return int(line.split(":", 1)[1].strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def pdf(url, path=None, port=None, on_start=None, solve=True, tries=20, profile=None):
    """A PDF, even behind a bot wall: clear the challenge, download through the cleared session,
    keep the file, and extract the text.

    Returns {path, bytes, pages, text, content_type, final_url}. Pass `path` to keep the file
    somewhere permanent — the download happens either way, so discarding it is pure loss.
    """
    out = download(url, path=path, port=port, on_start=on_start, solve=solve, tries=tries,
                   profile=profile)
    head = open(out["path"], "rb").read(5)
    if head != b"%PDF-":
        # A challenge page saved as .pdf is the classic silent failure — say so instead of handing
        # back bytes that will not open.
        out["text"] = None
        out["warning"] = (f"not a PDF (starts with {head!r}) — the server likely returned an HTML "
                          f"block page. Try fetching the containing page first so the session is "
                          f"fully cleared.")
        out["pages"] = None
        return out
    out["pages"] = pdf_pages(out["path"])
    out["text"] = pdf_text(out["path"])
    return out


# ── prefetch: overlap the waiting ────────────────────────────────────────────────────────────────

def prefetch(urls, concurrency=8, solve=True, tries=20, timeout=30, port=None, on_start=None,
             profile=None):
    """Start loading many urls at once and return IMMEDIATELY with a job id.

    Fetching pages one at a time serialises their load times, and a page load is almost entirely
    waiting. This hands the whole list to the engine, which loads up to `concurrency` of them in
    parallel tabs and holds each result until you collect it — so you fire the batch, go do
    something else, and read the pages as they land instead of blocking on the slowest one.
    """
    port = ensure(port, on_start=on_start, profile=profile)
    return call("prefetch", port=port, urls=list(urls), concurrency=concurrency,
                solve=solve, tries=tries, timeout=timeout, owner=OWNER)["job"]


def poll(job, port=None):
    """Counts only — how many are ready, loading, still queued, failed. Cheap to call in a loop."""
    return call("jobs", port=port, job=job)


def collect(job, mode="text", max_chars=40000, port=None, wait=0.0, want=1):
    """Take whatever is READY (rendered like capture() does) and forget it engine-side.

    wait: seconds to keep waiting for at least `want` results before giving up and returning what
    there is. 0 = take what is ready this instant and return. Polls at 0.5s, which is far below a
    page load and cheap enough not to matter.
    """
    deadline = time.monotonic() + max(0.0, wait)
    out = []
    while True:
        r = call("collect", port=port, job=job)
        for item in r.get("results", []):
            if item.get("status") == "error":
                out.append({"url": item.get("requested"), "error": item.get("error"), "text": None})
            else:
                out.append({"url": item.get("url"), "title": item.get("title"),
                            "challenge": item.get("challenge"),
                            "text": render(item, mode, max_chars)})
        if (out and len(out) >= want) or r.get("done") or time.monotonic() >= deadline:
            return {"results": out, "remaining": r.get("remaining", 0), "done": bool(r.get("done"))}
        time.sleep(0.5)


def fetch_many(urls, concurrency=8, mode="text", max_chars=40000, solve=True, tries=20,
               port=None, on_start=None, profile=None, timeout=300.0, per_url=30):
    """prefetch + drain: every page, in parallel, yielded as each one finishes.

    A generator so a caller can start using the first page while the rest are still loading.
    `timeout` is a whole-batch ceiling — without one a single wedged page would hang the generator
    forever; 300s is generous for 8 concurrent loads and still bounded.
    """
    job = prefetch(urls, concurrency=concurrency, solve=solve, tries=tries, timeout=per_url,
                   port=port, on_start=on_start, profile=profile)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = collect(job, mode=mode, max_chars=max_chars, port=port, wait=2.0)
        yield from got["results"]
        if got.get("done"):
            break


def save_shot(path, port=None, on_start=None):
    b64 = op("shot", port=port, on_start=on_start, timeout=90.0)["b64"]
    with open(path, "wb") as f:
        f.write(base64.b64decode(b64))
    return path
