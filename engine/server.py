"""server.py — the capture engine: ONE headed Chrome, driven over HTTP, shared by every caller.

Backed by nodriver, not Playwright: Playwright is the Turnstile tell (Cloudflare serves it a dead
challenge that never resolves), while nodriver drives Chrome over raw CDP and passes. One browser
does everything — clear Turnstile, capture pages, feed the viz screenshots — so there is no second
browser and no orphan windows.

Control port (POST JSON): goto{url} solve{tries} text shot js{expr} cookies clearcookies
                          newtab closetab closeextra tabs cdp move click key shutdown
GET: /status /tabs /viz /frame /markers ; POST /setmarkers

You do not need to run this by hand — engine/connect.py starts it on demand, and it puts vendor/ on
sys.path itself, so no PYTHONPATH is required. To run it in the foreground anyway:

    python engine/server.py            # PH_PORT (default 8731); log in tmp/nd-server.log
"""
import asyncio
import base64
import json
import os
import re
import sys
import threading
import traceback
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _use_vendored_nodriver():
    """Put the PATCHED nodriver first on sys.path, in a checkout AND in an installed package.

    Upstream nodriver 0.50.x has a non-UTF-8 byte in cdp/network.py that raises SyntaxError on
    import under CPython 3.14+. The fixed copy lives at `vendor/nodriver` in a source checkout and
    ships to `crawl/_vendor/nodriver` in the wheel. Looking only at `../vendor` (as this did) meant
    an INSTALLED playwrong found no vendored copy, imported the broken PyPI one, and died on its
    first run with exactly that SyntaxError — the landmine the README warns about, in the one place
    a user cannot fix it themselves."""
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "..", "vendor"),                  # source checkout
                 os.path.join(os.path.dirname(here), "crawl", "_vendor")):   # installed wheel
        if os.path.isdir(os.path.join(cand, "nodriver")):
            sys.path.insert(0, cand)
            return


_use_vendored_nodriver()
import nodriver as uc  # noqa: E402
from nodriver import cdp  # noqa: E402

PORT = int(os.environ.get("PH_PORT", "8731"))


def profile_dir():
    """Where Chrome keeps its user-data-dir, if anywhere.

    PH_PROFILE_DIR — an explicit path, for full control.
    PH_PROFILE     — just a NAME ("work", "logged-in"), resolved to an XDG data dir and created for
                     you. Persistent profiles are what you want for a site you log into: the session
                     survives an engine restart. Naming one beats inventing a path, which is why
                     this exists.
    neither        — ephemeral: a throwaway profile per launch. The default, because a persistent
                     profile silently accumulates real browsing state on disk and that should be an
                     opt-in.
    """
    d = os.environ.get("PH_PROFILE_DIR")
    if d:
        return os.path.expanduser(d)
    name = os.environ.get("PH_PROFILE")
    if not name:
        return None
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    d = os.path.join(base, "playwrong", "profiles", name)
    os.makedirs(d, exist_ok=True)
    return d


PROFILE_DIR = profile_dir()


def data_dir():
    """Scratch/log directory: the checkout's tmp/ when that is really ours to write to, otherwise an
    XDG cache dir. An INSTALLED package must never write into site-packages — which is exactly what
    the old unconditional `../tmp` did, creating site-packages/tmp/logs/."""
    cand = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "tmp"))
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


TMP = data_dir()
LOG = os.path.join(TMP, "nd-server.log")
CHALLENGE = ("just a moment", "verify you are human", "cf-chl", "challenge-platform")

# Ownership shown IN the tab title, so a human looking at a shared browser can tell at a glance which
# agent and which repo opened a given tab. The brackets are the rare "white square" pair specifically
# so the marker cannot collide with anything a real page would put in its own title, which is what
# makes stripping it again exact rather than a guess.
OWNER_L, OWNER_R = "\u27e6", "\u27e7"
OWNER_TITLE_RE = re.compile(rf"^{OWNER_L}[^{OWNER_R}]*{OWNER_R}\s*")


def strip_owner(title):
    """Titles reported by the API are the page's own. The label is decoration for the human watching
    the window, and must never leak into a capture, a result, or a caller's data."""
    return OWNER_TITLE_RE.sub("", title or "")

# Serialise the caller's expression IN THE PAGE and hand back a JSON string.
#
# Reading nodriver's return value directly does not work: it yields a RemoteObject for anything that
# isn't a primitive (so `({a:1})` came back as "RemoteObject(type_='object', ...)"), a raw
# ExceptionDetails for a thrown error, and a tuple in other cases — none of them JSON-serialisable,
# which killed the HTTP response mid-flight. Stringifying in the page sidesteps every one of those
# shapes: objects, arrays, promises, undefined and exceptions all arrive as one plain string.
#
# `await (…)` wraps both sync and async expressions, so no special case is needed for either.
JS_WRAP = ("(async () => { try { const v = await (%s);"
           " return JSON.stringify(v === undefined ? null : v); }"
           " catch (e) { return JSON.stringify("
           "{__playwrong_error: String((e && e.stack) || e)}); } })()")

