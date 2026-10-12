"""cdp.py — minimal Chrome DevTools Protocol client: launch/attach Chrome, flat sessions, raw JSON.

Replaces the vendored nodriver (AGPL, issue #41). Only what playwrong uses:
  - one websocket to the browser target; each tab is a flattened session on it (no socket per tab)
  - send(method, params) -> result dict; events dispatched by method, per session
  - typed errors: ConnectionLost, CDPError (Chrome's own message kept), JSError, TargetGone

Stealth is by omission: no --enable-automation, no chromedriver, never Runtime.enable.
"""
import asyncio
import base64
import json
import os
import platform
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.request

# Chrome writing DevToolsActivePort after spawn: worst launch seen 2.8 s (`op_done op=start` in
# tmp/nd-server.log, 2026-10-12); 1.25x. On expiry: CDPLaunchError with the tail of Chrome's log.
LAUNCH_TIMEOUT = 3.5
# Websocket upgrade to a local Chrome: ms. 2 s covers a loaded box; on expiry ConnectionLost.
WS_OPEN_TIMEOUT = 2.0
# SIGTERM -> exit for Chrome: ~0.3 s typical. On expiry: SIGKILL.
STOP_TIMEOUT = 2.0

# First hit wins. Names are looked up on PATH; absolute paths are checked as they are.
CHROME_CANDIDATES = {
    "Linux": ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
              "/snap/bin/chromium"],
    "Darwin": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
               "/Applications/Chromium.app/Contents/MacOS/Chromium", "google-chrome"],
    "Windows": [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe", "chrome"],
}

# nodriver's launch flags, kept verbatim except the port (0 + DevToolsActivePort: no bind race).
# IsolateOrigins,site-per-process OFF keeps the Turnstile iframe in-process, so DOM.getDocument
# (pierce) reaches its checkbox. Do not drop it.
CHROME_FLAGS = [
    "--remote-allow-origins=*", "--no-first-run", "--no-service-autorun",
    "--no-default-browser-check", "--homepage=about:blank", "--no-pings",
    "--password-store=basic", "--disable-infobars", "--disable-breakpad",
    "--disable-dev-shm-usage", "--disable-session-crashed-bubble",
    "--disable-search-engine-choice-screen",
    "--disable-features=IsolateOrigins,site-per-process",
]

PROFILE_PREFIX = "playwrong-profile-"


class CDPError(RuntimeError):
    """Chrome answered a command with an error. str() carries Chrome's own message."""
    def __init__(self, method, code, message):
        super().__init__(f"{method}: {message} ({code})")
        self.method, self.code, self.message = method, code, message


class ConnectionLost(ConnectionError):
    """The websocket to Chrome closed. Every pending and later command raises this."""


class TargetGone(RuntimeError):
    """The tab's target was destroyed or detached while a command was in flight."""


class JSError(RuntimeError):
    """Runtime.evaluate threw in the page."""


class CDPLaunchError(RuntimeError):
    """Chrome did not start, or did not open its DevTools port in time."""


def chrome_candidates():
    return CHROME_CANDIDATES.get(platform.system(), CHROME_CANDIDATES["Linux"])


def find_chrome():
    """PH_CHROME if set, else the first installed candidate for this platform."""
    p = os.environ.get("PH_CHROME")
    if p:
        return p
    for c in chrome_candidates():
        p = c if os.path.isabs(c) and os.path.exists(c) else shutil.which(c)
        if p:
            return p
    raise CDPLaunchError(f"no Chrome found (tried {', '.join(chrome_candidates())}); "
                         "install Google Chrome or set PH_CHROME to its binary")


def _log_tail(path, n=600):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - n))
            return f.read().decode(errors="replace").strip()
    except OSError:
        return ""