def log(a, **k):
    ts = datetime.now(UTC).isoformat(timespec="milliseconds")
    try:
        open(LOG, "a").write(f"{ts} {a} " + " ".join(f"{x}={y}" for x,y in k.items()) + "\n")
    except Exception:
        pass

def heal_profile(profile_dir):
    """Clear a stale SingletonLock so a persistent profile can be relaunched.

    Chrome writes SingletonLock/SingletonCookie/SingletonSocket into a user-data-dir and removes them
    on a CLEAN exit. After a crash, kill -9 or a host reboot they survive, pointing at a long-dead
    PID — and the next launch against that profile then HANGS INDEFINITELY: /status never reports
    alive, and nothing appears in the log past server_start. It is not a crash, the launch is stuck
    on the lock, which makes it look like the browser is simply broken.

    The documented fix was "rm the three Singleton* files yourself". Doing it here instead: the lock
    is only meaningful if its owning process is alive, so we check, and remove it only when it is
    not. A live lock (a second engine genuinely using this profile) is left alone.
    """
    if not profile_dir or not os.path.isdir(profile_dir):
        return
    lock = os.path.join(profile_dir, "SingletonLock")
    if not os.path.lexists(lock):
        return
    # ONLY SingletonLock encodes a pid, as a "<hostname>-<pid>" symlink target. SingletonCookie's
    # target is a large random number and SingletonSocket's is a socket path — parsing either as a
    # pid yields a value os.kill() rejects outright (OverflowError: Python int too large to convert
    # to C int), which is why liveness is decided from the lock alone.
    pid = None
    try:
        pid = int(os.readlink(lock).rsplit("-", 1)[-1])
    except (OSError, ValueError):
        pass
    if pid and pid > 0:
        try:
            os.kill(pid, 0)              # signal 0 = existence check only, never touches the process
            log("profile_lock_live", pid=pid)
            return                        # a real browser is using this profile; leave it alone
        except PermissionError:
            log("profile_lock_live", pid=pid)      # exists, just owned by another user
            return
        except (ProcessLookupError, OverflowError, OSError):
            pass                          # owner gone (or an unparseable pid) -> treat as stale
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        p = os.path.join(profile_dir, name)
        if not os.path.lexists(p):
            continue
        try:
            os.unlink(p)
            log("profile_lock_cleared", file=name, pid=pid)
        except OSError as e:
            log("profile_lock_err", file=name, e=str(e)[:60])


class ND:
    """nodriver browser on its own asyncio loop in a thread; sync-facing .do() for the HTTP handler."""
    def __init__(self):
        self.loop = asyncio.new_event_loop(); self.browser=self.tab=None
        self.tags = {}          # agent tag -> target_id. See _tab(): tags, never indices.
        self.jobs = {}          # prefetch job id -> {slots, total, delivered}
        self._launch = asyncio.Lock()   # serialises browser launch; see _ensure()
        self.owners = {}        # target_id -> "agent@repo:pid", shown in the tab title and /tabs
        threading.Thread(target=lambda:(asyncio.set_event_loop(self.loop),self.loop.run_forever()),
                         daemon=True).start()
    def run(self, coro): return asyncio.run_coroutine_threadsafe(coro, self.loop).result()
    async def _ensure(self):
        """Launch the browser once, however many callers arrive at once.

        The bare `if self.tab: return` was a double-checked lock with no lock. uc.start() awaits, so
        twelve concurrent first-ops ALL saw self.tab as None and ALL launched a browser: one engine,
        twelve Chromes, eleven of them untracked and therefore unclosable. Measured exactly that —
        nd_started fired 12 times for a single server. The lock makes the launch happen once; the
        re-check inside it is what the fast path outside cannot guarantee."""
        if self.tab: return
        async with self._launch:
            if self.tab: return         # someone else launched while we waited for the lock
            heal_profile(PROFILE_DIR)   # a stale lock would hang the launch below forever
            self.browser = await uc.start(headless=False, user_data_dir=PROFILE_DIR)
            self.tab = await self.browser.get("about:blank")
            self._publish_cdp()
            log("nd_started", cdp=f"{self.browser.config.host}:{self.browser.config.port}")
    async def _start(self):
        """Explicitly trigger the (otherwise lazy) browser launch and block until it's up - the
        primitive /status can't give you on its own. Chrome only launches on the FIRST real op
        (goto/newtab/etc via _ensure()); /status's "alive" reports whether that's happened yet, but
        polling /status alone never makes it happen - a caller that only checks status in a loop
        waits forever. Call this once after confirming the HTTP server itself is reachable, then
        /status will read alive:true."""
        await self._ensure()
        return {"started": True}
    def _publish_cdp(self):
        """Write the shared browser's CDP endpoint to a marker so OTHER processes can ATTACH to this
        same browser (nodriver.start(host,port) connects to an existing browser) and shard by opening
        their own tabs — the parallel-crawl use case. One browser, many attached drivers, tabs = shards."""
        try:
            host=self.browser.config.host; port=self.browser.config.port
            self._cdp={"host":host,"port":port,"http":f"http://{host}:{port}"}
            with open(os.path.join(TMP,"playwrong-cdp.json"),"w") as f:
                json.dump(self._cdp,f)
        except Exception as e:
            self._cdp={"error":str(e)[:120]}
    async def _cdp_info(self):
        await self._ensure()
        return getattr(self,"_cdp",{"error":"not published"})
    async def _tab(self, ref=None):
        """Resolve a tab reference to a live Tab.

        THIS is what makes one browser safe for many agents. Every op used to act on self.tab — the
        one globally-active tab — so two callers driving the engine at once silently read each
        other's pages. Now each op can name the tab it means.

        ref=None      the active tab (unchanged single-caller behaviour)
        ref="<tag>"   a tag registered by newtab{tag} — an agent's own handle
        ref=<int>     a positional index (convenient, but see the warning below)

        Tags resolve to TARGET IDs, never to indices. An index shifts whenever a lower-numbered tab
        closes, so an index handed out earlier can later point at somebody else's tab — the same
        class of bug as the active-tab race, just slower to show up.
        """
        await self._ensure()
        if ref is None: return self.tab
        await self._refresh()
        want = self.tags.get(ref, ref) if isinstance(ref, str) else None
        for i, t in enumerate(self.browser.tabs):
            if isinstance(ref, int) and i == ref: return t
            if want and getattr(getattr(t, "target", None), "target_id", None) == want: return t
        raise KeyError(f"no such tab: {ref!r} (tags: {sorted(self.tags)})")
    async def _goto(self, url, tab=None):
        t = await self._tab(tab)
        await t.get(url); await t.sleep(2)
        await self._label(t)
        # location.href, not the url we asked for: redirects (and challenge interstitials) mean the
        # two differ, and a caller that stores the requested url records a page it never got.
        return {"title": strip_owner(await t.evaluate("document.title")),
                "url": await self._href(t), "requested": url}
    async def _label(self, t):
        """Prefix this tab's title with its owner, best effort.

        Best effort on purpose: a single-page app rewrites document.title whenever it likes, so the
        label is a convenience for whoever is watching the screen, never something the code relies
        on. Failing to set it must never fail the navigation that just succeeded."""
        owner = self.owners.get(getattr(getattr(t, "target", None), "target_id", None))
        if not owner:
            return
        try:
            await t.evaluate(
                "(()=>{const m=%r,o=%r;const t=document.title.replace(/^\u27e6[^\u27e7]*\u27e7\s*/,'');"
                "document.title=m+o+'\u27e7 '+t;return 1})()" % (OWNER_L, owner))
        except Exception:
            pass

    async def _href(self, t=None):
        """Authoritative current url. nodriver's Tab has no .url attribute (only .target.url, which
        lags), so ask the page."""
        try: return await (t or self.tab).evaluate("location.href")
        except Exception: return ""
    def _is_chal(self, t, h):
        t=(t or "").lower(); h=(h or "").lower()
        return any(k in t for k in CHALLENGE) or "verify you are human" in h
    async def _solve(self, tries=20, tab=None):
        tb = await self._tab(tab)
        for i in range(tries):
            t=await tb.evaluate("document.title"); h=await tb.get_content()
            if not self._is_chal(t,h): log("solved",i=i); return {"passed":True,"iter":i}
            try:
                el=await tb.find("verify you are human", best_match=True, timeout=3)
                if el: await el.mouse_click(); log("clicked",i=i); await tb.sleep(4)
            except Exception as e: log("click_err",e=str(e)[:50])
            await tb.sleep(1)
        return {"passed":False,"iter":tries}
    async def _text(self, tab=None):
        t = await self._tab(tab)
        return {"title":strip_owner(await t.evaluate("document.title")),
                "html":await t.get_content(),"url": await self._href(t)}
    async def _frame(self, tab=None):
        t = await self._tab(tab)
        # Per-tab filename: two agents screenshotting at once would otherwise overwrite each other's
        # file between the save and the read.
        p=os.path.join(TMP,f"nd-frame-{getattr(getattr(t,'target',None),'target_id','x')}.png")
        await t.save_screenshot(p)
        try: return open(p,"rb").read()
        finally:
            try: os.remove(p)
            except OSError: pass
    async def _shot(self, tab=None):
        return {"b64": base64.b64encode(await self._frame(tab)).decode()}
    async def _clearcookies(self):
        await self._ensure(); await self.browser.cookies.clear(); return {"cleared":True}
    # --- added verbs (CDP input + tab ops) so the full client surface works on the nodriver engine ---
    async def _move(self, x, y, tab=None):
        t = await self._tab(tab)
        await t.send(cdp.input_.dispatch_mouse_event(type_="mouseMoved", x=float(x), y=float(y)))
        return {"ok":1,"x":x,"y":y}
    async def _click(self, x, y, tab=None):
        t = await self._tab(tab)
        for ty in ("mousePressed","mouseReleased"):
            await t.send(cdp.input_.dispatch_mouse_event(
                type_=ty, x=float(x), y=float(y), button=cdp.input_.MouseButton.LEFT, click_count=1))
        return {"ok":1,"x":x,"y":y}
    async def _key(self, key, tab=None):
        tb = await self._tab(tab)
        # named keys (Enter/Tab/...) carry a code; printable chars go as text
        named = {"Enter":13,"Tab":9,"Escape":27,"Backspace":8,"ArrowDown":40,"ArrowUp":38,
                  "ArrowLeft":37,"ArrowRight":39,"PageUp":33,"PageDown":34,"Home":36,"End":35,
                  "Space":32,"Delete":46}
        if key in named:
            for ty in ("keyDown","keyUp"):
                await tb.send(cdp.input_.dispatch_key_event(
                    type_=ty, key=key, windows_virtual_key_code=named[key]))
        else:
            await tb.send(cdp.input_.dispatch_key_event(type_="char", text=key))
        return {"ok":1,"key":key}
    async def _newtab(self, url="about:blank", tag=None, owner=None):
        """Open a tab and, optionally, TAG it as yours.

        A tag is how one agent keeps its work separate from another's inside a single shared
        browser: pass it back as `tab` on later ops and they act on your page regardless of what
        anyone else does to the active tab."""
        await self._ensure()
        t = await self.browser.get(url, new_tab=True)
        self.tab = t
        await self._refresh()          # the cache may not list the tab we just opened yet
        tid = getattr(getattr(t, "target", None), "target_id", None)
        if tag:
            self.tags[tag] = tid
        if owner:
            self.owners[tid] = owner
        return {"ok":1,"url":url,"index":self._tab_index(t),"tag":tag,"owner":owner,
                "target_id":str(tid or "")}
    # --- tab management: playwrong is ONE shared long-running browser that many agents SHARD by opening
    # their own tabs. Agents MUST track and CLOSE their tabs when done (else tabs/renderers leak — a
    # single-tab crawler that opened 8 tabs/run and never closed them left 22 orphan renderers). These
    # verbs let an agent enumerate the live tabs and close the ones it owns WITHOUT killing the server. ---
    def _tab_index(self, tab):
        try:
            for i, t in enumerate(self.browser.tabs):
                if t is tab: return i
        except Exception: pass
        return -1
    async def _refresh(self):
        """browser.tabs is a CACHED list (Browser._targets); nothing refreshes it on its own within a
        request. Without this, a closed tab keeps appearing and urls come back blank — which made tab
        hygiene silently unverifiable (closetab said closed:1, remaining:2, and the caller couldn't
        tell whether cleanup worked). Call before reading, and after closing."""
        try: await self.browser.update_targets()
        except Exception as e: log("refresh_err", e=str(e)[:60])
    async def _tabs(self):
        """List every open tab: index, url, title, and whether it's the server's 'active' tab. Agents
        use this to track what they opened and find tabs to close."""
        await self._ensure(); await self._refresh()
        by_id = {v: k for k, v in self.tags.items()}
        out=[]
        for i, t in enumerate(self.browser.tabs):
            # url/title come off the TargetInfo, not an evaluate(): it works for BACKGROUND tabs too
            # (evaluate only ever worked on the active one) and costs no round-trip per tab.
            ti = getattr(t, "target", None)
            tid = getattr(ti, "target_id", None)
            out.append({"index":i,"url":getattr(ti,"url","") or "",
                        "title":strip_owner(getattr(ti,"title","") or ""),
                        "active":(t is self.tab),"tag":by_id.get(tid),
                        "owner":self.owners.get(tid),"target_id":str(tid or "")})
        return {"tabs":out,"count":len(out)}
    async def _await_closed(self, ids):
        """Wait until Chrome has actually destroyed the targets we asked it to close.

        close_target is acknowledged immediately but the target is torn down asynchronously, so a
        refresh taken right after the call still lists the tab — which made "remaining" report a
        phantom leak on a tab that closed perfectly well. Observed teardown is single-digit ms; poll
        at 50ms and give up after 2s, at which point the tab really is stuck and the honest answer is
        to report it still open."""
        if not ids: return
        deadline = asyncio.get_event_loop().time() + 2.0
        while asyncio.get_event_loop().time() < deadline:
            await self._refresh()
            live = {getattr(getattr(t,"target",None),"target_id",None) for t in self.browser.tabs}
            if not (ids & live): return
            await asyncio.sleep(0.05)
        log("close_timeout", ids=",".join(str(i) for i in ids))
    async def _closetab(self, index=None, url=None, keep_first=True, tag=None):
        """Close a tab by index OR by url-substring match. keep_first protects tab 0 (the server's base
        tab). Never closes the last remaining tab. Returns how many were closed. This is how agents
        clean up their shard — NOT by killing the server."""
        await self._ensure(); await self._refresh()
        tabs=list(self.browser.tabs)
        if len(tabs)<=1: return {"closed":0,"reason":"only one tab; refusing to close the last"}
        # By TAG first: under concurrency an index is stale the moment another caller closes a
        # lower-numbered tab, so index-based cleanup starts closing other agents' tabs (or missing
        # its own and reporting a leak). A tag resolves to a target id, which never shifts.
        want_id = self.tags.get(tag) if tag else None
        targets=[]
        for i, t in enumerate(tabs):
            if keep_first and i==0: continue
            if want_id is not None:
                if getattr(getattr(t,"target",None),"target_id",None) == want_id:
                    targets.append((i,t))
                continue
            # t.url does not exist on a nodriver Tab, so the documented url-substring form matched
            # NOTHING before this; the url lives on the target info.
            turl = getattr(getattr(t,"target",None),"url","") or ""
            if index is not None and i==index: targets.append((i,t))
            elif url is not None and url in turl: targets.append((i,t))
        closed=0; ids=set()
        for i, t in targets:
            try:
                tid = getattr(getattr(t,"target",None),"target_id",None)
                await t.close()
                closed+=1; ids.add(tid)
            except Exception as e:
                log("closetab_err",i=i,e=str(e)[:80])
        await self._await_closed(ids)  # so "remaining" is the truth, not a mid-teardown snapshot
        if tag:
            self.tags.pop(tag, None)
        if self.tab not in self.browser.tabs:
            self.tab=self.browser.tabs[0] if self.browser.tabs else None
        return {"closed":closed,"remaining":len(self.browser.tabs)}
    async def _closeextra(self):
        """Close ALL tabs except the base tab (index 0) — the panic 'clean up leaked tabs' button. Use
        after a crashed/aborted crawl left orphan tabs. Never touches the server process."""
        await self._ensure(); await self._refresh()
        n=0; ids=set()
        for t in list(self.browser.tabs)[1:]:
            try:
                ids.add(getattr(getattr(t,"target",None),"target_id",None))
                await t.close(); n+=1
            except Exception: pass
        await self._await_closed(ids)
        self.tab=self.browser.tabs[0] if self.browser.tabs else None
        return {"closed":n,"remaining":len(self.browser.tabs)}
    async def _js(self, expr, tab=None):
        """Evaluate in the page, RESOLVING promises.

        Without await_promise an async expression handed back a bare Promise, which serialised to
        null — so `js` looked like it silently returned nothing for anything async. Top-level `await`
        is also not valid inside a CDP evaluate, so an expression starting with it is wrapped in an
        async IIFE rather than being rejected."""
        t = await self._tab(tab)
        raw = await t.evaluate(JS_WRAP % expr.strip().rstrip(";"),
                               await_promise=True, return_by_value=True)
        if not isinstance(raw, str):
            # The wrapper always resolves to a string, so anything else means the driver could not
            # evaluate it at all (usually a syntax error in the expression).
            return {"error": f"js could not be evaluated: {str(raw)[:300]}"}
        try:
            val = json.loads(raw)
        except ValueError:
            return {"result": raw}
        if isinstance(val, dict) and "__playwrong_error" in val:
            return {"error": f"js exception: {val['__playwrong_error']}"}
        return {"result": val}
    # ── prefetch: fire a batch of urls, collect them when they are ready ─────────────────────────
    # A page load is mostly WAITING. Fetching ten urls one at a time serialises ten waits; loading
    # them in N tabs at once overlaps them, and the caller reads results as they land instead of
    # blocking on the slowest page. Same one browser — each url gets its own tab, so nothing
    # contends, and the tab is closed the moment its page has been captured.
    async def _prefetch(self, urls, concurrency=8, solve=True, tries=20, timeout=30, owner=None):
        """Start a batch and return IMMEDIATELY with a job id. Nothing here blocks the caller."""
        await self._ensure()
        self._jobn = getattr(self, "_jobn", 0) + 1
        job = f"job{self._jobn}"
        urls = [u for u in (urls or []) if u]
        # Slots are keyed by INDEX, not url. Keying by url silently collapsed a batch that contained
        # the same url twice — "asked 3, got 2" — and any caller waiting for all of them then waited
        # forever. Duplicates in a url list are completely ordinary.
        self.jobs[job] = {"slots": {}, "total": len(urls), "delivered": 0}
        self._reap_jobs()
        asyncio.ensure_future(self._batch(job, urls, concurrency, solve, tries, timeout, owner))
        return {"job": job, "count": len(urls), "concurrency": concurrency, "timeout": timeout}

    def _reap_jobs(self):
        """Keep finished jobs queryable for a while, then drop the oldest.

        A drained job used to be deleted outright, so a caller that polled once more — the obvious
        way to check "am I done yet" — got "no such job" instead of "nothing left". Keeping the last
        20 makes the answer honest without growing without bound; bodies are already gone by then,
        so what remains is a counter."""
        done = [j for j, d in self.jobs.items() if d.get("total", 0) <= d.get("delivered", 0)
                and not d.get("slots")]
        for j in done[:-20]:
            self.jobs.pop(j, None)

    async def _batch(self, job, urls, concurrency, solve, tries, timeout=30, owner=None):
        """Load every url, at most `concurrency` at a time, each in its own tab.

        PER-URL DEADLINE, not just a batch one. Measured on a real run: 7 of 8 pages finished in 3
        seconds while a single wedged page held the batch for 104 more — exactly the stall this
        feature exists to remove. A page that has not finished within `timeout` is recorded as timed
        out and its tab closed, so one bad url costs one slot, never the whole batch.

        `timeout` covers the load AND any Turnstile solve, so raise it for challenge-heavy batches:
        a solve legitimately spends 10-30s clicking and waiting.
        """
        sem = asyncio.Semaphore(max(1, int(concurrency)))
        slots = self.jobs[job]["slots"]

        async def load(i, url, tabs):
            t = await self.browser.get("about:blank", new_tab=True)
            tabs[i] = t                         # recorded so a timeout can still close it
            tid = getattr(getattr(t, "target", None), "target_id", None)
            if owner:
                self.owners[tid] = owner        # a batch tab is short-lived but still someone's
            await t.get(url)
            await self._label(t)
            await t.sleep(2)
            title = await t.evaluate("document.title")
            html = await t.get_content()
            passed = None
            if solve and self._is_chal(title, html):
                passed = False
                for _ in range(tries):
                    title = await t.evaluate("document.title")
                    html = await t.get_content()
                    if not self._is_chal(title, html):
                        passed = True
                        break
                    try:
                        el = await t.find("verify you are human", best_match=True, timeout=3)
                        if el:
                            await el.mouse_click()
                            await t.sleep(4)
                    except Exception:
                        pass
                    await t.sleep(1)
            try: href = await t.evaluate("location.href")
            except Exception: href = url
            slots[i] = {"status": "ready", "title": strip_owner(title), "html": html, "url": href,
                        "requested": url, "challenge": passed}

        async def one(i, url):
            async with sem:                     # at most `concurrency` tabs open at once
                slots[i] = {"status": "loading", "requested": url}
                tabs = {}
                try:
                    await asyncio.wait_for(load(i, url, tabs), timeout=timeout)
                except TimeoutError:
                    # A load that never "finishes" usually still RENDERED — pypi.org does exactly
                    # this, holding a connection open long after the content is there. Take what
                    # rendered instead of discarding a good capture.
                    slots[i] = await self._partial(tabs.get(i), url, timeout)
                    log("prefetch_timeout", url=url[:60], salvaged=slots[i]["status"])
                except Exception as e:
                    slots[i] = {"status": "error", "requested": url, "error": repr(e)[:200]}
                    log("prefetch_err", url=url[:60], e=repr(e)[:80])
                finally:
                    t = tabs.get(i)
                    if t is not None:
                        try: await t.close()
                        except Exception: pass

        await asyncio.gather(*(one(i, u) for i, u in enumerate(urls)), return_exceptions=True)
        await self._refresh()
        log("prefetch_done", job=job, n=len(urls))

    async def _partial(self, t, url, timeout):
        """Best-effort capture from a tab whose load never completed. Returns a ready result when
        there is real content, otherwise an honest error."""
        if t is not None:
            try:
                title = await asyncio.wait_for(t.evaluate("document.title"), timeout=5)
                html = await asyncio.wait_for(t.get_content(), timeout=10)
                if html and len(html) > 500:      # enough to be a page, not a blank shell
                    return {"status": "ready", "title": title, "html": html, "url": url,
                            "requested": url, "partial": True}
            except Exception:
                pass
        return {"status": "error", "requested": url, "error": f"timed out after {timeout}s"}

    async def _jobstatus(self, job=None):
        """Counts only — cheap to poll, and never ships page bodies you have not asked for."""
        def summarise(j, d):
            v = list(d["slots"].values())
            return {"job": j, "total": d["total"], "delivered": d.get("delivered", 0),
                    "ready": sum(1 for x in v if x.get("status") == "ready"),
                    "loading": sum(1 for x in v if x.get("status") == "loading"),
                    "errors": sum(1 for x in v if x.get("status") == "error"),
                    "pending": d["total"] - d.get("delivered", 0) - len(v),
                    "done": d.get("delivered", 0) >= d["total"]}
        if job:
            d = self.jobs.get(job)
            return summarise(job, d) if d else {"error": f"no such job {job!r}"}
        return {"jobs": [summarise(j, d) for j, d in self.jobs.items()]}

    async def _collect(self, job, drain=True):
        """Hand back every result that is READY, and by default forget it.

        Draining matters: a batch of pages is tens of MB of HTML, and holding it after the caller has
        read it is a slow leak in a long-lived server. The job record itself survives so that asking
        again returns "nothing left" rather than "no such job"."""
        d = self.jobs.get(job)
        if not d:
            return {"error": f"no such job {job!r}"}
        done = {i: v for i, v in d["slots"].items() if v.get("status") in ("ready", "error")}
        if drain:
            for i in done:
                d["slots"].pop(i, None)
            d["delivered"] = d.get("delivered", 0) + len(done)
        remaining = d["total"] - d.get("delivered", 0)
        return {"job": job, "results": list(done.values()), "remaining": max(0, remaining),
                "done": remaining <= 0}

    async def _cookies(self):
        await self._ensure()
        cks = await self.browser.cookies.get_all()
        return {"cookies":[{"name":c.name,"value":c.value,"domain":getattr(c,"domain",None)} for c in cks]}
    def do(self, op, a):
        m={"start":lambda:self._start(),
           # every driving op takes an optional `tab` (a tag, or an index) so concurrent callers
           # never have to rely on which tab happens to be active
           "goto":lambda:self._goto(a["url"],a.get("tab")),
           "solve":lambda:self._solve(a.get("tries",20),a.get("tab")),
           "text":lambda:self._text(a.get("tab")),"shot":lambda:self._shot(a.get("tab")),
           "clearcookies":lambda:self._clearcookies(),
           "move":lambda:self._move(a["x"],a["y"],a.get("tab")),
           "click":lambda:self._click(a["x"],a["y"],a.get("tab")),
           "key":lambda:self._key(a["key"],a.get("tab")),
           "newtab":lambda:self._newtab(a.get("url","about:blank"),a.get("tag"),a.get("owner")),
           "js":lambda:self._js(a["expr"],a.get("tab")),"cookies":lambda:self._cookies(),
           "tabs":lambda:self._tabs(),
           "closetab":lambda:self._closetab(a.get("index"),a.get("url"),a.get("keep_first",True),
                                            a.get("tag")),
           "closeextra":lambda:self._closeextra(),
           "cdp":lambda:self._cdp_info(),
           "prefetch":lambda:self._prefetch(a.get("urls"),a.get("concurrency",8),
                                            a.get("solve",True),a.get("tries",20),
                                            a.get("timeout",30),a.get("owner")),
           "jobs":lambda:self._jobstatus(a.get("job")),
           "collect":lambda:self._collect(a.get("job"),a.get("drain",True))}
        if op not in m: return {"error":f"unknown {op}"}
        try: return self.run(m[op]())
        except Exception as e:
            # Log the traceback, not just repr(e): a bare "OverflowError(...)" with no file or line
            # is nearly useless when the cause is three frames down in a helper.
            log("op_err",op=op,e=repr(e)[:120],
                where="|".join(traceback.format_exc().strip().splitlines()[-3:])[:300])
            return {"error":repr(e)[:160]}