async def launch(headless=False, user_data_dir=None, extra_args=None, log_path=None):
    """Spawn Chrome and connect. Returns a Browser that owns the process (and a temp profile)."""
    temp = user_data_dir is None
    udd = tempfile.mkdtemp(prefix=PROFILE_PREFIX) if temp else user_data_dir
    port_file = os.path.join(udd, "DevToolsActivePort")
    try:
        os.unlink(port_file)              # a persistent profile keeps the last run's port
    except FileNotFoundError:
        pass
    args = [find_chrome(), *CHROME_FLAGS, f"--user-data-dir={udd}",
            "--remote-debugging-host=127.0.0.1", "--remote-debugging-port=0"]
    if headless:
        args.append("--headless=new")
    if getattr(os, "getuid", lambda: 1)() == 0:
        args.append("--no-sandbox")
    args += list(extra_args or ())
    args.append("about:blank")
    log_f = open(log_path, "wb") if log_path else subprocess.DEVNULL    # truncated per launch
    # Own process group: stop() ends Chrome's helpers too, not just the main process.
    proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log_f, stderr=log_f,
                            start_new_session=True)
    if log_path:
        log_f.close()
    t0 = time.monotonic()
    port = None
    while time.monotonic() - t0 < LAUNCH_TIMEOUT:
        if proc.poll() is not None:
            break
        try:
            with open(port_file) as f:
                first = f.readline().strip()
            if first:
                port = int(first)
                break
        except (OSError, ValueError):
            pass
        await asyncio.sleep(0.02)
    if port is None:
        why = (f"exited with code {proc.returncode}" if proc.poll() is not None else
               f"opened no DevTools port within {LAUNCH_TIMEOUT}s "
               f"(waited {time.monotonic() - t0:.2f}s)")
        if proc.poll() is None:
            proc.kill()
        if temp:
            shutil.rmtree(udd, ignore_errors=True)
        tail = _log_tail(log_path) if log_path else ""
        raise CDPLaunchError(f"Chrome {why}: {' '.join(args[:1])}" + (f"\n{tail}" if tail else ""))
    b = await attach("127.0.0.1", port)
    b.process, b.user_data_dir, b._temp_profile = proc, udd, temp
    return b


def _version(host, port):
    # Local DevTools HTTP endpoint, not the web: urllib is right here. Same bound as the ws open.
    with urllib.request.urlopen(f"http://{host}:{port}/json/version", timeout=WS_OPEN_TIMEOUT) as r:
        return json.loads(r.read())


async def attach(host, port):
    """Connect to a running Chrome's DevTools endpoint. The Browser owns no process."""
    # Imported here so find_chrome() and the rest stay stdlib-only (scripts/doctor.py uses them).
    from websockets.asyncio.client import connect as _ws_connect
    try:
        info = await asyncio.to_thread(_version, host, port)
        ws = await _ws_connect(info["webSocketDebuggerUrl"], max_size=2 ** 28, ping_interval=None,
                               open_timeout=WS_OPEN_TIMEOUT)
    except Exception as e:
        raise ConnectionLost(f"could not connect to Chrome at {host}:{port}: {e!r}") from e
    b = Browser(host, port, ws)
    await b._start()
    return b


class Browser:
    def __init__(self, host, port, ws):
        self.host, self.port, self.ws = host, port, ws
        self.process = None
        self.user_data_dir = None
        self._temp_profile = False
        self._id = 0
        self._pending = {}          # id -> (future, session_id, method)
        self._handlers = {}         # method -> [callback] for browser-level events
        self._sessions = {}         # session_id -> Tab
        self._tabs = {}             # target_id -> Tab, insertion order = first seen
        self.closed = None          # ConnectionLost once the socket is gone
        self._reader = None

    async def _start(self):
        self._reader = asyncio.ensure_future(self._read())
        self.on("Target.targetCreated", self._on_target)
        self.on("Target.targetInfoChanged", self._on_target)
        self.on("Target.targetDestroyed", self._on_destroyed)
        self.on("Target.detachedFromTarget", self._on_detached)
        await self.send("Target.setDiscoverTargets", {"discover": True})
        await self.refresh()

    # ── wire ──────────────────────────────────────────────────────────────────────────────────
    async def send(self, method, params=None, session_id=None):
        if self.closed:
            raise self.closed
        self._id += 1
        i = self._id
        fut = asyncio.get_running_loop().create_future()
        self._pending[i] = (fut, session_id, method)
        msg = {"id": i, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        try:
            await self.ws.send(json.dumps(msg))
        except Exception as e:
            self._pending.pop(i, None)
            self._lost(e)
            raise self.closed from e
        try:
            return await fut
        finally:
            self._pending.pop(i, None)

    async def _read(self):
        err = None
        try:
            async for raw in self.ws:
                m = json.loads(raw)
                if "id" in m:
                    p = self._pending.get(m["id"])
                    if p and not p[0].done():
                        if "error" in m:
                            e = m["error"]
                            p[0].set_exception(CDPError(p[2], e.get("code"), e.get("message")))
                        else:
                            p[0].set_result(m.get("result", {}))
                    continue
                sid = m.get("sessionId")
                tab = self._sessions.get(sid) if sid else None
                handlers = (tab._handlers if tab else self._handlers).get(m.get("method"), ())
                for cb in list(handlers):
                    self._dispatch(cb, m.get("params", {}))
        except Exception as e:
            err = e
        self._lost(err)

    @staticmethod
    def _dispatch(cb, params):
        # Async handlers run as tasks: a handler that awaits a send must never stall the reader.
        try:
            r = cb(params)
            if asyncio.iscoroutine(r):
                asyncio.ensure_future(r)
        except Exception:
            pass

    def _lost(self, err):
        if self.closed:
            return
        self.closed = ConnectionLost(f"Connection closed to Chrome {self.host}:{self.port}"
                                     + (f": {err!r}" if err else ""))
        for fut, _, _ in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(self.closed)

    def fail_session(self, session_id, err):
        """Fail every command in flight on one session (a crashed or detached tab)."""
        for fut, sid, _ in list(self._pending.values()):
            if sid == session_id and not fut.done():
                fut.set_exception(err)

    def on(self, method, cb):
        self._handlers.setdefault(method, []).append(cb)

    # ── targets ───────────────────────────────────────────────────────────────────────────────
    def _tab_for(self, info):
        tid = info["targetId"]
        t = self._tabs.get(tid)
        if t is None:
            t = self._tabs[tid] = Tab(self, tid)
        t._info(info)
        return t

    def _on_target(self, p):
        info = p.get("targetInfo", {})
        if info.get("type") == "page":
            self._tab_for(info)

    def _on_destroyed(self, p):
        t = self._tabs.pop(p.get("targetId"), None)
        if t is not None:
            t._gone(TargetGone(f"tab {t.target_id[:8]} was closed"))

    def _on_detached(self, p):
        t = self._sessions.get(p.get("sessionId"))
        if t is not None:
            t._detached(TargetGone(f"tab {t.target_id[:8]} detached"))

    async def refresh(self):
        """Re-read the page targets from Chrome; drops tabs Chrome no longer lists."""
        r = await self.send("Target.getTargets")
        live = set()
        for info in r.get("targetInfos", []):
            if info.get("type") == "page":
                live.add(info["targetId"])
                self._tab_for(info)
        for tid in [t for t in self._tabs if t not in live]:
            self._tabs.pop(tid)._gone(TargetGone(f"tab {tid[:8]} is gone"))

    @property
    def tabs(self):
        return list(self._tabs.values())

    async def new_tab(self, url="about:blank", new_window=False):
        r = await self.send("Target.createTarget", {"url": url, "newWindow": new_window})
        tid = r["targetId"]
        return self._tabs.get(tid) or self._tab_for({"targetId": tid, "url": url, "type": "page"})

    async def close_target(self, target_id):
        await self.send("Target.closeTarget", {"targetId": target_id})

    # ── browser-wide ──────────────────────────────────────────────────────────────────────────
    async def cookies(self):
        return (await self.send("Storage.getCookies")).get("cookies", [])

    async def clear_cookies(self):
        await self.send("Storage.clearCookies")

    @property
    def stopped(self):
        """Owned process: has it exited. Attached handle: is the socket gone."""
        if self.process is not None:
            return self.process.poll() is not None
        return self.closed is not None

    async def aclose(self):
        """Close the websocket only; Chrome keeps running."""
        try:
            await self.ws.close()
        except Exception:
            pass
        self._lost(None)

    def stop(self):
        """Terminate the owned Chrome and remove a temp profile. Sync: safe from any thread."""
        try:
            self.ws.transport.abort()
        except Exception:
            pass
        p = self.process
        if p is not None:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(STOP_TIMEOUT)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait(STOP_TIMEOUT)
            try:
                os.killpg(p.pid, signal.SIGKILL)   # helpers still writing into the profile
            except (ProcessLookupError, PermissionError, AttributeError):   # AttributeError: Windows
                pass
        if self._temp_profile and self.user_data_dir:
            # Helpers die within ms of SIGKILL; 0.5 s of retries, then leave it for cleanup_orphans.
            for _ in range(10):
                shutil.rmtree(self.user_data_dir, ignore_errors=True)
                if not os.path.exists(self.user_data_dir):
                    break
                time.sleep(0.05)


class Tab:
    """One page target. Commands go over a flattened session, attached on first use."""
    def __init__(self, browser, target_id):
        self.browser, self.target_id = browser, target_id
        self.url = self.title = self.type = ""
        self.session_id = None
        self.error = None           # set when crashed/closed: every later send raises it
        self._handlers = {}
        self._attaching = None

    def _info(self, info):
        self.url = info.get("url", self.url)
        self.title = info.get("title", self.title)
        self.type = info.get("type", self.type)

    def _gone(self, err):
        self.error = self.error or err
        self._detached(err)

    def _detached(self, err):
        if self.session_id:
            self.browser._sessions.pop(self.session_id, None)
            self.browser.fail_session(self.session_id, err)
        self.session_id = None
        self._attaching = None

    def fail(self, err):
        """Mark the tab dead (renderer crash): in-flight and later commands raise err."""
        self.error = err
        if self.session_id:
            self.browser.fail_session(self.session_id, err)

    @property
    def attached(self):
        return self.session_id is not None

    async def _attach(self):
        # One attach shared by concurrent first sends; a failed one is retried by the next send.
        a = self._attaching
        if a is None or (a.done() and (a.cancelled() or a.exception())):
            async def go():
                r = await self.browser.send("Target.attachToTarget",
                                            {"targetId": self.target_id, "flatten": True})
                self.session_id = r["sessionId"]
                self.browser._sessions[self.session_id] = self
            a = self._attaching = asyncio.ensure_future(go())
        await asyncio.shield(a)

    async def send(self, method, params=None):
        if self.error:
            raise self.error
        if not self.session_id:
            await self._attach()
        return await self.browser.send(method, params, session_id=self.session_id)

    def on(self, method, cb):
        self._handlers.setdefault(method, []).append(cb)

    def off(self, method, cb):
        try:
            self._handlers.get(method, []).remove(cb)
        except ValueError:
            pass

    # ── page ops ──────────────────────────────────────────────────────────────────────────────
    async def navigate(self, url):
        """Page.navigate: returns once Chrome has started the load, not when it finishes."""
        return await self.send("Page.navigate", {"url": url})

    async def evaluate(self, expr, await_promise=False):
        """The expression's value (by value). Raises JSError if it threw."""
        r = await self.send("Runtime.evaluate", {
            "expression": expr, "returnByValue": True, "awaitPromise": await_promise,
            "userGesture": True, "allowUnsafeEvalBlockedByCSP": True})
        if "exceptionDetails" in r:
            d = r["exceptionDetails"]
            raise JSError((d.get("exception") or {}).get("description") or d.get("text") or str(d))
        return r.get("result", {}).get("value")

    async def content(self):
        """Serialised document, doctype included. Works with page JS disabled."""
        doc = await self.send("DOM.getDocument", {"depth": 0})
        r = await self.send("DOM.getOuterHTML", {"backendNodeId": doc["root"]["backendNodeId"]})
        return r.get("outerHTML", "")

    async def screenshot(self):
        r = await self.send("Page.captureScreenshot", {"format": "png"})
        return base64.b64decode(r["data"])

    async def click(self, x, y):
        """Move, press, release at (x, y) in CSS pixels of the viewport."""
        x, y = float(x), float(y)
        await self.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        for ty in ("mousePressed", "mouseReleased"):
            await self.send("Input.dispatchMouseEvent", {"type": ty, "x": x, "y": y,
                                                         "button": "left", "buttons": 1,
                                                         "clickCount": 1})

    async def find_text(self, text, timeout=3.0, poll=0.5):
        """Centre (x, y) of the element whose text best matches `text`, searching iframes and shadow
        roots; None after `timeout`. Best match = text length closest to the needle's."""
        t0 = time.monotonic()
        while True:
            try:
                doc = (await self.send("DOM.getDocument", {"depth": -1, "pierce": True}))["root"]
                for el in sorted(_text_hosts(doc, text),
                                 key=lambda e: abs(len(text) - len(_text_all(e)))):
                    try:
                        q = await self.send("DOM.getContentQuads",
                                            {"backendNodeId": el["backendNodeId"]})
                    except CDPError:
                        continue                    # not rendered (display:none, detached)
                    if q.get("quads"):
                        quad = q["quads"][0]
                        return sum(quad[0::2]) / 4, sum(quad[1::2]) / 4
            finally:
                try:
                    await self.send("DOM.disable")   # stop Chrome tracking the whole tree
                except Exception:
                    pass
            if time.monotonic() - t0 >= timeout:
                return None
            await asyncio.sleep(poll)

    async def close(self):
        """Close through the BROWSER session: a crashed tab's own session never answers (#28)."""
        await self.browser.close_target(self.target_id)


def _children(n):
    kids = list(n.get("children", ())) + list(n.get("shadowRoots", ()))
    if n.get("contentDocument"):
        kids.append(n["contentDocument"])
    return kids


def _text_hosts(root, needle):
    """Nearest element above every text node containing `needle` (case-insensitive)."""
    needle = needle.strip().lower()
    out, stack = [], [(root, None)]
    while stack:
        n, el = stack.pop()
        if n.get("nodeType") == 3:
            if el is not None and needle in (n.get("nodeValue") or "").lower():
                out.append(el)
            continue
        here = n if n.get("nodeType") == 1 else el
        stack.extend((k, here) for k in _children(n))
    return out


def _text_all(el):
    parts, stack = [], [el]
    while stack:
        n = stack.pop()
        if n.get("nodeType") == 3:
            parts.append(n.get("nodeValue") or "")
        else:
            stack.extend(_children(n))
    return " ".join(p.strip() for p in parts if p.strip())