B = ND()
MARKERS={"aim":None,"cursor":None,"path":[],"box":None,"ollama":None}
VIZ_HTML="""<!doctype html><meta charset=utf-8><title>nd viz</title>
<style>body{margin:0;font:13px monospace;background:#111;color:#ddd;display:flex;height:100vh}
#l{flex:1;position:relative}#r{width:280px;padding:10px}img{width:100%}#ov{position:absolute;inset:0}</style>
<div id=l><img id=s><canvas id=ov></canvas></div><div id=r><b>nd viz</b><div id=i></div></div>
<script>const s=document.getElementById('s'),ov=document.getElementById('ov'),inf=document.getElementById('i');
async function t(){s.src='/frame?'+Date.now();const m=await(await fetch('/markers')).json();
await s.decode().catch(()=>{});ov.width=s.clientWidth;ov.height=s.clientHeight;
const sx=s.clientWidth/(s.naturalWidth||1),sy=s.clientHeight/(s.naturalHeight||1),c=ov.getContext('2d');
c.clearRect(0,0,ov.width,ov.height);
if(m.box){c.strokeStyle='lime';c.lineWidth=2;c.strokeRect(m.box[0]*sx,m.box[1]*sy,(m.box[2]-m.box[0])*sx,(m.box[3]-m.box[1])*sy);}
if(m.aim){c.strokeStyle='red';c.strokeRect(m.aim[0]*sx-12,m.aim[1]*sy-12,24,24);}
let o=m.ollama||{};inf.innerHTML='model '+(o.model||'-')+'<br>time '+(o.ms||'-')+'ms<br>conf '+(o.confidence||'-')+'<br>'+(o.description||'');}
setInterval(t,100);t();</script>"""

def _shutdown():
    """Stop the BROWSER, then exit.

    This used to be a bare os._exit(0). The HTTP process died instantly and Chrome — a separate
    process tree that nothing else was driving — kept running forever on its throwaway profile. Every
    `playwrong --stop` therefore leaked an entire browser: 12 of them were found holding 15.7 GB, all
    from one afternoon of testing. They are easy to miss because a headed window just sits there
    behind everything else.

    So: close the browser first, and only then exit. The stop is bounded (10s) because a wedged
    browser must not turn "stop" into "hang" — if it will not go quietly we exit anyway and
    scripts/cleanup_orphans.py can collect the remains.
    """
    try:
        if B.browser is not None:
            fut = asyncio.run_coroutine_threadsafe(_stop_browser(), B.loop)
            fut.result(timeout=10)
            log("browser_stopped")
    except Exception as e:
        log("shutdown_err", e=repr(e)[:120])
    os._exit(0)


async def _stop_browser():
    try:
        B.browser.stop()          # nodriver's own teardown: closes tabs and the browser process
    except Exception:
        pass


class H(BaseHTTPRequestHandler):
    def _j(self,o,c=200):
        # default=str so ONE unserialisable value can never kill the response. Without it the
        # encoder raises after send_response, the client sees "Remote end closed connection without
        # response", and the real cause is buried in the server's stdout log.
        b=json.dumps(o,default=str).encode();self.send_response(c)
        self.send_header("Content-Type","application/json");self.send_header("Content-Length",str(len(b)))
        self.end_headers();self.wfile.write(b)
    def _raw(self,b,ct,c=200):
        self.send_response(c);self.send_header("Content-Type",ct)
        self.send_header("Content-Length",str(len(b)));self.end_headers();self.wfile.write(b)
    def log_message(self,*a):pass
    def do_GET(self):
        if self.path=="/status":self._j({"server":True,"alive":B.tab is not None})
        elif self.path=="/viz":self._raw(VIZ_HTML.encode(),"text/html")
        elif self.path.startswith("/frame"):
            try:self._raw(B.run(B._frame()),"image/png")
            except Exception:self._raw(b"","text/plain",500)
        elif self.path=="/markers":self._j(MARKERS)
        elif self.path=="/tabs":
            try:self._j(B.run(B._tabs()))
            except Exception as e:self._j({"error":str(e)[:120]},500)
        else:self._j({"error":"?"},404)
    def do_POST(self):
        n=int(self.headers.get("Content-Length") or 0)
        raw=self.rfile.read(n) or b"{}"
        try:
            a=json.loads(raw)
        except ValueError as e:
            # Parsing outside a try dropped the connection mid-response, so the client saw
            # "Remote end closed connection without response" and no clue why.
            self._j({"error":f"invalid JSON body: {e}"},400); return
        if not isinstance(a, dict):
            self._j({"error":"body must be a JSON object"},400); return
        op=self.path.strip("/")
        if op=="shutdown":self._j({"ok":1});threading.Thread(target=_shutdown).start();return
        if op=="setmarkers":MARKERS.update(a);self._j(MARKERS);return
        self._j(B.do(op,a))

def _already_serving(port):
    """Is a healthy engine already on this port? Then this process must not become a second one.

    Belt and braces behind connect.ensure()'s spawn lock: anything at all can start server.py — a
    stale script, a race, a user in a terminal — and a second engine on the same port means a second
    Chrome, split state, and a browser nobody closes. Cheap to check, and the failure it prevents
    (255 browsers from one runaway loop) is expensive."""
    import urllib.request
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=3).read()
        return True
    except Exception:
        return False


if __name__=="__main__":
    if _already_serving(PORT):
        log("server_duplicate_exit", port=PORT)
        print(f"an engine is already serving 127.0.0.1:{PORT} — not starting a second one",
              file=sys.stderr)
        sys.exit(0)
    log("server_start",port=PORT)
    ThreadingHTTPServer(("127.0.0.1",PORT),H).serve_forever()
