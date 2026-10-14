"""server.py — the capture engine: ONE headed Chrome, driven over HTTP, shared by every caller.

Raw CDP (engine/cdp.py), not Playwright: Playwright is the Turnstile tell (Cloudflare serves it a
dead challenge that never resolves). One browser does everything — clear Turnstile, capture pages,
feed the viz screenshots — so there is no second browser and no orphan windows.

Control port (POST JSON): goto{url} solve{tries} text shot js{expr} cookies clearcookies
                          newtab closetab closeextra tabs cdp window move click key shutdown
GET: /status /tabs /viz /frame /markers ; POST /setmarkers

You do not need to run this by hand — engine/connect.py starts it on demand. To run it in the
foreground anyway:

    python engine/server.py            # PH_PORT (default 8731); log in tmp/nd-server.log
                                       # PH_MEMORY_MAX (default 8G) caps python+Chrome via systemd
"""
import asyncio
import base64
import ctypes
import json
import os
import re
import select
import signal
import subprocess
import sys
import threading
import time
import traceback
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cdp  # engine/cdp.py; server.py runs as a script, so its dir is on sys.path
import connect
import logroll
import memcap

PORT = int(os.environ.get("PH_PORT", "8731"))


def profile_dir():
    """Chrome's user-data-dir: PH_PROFILE_DIR (a path), PH_PROFILE (a name, under
    $XDG_DATA_HOME/playwrong/profiles/), or None = a throwaway profile per launch (the default)."""
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


# A local `git` answers in ms; 5 s covers a cold disk. On expiry the sha is reported as None.
GIT_TIMEOUT = 5


def code_version():
    """{sha, dirty, started} of the source this process RUNS, stamped at launch: a long-lived engine
    keeps serving old code after the checkout moves. sha is None outside a git checkout."""
    here = os.path.dirname(os.path.abspath(__file__))
    sha = dirty = None
    try:
        r = subprocess.run(["git", "-C", here, "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=GIT_TIMEOUT)
        if r.returncode == 0:
            sha = r.stdout.strip()
            # Repo root, not engine/: "dirty" means the whole checkout differs from that sha.
            root = subprocess.run(["git", "-C", here, "rev-parse", "--show-toplevel"],
                                  capture_output=True, text=True,
                                  timeout=GIT_TIMEOUT).stdout.strip() or here
            d = subprocess.run(["git", "-C", root, "status", "--porcelain"],
                               capture_output=True, text=True, timeout=GIT_TIMEOUT)
            dirty = bool(d.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return {"sha": sha, "dirty": dirty, "started": datetime.now(UTC).isoformat(timespec="seconds")}


CODE = code_version()
TMP = connect.DATA                               # checkout tmp/, or the XDG cache when installed
LOG = os.path.join(TMP, "nd-server.log")
CHROME_LOG = os.path.join(TMP, "chrome.log")     # Chrome's stdout/stderr, truncated per launch

# Owner label in the tab title, for whoever watches the shared browser. The rare ⟦⟧ pair makes
# stripping it from returned titles exact (#16).
OWNER_L, OWNER_R = "\u27e6", "\u27e7"
OWNER_TITLE_RE = re.compile(rf"^{OWNER_L}[^{OWNER_R}]*{OWNER_R}\s*")


def unlabel(v, owner):
    """Remove this tab's own ⟦owner⟧ label from a returned value, recursing into lists and dicts (a
    js result can be any shape). Exact string, so a page's own ⟦⟧ survives (#16)."""
    if not owner:
        return v
    if isinstance(v, str):
        return v.replace(f"{OWNER_L}{owner}{OWNER_R} ", "")
    if isinstance(v, list):
        return [unlabel(x, owner) for x in v]
    if isinstance(v, dict):
        return {k: unlabel(x, owner) for k, x in v.items()}
    return v


def strip_owner(title):
    """The page's own title: the owner label is for the human watching, never for callers."""
    return OWNER_TITLE_RE.sub("", title or "")

# Serialise the caller's expression IN THE PAGE and hand back a JSON string: undefined, promises and
# thrown errors (with stack) all arrive as one plain string. `await (…)` covers sync and async.
JS_WRAP = ("(async () => { try { const v = await (%s);"
           " return JSON.stringify(v === undefined ? null : v); }"
           " catch (e) { return JSON.stringify("
           "{__playwrong_error: String((e && e.stack) || e)}); } })()")

PID = os.getpid()
STARTED = time.monotonic()

def log(a, **k):
    # pid + rss on every line, so an OOM record (pid, size) can be tied to this log (#23).
    ts = datetime.now(UTC).isoformat(timespec="milliseconds")
    try:
        logroll.roll(LOG)                 # every engine on this checkout shares the file (#39)
        with open(LOG, "a") as f:
            f.write(f"{ts} pid={PID} rss_mb={proc_status_mb('VmRSS')} {a} "
                    + " ".join(f"{x}={y}" for x,y in k.items()) + "\n")
    except Exception:
        pass

# ── memory: measure, bound, fail loud (#23) ──────────────────────────────────────────────────────
MEMORY_MAX = os.environ.get("PH_MEMORY_MAX", "8G")     # cgroup cap for python + Chrome; "0" = none
MEMORY_CAP = "unknown"                                   # what memcap.enter() achieved, for /status

# Self-restart threshold on this process's RSS, checked after every op and every 60 s. A healthy
# engine stays well under 100 MB (#23); 2G is a leak, and a quarter of the default cgroup cap.
RSS_LIMIT = memcap.parse_size(os.environ.get("PH_RSS_LIMIT", "2G")) or (2 << 30)

def proc_status_mb(key):
    """One Vm* field of /proc/self/status in MB, or None off Linux."""
    try:
        for line in open("/proc/self/status"):
            if line.startswith(key):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None

# PH_DISPLAY=xvfb: headed Chrome on the engine's own Xvfb, off the desktop (still headed, so nothing
# changes for Turnstile; test engines use it). Unset = the user's desktop.
DISPLAY_MODE = os.environ.get("PH_DISPLAY", "")
# Xvfb answering -displayfd. Expiry is fatal (the engine exits naming Xvfb), and starts on a loaded
# box ran past 0.2 s (#36, #53), so 1 s.
XVFB_START_TIMEOUT = 1.0
XVFB = None                 # the Xvfb Popen in PH_DISPLAY=xvfb mode

def _die_with_parent():
    """Xvfb is the engine's; if the engine is killed, take Xvfb with it (PR_SET_PDEATHSIG)."""
    ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)

def start_xvfb():
    """Start the engine's own Xvfb on a free display and point DISPLAY at it. Returns ':N'."""
    global XVFB
    r, w = os.pipe()
    t0 = time.monotonic()
    XVFB = subprocess.Popen(["Xvfb", "-displayfd", str(w), "-screen", "0", "1920x1080x24",
                             "-nolisten", "tcp"], pass_fds=(w,), stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            preexec_fn=_die_with_parent)
    os.close(w)
    ready, _, _ = select.select([r], [], [], XVFB_START_TIMEOUT)
    n = os.read(r, 16).decode().strip() if ready else ""
    os.close(r)
    if not n:
        XVFB.terminate()
        raise RuntimeError(f"Xvfb did not report a display within {XVFB_START_TIMEOUT}s "
                           f"(PH_DISPLAY=xvfb); is it installed and runnable?")
    os.environ["DISPLAY"] = f":{n}"
    for k in ("WAYLAND_DISPLAY", "XAUTHORITY"):    # Chrome must not fall back to the desktop
        os.environ.pop(k, None)
    log("xvfb_started", display=f":{n}", pid=XVFB.pid, ms=int((time.monotonic() - t0) * 1000))
    return f":{n}"

# Headed by default: headless is the Turnstile tell. PH_HEADLESS=1 is for test engines on an
# isolated port, so test runs don't open windows on the user's desktop.
HEADLESS = os.environ.get("PH_HEADLESS") == "1"
# Chrome recycle triggers (#23): open tabs (2x the crawler's --tabs cap) and tabs opened since launch.
CHROME_MAX_TABS = int(os.environ.get("PH_CHROME_MAX_TABS", "64"))
CHROME_MAX_FETCHES = int(os.environ.get("PH_CHROME_MAX_FETCHES", "1000"))
# New tabs are refused above this share of the cap held as unreclaimable memory, so a caller backs
# off before the kernel has to kill a renderer (#28).
HIGH_WATER = float(os.environ.get("PH_MEMORY_HIGH_WATER", "0.85"))
# Per-tab heap query in the 60 s mem line: a local CDP round trip is ms; a tab busy in JS answers
# late, and 1 s is past that. On expiry the tab is logged heap_mb=None ("busy"), not waited on.
HEAP_QUERY_TIMEOUT = 1.0

# How long a dropped tab socket waits for the matching Target.targetCrashed before it is treated as
# the browser dying: a local CDP event lands in ms, 0.25 s is far past that and still short.
CRASH_EVENT_WAIT = 0.25

NEWTAB_TIMEOUT, CLOSE_TIMEOUT = cdp.NEWTAB_TIMEOUT, cdp.CLOSE_TIMEOUT
# Unanswered newtabs in a row before the browser connection is treated as dead and reattached
# (same Chrome, session kept) instead of wedging until a manual --stop. 2: one can be a blip.
NEWTAB_STRIKES = 2

class NewTabTimeout(RuntimeError):
    """Chrome did not answer Target.createTarget within NEWTAB_TIMEOUT (#29)."""

class TabCrashed(RuntimeError):
    """An op aimed at a tab whose renderer process is gone (#28)."""

class MemoryHighWater(RuntimeError):
    """A new tab was refused because the engine's scope is near its memory cap (#28)."""

def scope_mb():
    """The engine scope's memory in MB, from its cgroup files: current/peak/unreclaimable/max."""
    m = memcap.cgroup_memory()
    out = {f"scope_{k}_mb": (v >> 20 if v is not None else None) for k, v in m.items()}
    return {**out, "scope_oom_kills": memcap.oom_kills()}

def top_renderers(n=3):
    """The n largest Chrome renderers in this scope as "pid:MB", from /proc; no CDP traffic."""
    out = []
    try:
        pids = open(f"{memcap._cgroup_dir()}/cgroup.procs").read().split()
    except (OSError, TypeError):
        return ""
    for pid in pids:
        try:
            if b"--type=renderer" not in open(f"/proc/{pid}/cmdline", "rb").read():
                continue
            for line in open(f"/proc/{pid}/status"):
                if line.startswith("VmRSS"):
                    out.append((int(line.split()[1]) >> 10, pid))
        except (OSError, ValueError):
            continue
    return ",".join(f"{pid}:{mb}" for mb, pid in sorted(out, reverse=True)[:n])

def check_headroom(what):
    """Raise MemoryHighWater when unreclaimable memory is over HIGH_WATER of the cap."""
    m = memcap.cgroup_memory()
    if m.get("max") and m["unreclaimable"] > HIGH_WATER * m["max"]:
        used, cap = m["unreclaimable"] >> 20, m["max"] >> 20
        log("high_water", refused=what, unreclaimable_mb=used, max_mb=cap, high_water=HIGH_WATER)
        raise MemoryHighWater(
            f"engine memory high-water: {used} MB unreclaimable of the {cap} MB cap "
            f"(over {HIGH_WATER:.0%}), so {what} was refused. Close tabs you no longer need, "
            f"or retry once other work finishes.")

# After Page.navigate returns (the load has only started): let the document arrive before reading.
SETTLE = 2.0
# Past SETTLE, wait for readyState "complete": ~1.25x the worst seen (#32). Expiry flags the capture
# partial, never silent.
LOAD_WAIT = 5.0
# Solve loop: look for the checkbox (a page shows it within ~2 s); after a click, Turnstile verifies
# and reloads (~3 s); between tries. A try is ~8 s worst case, which connect.solve_timeout() assumes.
FIND_TIMEOUT, AFTER_CLICK, SOLVE_PAUSE = 3.0, 4.0, 1.0
# closeTarget is acked before teardown (ms); past this a tab is reported still open.
CLOSE_SETTLE = 2.0
# A prefetch job nobody polled for this long belongs to a dead client: 20x the default per-url timeout.
JOB_IDLE = 600
# Reading a timed-out tab: a busy renderer still answers within seconds; past this, report the timeout.
PARTIAL_READ_TIMEOUT = 5.0
# /status probe: one local round trip is ms; past this the loop is busy and `alive` falls back to
# the process check.
STATUS_PROBE_TIMEOUT = 3.0

def cdp_sockets():
    """Established TCP connections to Chrome's CDP port (/proc/net/tcp): the engine's one, plus any
    attached clients. Where a socket leak shows first (#23)."""
    port = B.cdp_endpoint.get("port")
    if not port:
        return 0
    n = 0
    try:
        for line in open("/proc/net/tcp").readlines()[1:]:
            f = line.split()
            if f[3] == "01" and int(f[2].split(":")[1], 16) == port:
                n += 1
    except (OSError, ValueError, IndexError):
        return None
    return n

def mem_snapshot():
    """What /status and the watchdog report. Three /proc reads, no CDP traffic."""
    try:
        fds = len(os.listdir("/proc/self/fd"))
    except OSError:
        fds = None
    return {"pid": PID, "rss_mb": proc_status_mb("VmRSS"), "swap_mb": proc_status_mb("VmSwap"),
            "fds": fds, "cdp_sockets": cdp_sockets(), "threads": threading.active_count(),
            "tabs": B.tab_count(), "uptime_s": int(time.monotonic() - STARTED), **scope_mb()}

# mem line + RSS check while idle (no op to catch growth on). 60 s bounds a #23-rate leak between
# samples while keeping the log small.
MEMWATCH_PERIOD = 60

def _memwatch():
    while True:
        time.sleep(MEMWATCH_PERIOD)
        s = mem_snapshot()
        log("mem", **s, top_renderers=top_renderers())
        try:
            for h in B.run_timeout(B._tab_heaps(), HEAP_QUERY_TIMEOUT + 1.0):
                log("tab_mem", **h)
        except Exception as e:
            log("tab_mem_err", e=repr(e)[:80])
        B.loop.call_soon_threadsafe(B._reap_jobs)
        check_rss("watchdog")

def check_rss(where):
    """The self-restart trigger (#23): over RSS_LIMIT -> retire this engine. Returns the RSS read."""
    rss = proc_status_mb("VmRSS")
    if rss and (rss << 20) > RSS_LIMIT:
        _retire_engine(where=where, rss_mb=rss, limit_mb=RSS_LIMIT >> 20)
    return rss

SRV = None                      # the ThreadingHTTPServer, once bound
# Retire waits this long for in-flight ops: a slow real page loads in seconds; past it the op is cut
# and logged (retire_cut).
RETIRE_DRAIN = 40.0
_INFLIGHT = [0]                 # ops being served right now; guarded by _INFLIGHT_LOCK
_INFLIGHT_LOCK = threading.Lock()
_RETIRING = threading.Event()

def _retire_engine(**why):
    """Stop taking requests, let in-flight ops finish, close Chrome, exit 3. connect.py respawns the
    engine on the next call, so from a caller's side this is one failed request, not an outage."""
    if _RETIRING.is_set():
        return
    _RETIRING.set()
    log("rss_limit", action="retire", **why)
    threading.Thread(target=_retire_worker, daemon=True, name="retire").start()

def _retire_worker():
    if SRV is not None:
        SRV.shutdown()          # returns once serve_forever() has stopped accepting (0.5s poll)
    deadline = time.monotonic() + RETIRE_DRAIN
    while time.monotonic() < deadline and _INFLIGHT[0] > 0:
        time.sleep(0.1)
    if _INFLIGHT[0]:
        log("retire_cut", inflight=_INFLIGHT[0], waited_s=RETIRE_DRAIN)
    _shutdown(code=3)

def ensure_display(env=None, uid=None, x11_dir="/tmp/.X11-unix", run_dir=None):
    """Adopt the user's desktop display when spawned without one; headed Chrome cannot start otherwise.
    An engine spawned from an ssh/remote-editor shell has no DISPLAY, and Chrome then exits at launch.
    Returns what was adopted; raises if there is none."""
    env = os.environ if env is None else env
    if env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"):
        return {}
    uid = os.getuid() if uid is None else uid
    run_dir = run_dir or f"/run/user/{uid}"
    got = {}
    try:
        socks = sorted((int(n[1:]), n) for n in os.listdir(x11_dir) if re.fullmatch(r"X\d+", n)
                       and os.stat(os.path.join(x11_dir, n)).st_uid == uid)
    except OSError:
        socks = []
    if socks:
        got["DISPLAY"] = f":{socks[0][0]}"
        if not env.get("XAUTHORITY"):
            try:
                auth = sorted(n for n in os.listdir(run_dir) if n.startswith(".mutter-Xwaylandauth."))
            except OSError:
                auth = []
            home_auth = os.path.expanduser("~/.Xauthority")
            if auth:
                got["XAUTHORITY"] = os.path.join(run_dir, auth[0])
            elif os.path.exists(home_auth):
                got["XAUTHORITY"] = home_auth
    elif os.path.exists(os.path.join(run_dir, "wayland-0")):
        got["WAYLAND_DISPLAY"] = "wayland-0"
    if not got:
        raise RuntimeError(f"no display: headed Chrome needs a desktop session for uid {uid}, and none "
                           f"was found ({x11_dir}/X*, {run_dir}/wayland-0). Log in to the desktop, "
                           "or start the engine with DISPLAY set.")
    env.update(got)
    return got

def heal_profile(profile_dir):
    """Remove a persistent profile's Singleton* files when their owning pid is dead.
    - A crash or reboot leaves them behind, and the next launch on that profile hangs on the lock.
    - A live owner (another engine on this profile) is left alone."""
    if not profile_dir or not os.path.isdir(profile_dir):
        return
    lock = os.path.join(profile_dir, "SingletonLock")
    if not os.path.lexists(lock):
        return
    # Only SingletonLock's target ("<host>-<pid>") holds a pid; the other two hold a random number
    # and a socket path.
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


def _dead_conn(e):
    """Is this exception "the browser went away", as opposed to a page-level failure?
    cdp.ConnectionLost is a ConnectionError; websockets' own close errors are matched by name."""
    return isinstance(e, ConnectionError) or "ConnectionClosed" in type(e).__name__


def _lost_target(e):
    """Is this "the TAB went away" with Chrome still fine (#22)? Page-level: re-point, never forget().
    Chrome's -32001/-32602 messages cover a session or target that outlived the tab."""
    s = str(e)
    return (isinstance(e, cdp.TargetGone) or "Session with given id not found" in s
            or "No target with given id" in s)


class ND:
    """The browser on its own asyncio loop in a thread; sync-facing .do() for the HTTP handler."""
    def __init__(self):
        self.loop = asyncio.new_event_loop(); self.browser=self.tab=None
        self._dead = False      # set when an op hits a dropped connection; cleared by _ensure()
        self.crashed = {}       # target_id -> crash status, from Target.targetCrashed (#28)
        self.newtab_strikes = 0 # consecutive NewTabTimeouts; see NEWTAB_STRIKES (#29)
        self.test_stalls = 0    # PH_TEST_HOOKS only: newtabs to stall past NEWTAB_TIMEOUT
        self.tags = {}          # agent tag -> target_id. See _tab(): tags, never indices.
        self.jobs = {}          # prefetch job id -> {slots, total, delivered}
        self._launch = asyncio.Lock()   # serialises browser launch; see _ensure()
        self.owners = {}        # target_id -> "agent@repo:pid", shown in the tab title and /tabs
        self.fetches = 0        # tabs opened since this Chrome launched; see _maybe_recycle()
        self.cdp_endpoint = {}  # {host, port, http} of the current Chrome; see _publish_cdp()
        self._jobn = 0          # prefetch job counter
        self._bg = set()        # background tasks, held so the loop cannot collect them mid-run
        threading.Thread(target=lambda:(asyncio.set_event_loop(self.loop),self.loop.run_forever()),
                         daemon=True).start()
    def run(self, coro): return asyncio.run_coroutine_threadsafe(coro, self.loop).result()
    def run_timeout(self, coro, timeout):
        """run(), bounded: diagnostics must answer while the loop is busy. Raises TimeoutError; the
        coroutine finishes on its own."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=timeout)
    def spawn(self, coro):
        """Fire-and-forget on the loop, with a reference held until it finishes."""
        task = asyncio.ensure_future(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return task
    def gone(self):
        """Has the Chrome we launched exited? Reads the process returncode; no CDP round trip."""
        if self.browser is None: return True
        try: return bool(self.browser.stopped)
        except Exception: return True   # can't tell -> assume gone; a spare relaunch beats a wedge
    def ok(self):
        """Usable browser: launched, not marked dead by a failed op, process still running."""
        return self.tab is not None and not self._dead and not self.gone()
    async def probe(self, timeout=1.0):
        """True/False from one Browser.getVersion round trip; None if it took over `timeout`.
        - A live process is not a reachable browser (#6), so health needs real traffic.
        - A local round trip is ms; 1 s means busy with a page, so expiry says "can't tell"."""
        if not self.ok(): return False
        try:
            # Through the BROWSER session: health must not depend on one tab existing (#22).
            await asyncio.wait_for(self.browser.send("Browser.getVersion"), timeout)
            return True
        except TimeoutError:
            return None
        except Exception as e:
            log("probe_failed", e=repr(e)[:100])
            return False
    def owner_of(self, t):
        """The `agent@repo:pid` that opened this tab, or None if it was opened without a label."""
        return self.owners.get(getattr(t, "target_id", None))
    def owner_alive(self, owner):
        """Is the pid in an `agent@repo:pid` label alive? No pid -> assumed alive (closeextra must
        never close a live agent's tab on a guess)."""
        if not owner or ":" not in owner:
            return True
        try:
            pid = int(owner.rsplit(":", 1)[1])
        except ValueError:
            return True
        try:
            os.kill(pid, 0)          # signal 0: existence check, sends nothing
        except ProcessLookupError:
            return False
        except PermissionError:
            return True              # exists, owned by another user
        return True
    def chrome_pid(self):
        """PID of our Chrome, or None. Tests kill exactly this one, never a pgrep match."""
        p = self.browser.process if self.browser is not None else None
        return getattr(p, "pid", None)
    def forget(self, why):
        """Mark the connection dead but KEEP the handle: _ensure() uses it to tell "Chrome exited"
        (#8, relaunch) from "only the socket died" (#6, reattach). Tags and owners go now: they are
        target ids, and a stale one resolving in a new browser would hand an agent another's page."""
        log("browser_lost", why=str(why)[:100])
        self._dead = True
        self.tab = None
        self.tags.clear(); self.owners.clear()
    async def _reattach(self):
        """Same Chrome, new connection; True if it worked. Preferred over a relaunch, which loses the
        cleared Turnstile session and every login."""
        info = self.cdp_endpoint
        if not info.get("host"): return False
        old = self.browser
        try:
            new = await cdp.attach(info["host"], info["port"])
            # An attached handle owns no process, so gone() read True and the next op launched a
            # second Chrome, orphaning this one (#33). Carry ours over.
            if old is not None and new.process is None:
                new.process, new.user_data_dir = old.process, old.user_data_dir
                new._temp_profile = old._temp_profile
            self.browser = new
            self.tab = await self._first_tab()
            await self._watch_crashes()
            self._dead = False
            log("nd_reattached", cdp=f"{info['host']}:{info['port']}")
            if old is not None:
                self.spawn(old.aclose())
            return True
        except Exception as e:
            log("reattach_failed", e=repr(e)[:100])
            return False
    async def _retire(self):
        """Close a still-running browser we are about to replace, so it is not orphaned."""
        try:
            if not self.gone(): await self.browser.close()
        except Exception as e:
            log("retire_err", e=repr(e)[:80])
    async def _ensure(self):
        """A usable browser, launched once however many callers arrive together.
        - Lock + re-check: concurrent first ops each launched a Chrome without it.
        - Dead connection, live process: reattach (keeps the session). Dead process: relaunch
          (#6, #7, #8)."""
        if self.ok(): return
        async with self._launch:
            if self.ok(): return          # someone else fixed it while we waited for the lock
            if self.browser is not None:
                if not self.gone() and await self._reattach(): return
                if self.gone():
                    # Relaunching used to leave no trace of WHY Chrome went; the exit code is it (#35).
                    p = self.browser.process
                    log("browser_exited", pid=getattr(p, "pid", None),
                        returncode=getattr(p, "returncode", None))
                await self._retire()      # still running but unreachable: stop it, don't strand it
            self.browser = self.tab = None
            self._dead = False
            heal_profile(PROFILE_DIR)   # a stale lock would hang the launch below forever
            if not HEADLESS:
                adopted = ensure_display()
                if adopted: log("display_adopted", **adopted)
            kills = memcap.oom_kills()
            try:
                # On Xvfb, X11 explicitly: Chrome picks Wayland from the session environment.
                args = ["--ozone-platform=x11"] if XVFB is not None else None
                self.browser = await cdp.launch(headless=HEADLESS, user_data_dir=PROFILE_DIR,
                                                extra_args=args, log_path=CHROME_LOG)
            except Exception as e:
                # Under the cap the usual cause is the kernel killing Chrome's processes, which
                # memory.events records (#28).
                after = memcap.oom_kills()
                if kills is not None and after is not None and after > kills:
                    raise RuntimeError(
                        f"Chrome could not start: the kernel OOM-killed {after - kills} of its "
                        f"processes under the engine's {MEMORY_CAP} memory cap. Raise PH_MEMORY_MAX "
                        f"(it is set when the engine starts).") from e
                raise
            self.tab = await self._first_tab()
            self._publish_cdp()
            self.crashed.clear()
            await self._watch_crashes()
            log("nd_started", cdp=f"{self.browser.host}:{self.browser.port}")
    async def _first_tab(self):
        """The first page Chrome lists, or a new about:blank when every tab is gone."""
        tabs = self.browser.tabs
        return tabs[0] if tabs else await self.browser.new_tab("about:blank")
    async def _start(self):
        """Launch (or relaunch) Chrome and block until it is up. Chrome is otherwise lazy: polling
        /status never starts it. connect._wake() calls this whenever /status says alive:false."""
        await self._ensure()
        return {"started": True}
    def tab_count(self):
        """Tabs the browser lists, from the event-maintained target list; no CDP round trip."""
        try: return len(self.browser.tabs) if self.browser is not None else 0
        except Exception: return 0
    def _publish_cdp(self):
        """Record the CDP endpoint (the `cdp` op, tmp/playwrong-cdp.json) so other processes can
        attach to this same browser and drive their own tabs."""
        host, port = self.browser.host, self.browser.port
        self.cdp_endpoint = {"host": host, "port": port, "http": f"http://{host}:{port}"}
        try:
            with open(os.path.join(TMP, "playwrong-cdp.json"), "w") as f:
                json.dump(self.cdp_endpoint, f)
        except OSError as e:
            log("publish_cdp_err", e=str(e)[:80])
    async def _cdp_op(self, method=None, params=None, tab=None, browser=False):
        """No method: the CDP endpoint. With one: raw passthrough on the tab's session (`tab`,
        default the active tab) or the browser's (`browser`). Request/response only; for events,
        attach a client to the endpoint."""
        await self._ensure()
        if not method:
            return self.cdp_endpoint
        target = self.browser if browser else await self._tab(tab)
        return {"result": await target.send(method, params or {})}
    async def _watch_crashes(self):
        """Renderer crashes, so an op on a killed tab fails by name instead of hanging (#28).
        Target discovery is already on (cdp.Browser keeps its tab list live with it)."""
        self.browser.on("Target.targetCrashed", self._on_crash)
    def _on_crash(self, p):
        tid, status = p.get("targetId"), p.get("status")
        self.crashed[tid] = status
        log("renderer_crashed", target=(tid or "")[:8], status=status, code=p.get("errorCode"),
            owner=self.owners.get(tid), memory_cap=MEMORY_CAP, **scope_mb())
        for t in list(self.browser.tabs if self.browser else []):
            if t.target_id == tid:
                t.fail(self._crash_error(tid))   # in-flight and later commands raise TabCrashed
    async def _crash_for(self, ref):
        """TabCrashed if the tab `ref` names crashed, else None. A dying renderer's socket error must
        not read as the browser dying; the crash event can lag it by CRASH_EVENT_WAIT."""
        tid = (self.tags.get(ref) if isinstance(ref, str) else getattr(self.tab, "target_id", None))
        t0 = time.monotonic()
        while tid and time.monotonic() - t0 < CRASH_EVENT_WAIT:
            if tid in self.crashed:
                return self._crash_error(tid)
            await asyncio.sleep(0.02)
        return None
    async def _close_tab_obj(self, t):
        """Target.closeTarget on the browser session, bounded by CLOSE_TIMEOUT (#29). Never the
        tab's own session: a crashed renderer's never answers (#28)."""
        tid = t.target_id
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(self.browser.close_target(tid), CLOSE_TIMEOUT)
        except TimeoutError:
            log("close_slow", target=tid[:8], limit_s=CLOSE_TIMEOUT,
                elapsed_s=round(time.monotonic() - t0, 2))
        except Exception as e:
            log("close_err", target=tid[:8], e=repr(e)[:80])
        self.crashed.pop(tid, None)
    def _crash_error(self, tid):
        return TabCrashed(
            f"this tab's renderer was killed ({self.crashed.get(tid)}), so its page is gone. Under "
            f"the engine's {MEMORY_CAP} memory cap that usually means the page ran out of memory; "
            f"other tabs are unaffected. Close it (close_tab) and open a new one.")
    async def _tab(self, ref=None):
        """_resolve_tab(), refusing a tab whose renderer crashed (#28)."""
        t = await self._resolve_tab(ref)
        tid = t.target_id
        if tid in self.crashed:
            raise self._crash_error(tid)
        return t
    async def _resolve_tab(self, ref=None):
        """A live Tab for `ref`: None = the active tab; "<tag>" = an agent's own tab (resolves to a
        target id, so it never drifts); int = an index (shifts when lower tabs close: avoid)."""
        await self._ensure()
        if ref is None: return self.tab
        await self._refresh()
        want = self.tags.get(ref, ref) if isinstance(ref, str) else None
        for i, t in enumerate(self.browser.tabs):
            if isinstance(ref, int) and i == ref: return t
            if want and t.target_id == want: return t
        # A known tag whose tab is gone (closed, swept, crashed): re-open under the same tag rather
        # than wedge that agent (#13, #14).
        if isinstance(ref, str) and ref in self.tags:
            owner = self.owners.pop(self.tags[ref], None)
            log("tag_stale", tag=ref[:40])
            del self.tags[ref]
            return await self._newtab_raw("about:blank", ref, owner)
        raise KeyError(f"no such tab: {ref!r} (tags: {sorted(self.tags)})")
    async def _loaded(self, t, no_js=False):
        """document.readyState once "complete" or after LOAD_WAIT (#32). None when it can't be read:
        evaluate is blocked under no_js."""
        if no_js:
            return None
        t0 = time.monotonic()
        while True:
            try:
                state = await t.evaluate("document.readyState", isolated=True)
            except Exception:
                return None
            if state == "complete" or time.monotonic() - t0 >= LOAD_WAIT:
                if time.monotonic() - t0 > 0.5:
                    log("load_wait", state=state, s=round(time.monotonic() - t0, 1))
                return state
            await asyncio.sleep(0.1)
    async def _goto(self, url, tab=None, no_js=False):
        t = await self._tab(tab)
        try:
            await t.send("Emulation.setScriptExecutionDisabled", {"value": bool(no_js)})
        except Exception:
            pass
        await t.navigate(url); await asyncio.sleep(SETTLE)
        state = await self._loaded(t, no_js)
        if not no_js:
            await self._label(t)
            try:
                title = strip_owner(await t.evaluate("document.title", isolated=True))
            except Exception:
                title = await self._title_from_html(t)
        else:
            title = await self._title_from_html(t)
        # location.href, not the url asked for: redirects and interstitials make them differ.
        return {"title": title, "url": await self._href(t), "requested": url,
                "ready_state": state}

    async def _title_from_html(self, t):
        try:
            return strip_owner(connect.title_of(await t.content()))
        except Exception:
            return ""
    async def _label(self, t):
        """Prefix this tab's title with its owner. Best effort: an SPA rewrites its title at will,
        and a failed label must never fail the navigation."""
        owner = self.owners.get(t.target_id)
        if not owner:
            return
        try:
            await t.evaluate(
                # \\s: a JS regex escape inside a Python string (#9).
                "(()=>{const m=%r,o=%r;const t=document.title.replace(/^\u27e6[^\u27e7]*\u27e7\\s*/,'');"
                "document.title=m+o+'\u27e7 '+t;return 1})()" % (OWNER_L, owner), isolated=True)
        except Exception:
            pass

    async def _href(self, t=None):
        """Authoritative current url: Tab.url comes from target events and can lag, so ask the page."""
        try: return await (t or self.tab).evaluate("location.href", isolated=True)
        except Exception: return ""
    async def _solve(self, tries=20, tab=None):
        passed, i = await self._solve_on(await self._tab(tab), tries)
        return {"passed": passed, "iter": i}
    async def _solve_on(self, t, tries):
        """Click the checkbox until the page is no longer a challenge: (passed, iterations)."""
        for i in range(tries):
            title, page = await t.evaluate("document.title", isolated=True), await t.content()
            if not connect.title_is_challenge(title, page):
                log("solved", i=i)
                return True, i
            try:
                hit = await t.find_text("verify you are human", timeout=FIND_TIMEOUT)
                if hit:
                    await hit.click(); log("clicked", i=i)
                    await asyncio.sleep(AFTER_CLICK)
            except Exception as e:
                log("click_err", e=str(e)[:50])
            await asyncio.sleep(SOLVE_PAUSE)
        return False, tries
    async def _text(self, tab=None):
        t = await self._tab(tab)
        # The html's <title> carries the label too (#16).
        return {"title":strip_owner(await t.evaluate("document.title", isolated=True)),
                "html":unlabel(await t.content(), self.owner_of(t)),
                "url": await self._href(t)}
    async def _frame(self, tab=None):
        return await (await self._tab(tab)).screenshot()
    async def _shot(self, tab=None):
        return {"b64": base64.b64encode(await self._frame(tab)).decode()}
    async def _clearcookies(self):
        await self._ensure(); await self.browser.clear_cookies(); return {"cleared":True}
    # --- CDP input ---
    async def _move(self, x, y, tab=None):
        t = await self._tab(tab)
        await t.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": float(x), "y": float(y)})
        return {"ok":1,"x":x,"y":y}
    async def _click(self, x, y, tab=None):
        await (await self._tab(tab)).click(x, y)
        return {"ok":1,"x":x,"y":y}
    async def _key(self, key, tab=None):
        tb = await self._tab(tab)
        # named keys (Enter/Tab/...) carry a code; printable chars go as text
        named = {"Enter":13,"Tab":9,"Escape":27,"Backspace":8,"ArrowDown":40,"ArrowUp":38,
                  "ArrowLeft":37,"ArrowRight":39,"PageUp":33,"PageDown":34,"Home":36,"End":35,
                  "Space":32,"Delete":46}
        if key in named:
            for ty in ("keyDown","keyUp"):
                await tb.send("Input.dispatchKeyEvent", {"type": ty, "key": key,
                                                         "windowsVirtualKeyCode": named[key]})
        else:
            await tb.send("Input.dispatchKeyEvent", {"type": "char", "text": key})
        return {"ok":1,"key":key}
    async def _maybe_recycle(self):
        """Replace Chrome past CHROME_MAX_TABS open or CHROME_MAX_FETCHES opened (#23).
        - Checked before a newtab, so the caller's tab lands in the fresh browser.
        - Other agents' tags become tombstones: their next op re-opens the tab (#14).
        - The cleared session and logins go too, unless the profile is persistent."""
        if self.browser is None or self.gone():
            return
        n = self.tab_count()
        if n < CHROME_MAX_TABS and self.fetches < CHROME_MAX_FETCHES:
            return
        log("chrome_recycle", why="tabs" if n >= CHROME_MAX_TABS else "fetches", tabs=n,
            fetches=self.fetches, max_tabs=CHROME_MAX_TABS, max_fetches=CHROME_MAX_FETCHES)
        await self._retire()
        self.browser = self.tab = None
        self._dead = False
        self.fetches = 0
        for tag in self.tags: self.tags[tag] = None      # tombstones: re-openable, see _tab()
        self.owners.clear()
        await self._ensure()
    async def _repoint(self, why):
        """The active tab vanished but Chrome is fine (#22): make the first untagged page (or a new
        about:blank) active. Never forget()s the browser."""
        await self._refresh()
        tagged = {v for v in self.tags.values() if v}
        for t in self.browser.tabs:
            if t.target_id not in tagged:
                self.tab = t
                break
        else:
            self.tab = await self.browser.new_tab("about:blank")
            await self._refresh()
        log("tab_repointed", why=why, target=self.tab.target_id)
    async def _tab_heaps(self):
        """Per-tab JS heap + array-buffer MB for the mem log (#28), queried concurrently."""
        if self.browser is None:
            return []
        async def heap_mb(t):
            if not t.attached:
                return "unconnected"          # never driven: attaching just to measure is waste
            try:
                h = await asyncio.wait_for(t.send("Runtime.getHeapUsage"), HEAP_QUERY_TIMEOUT)
                return int(h["totalSize"] + h.get("backingStorageSize", 0)) >> 20
            except Exception:
                return None                   # busy, crashed, or not a page; logged as such
        async def one(t):
            return {"target": t.target_id[:8], "owner": self.owners.get(t.target_id),
                    "heap_mb": await heap_mb(t), "url": (t.url or "")[:60]}
        return await asyncio.gather(*(one(t) for t in list(self.browser.tabs)))
    async def _open_target(self, url):
        """Target.createTarget, bounded by NEWTAB_TIMEOUT (#29). A late answer still opens a tab,
        so that tab is closed when it lands rather than left untagged."""
        async def get():
            if self.test_stalls > 0:
                self.test_stalls -= 1
                await asyncio.sleep(NEWTAB_TIMEOUT * 4)
            try:
                return await self.browser.new_tab(url)
            except Exception as e:
                if "no browser is open" not in str(e):
                    raise
                # Every window closed but Chrome still up (headless always; headed while it shuts
                # down): a tab needs a window to go in, so open one (#35).
                log("no_window", action="new_window")
                return await self.browser.new_tab(url, new_window=True)
        task = asyncio.ensure_future(get())
        t0 = time.monotonic()
        try:
            t = await asyncio.wait_for(asyncio.shield(task), NEWTAB_TIMEOUT)
        except TimeoutError:
            def reap(f):
                if not f.cancelled() and f.exception() is None:
                    self.spawn(self._close_tab_obj(f.result()))
            task.add_done_callback(reap)
            self.newtab_strikes += 1
            log("newtab_timeout", limit_s=NEWTAB_TIMEOUT, elapsed_s=round(time.monotonic() - t0, 2),
                strikes=self.newtab_strikes)
            if self.newtab_strikes >= NEWTAB_STRIKES:
                self.newtab_strikes = 0
                self.forget(f"newtab unanswered {NEWTAB_STRIKES}x in a row")   # next op reattaches
            raise NewTabTimeout(
                f"Chrome did not open a tab within {NEWTAB_TIMEOUT}s. "
                f"Retry: after {NEWTAB_STRIKES} in a row the engine reconnects to the same Chrome."
            ) from None
        self.newtab_strikes = 0
        return t
    async def _newtab_raw(self, url="about:blank", tag=None, owner=None):
        """Open a tab, register its tag/owner, return the Tab (for _tab() to hand straight back)."""
        await self._ensure()
        check_headroom("a new tab")
        await self._maybe_recycle()
        self.fetches += 1
        t = await self._open_target(url)
        if not tag:
            self.tab = t                # only untagged tabs become active (#22)
        tid = t.target_id
        if tag:
            self.tags[tag] = tid
        if owner:
            self.owners[tid] = owner
        return t

    async def _newtab(self, url="about:blank", tag=None, owner=None):
        """Open a tab; a `tag` makes it yours: pass it as `tab` on later ops."""
        t = await self._newtab_raw(url, tag, owner)
        return {"ok":1,"url":url,"index":self._tab_index(t),"tag":tag,"owner":owner,
                "target_id":t.target_id}
    # --- tab management: agents shard the one browser by tab and close what they open ---
    def _tab_index(self, tab):
        return next((i for i, t in enumerate(self.browser.tabs) if t is tab), -1)
    async def _refresh(self):
        """Re-read the tab list from Chrome. Target events keep it live, but an answer that callers
        rely on (closetab's `remaining`) should come from Chrome itself."""
        try: await self.browser.refresh()
        except Exception as e: log("refresh_err", e=str(e)[:60])
    async def _tabs(self):
        """Every open tab: index, url, title, active, tag, owner, target_id."""
        await self._ensure(); await self._refresh()
        by_id = {v: k for k, v in self.tags.items() if v}   # skip tombstones: None is not an id
        out=[]
        for i, t in enumerate(self.browser.tabs):
            # url/title from the TargetInfo: works for background tabs, no round trip per tab.
            out.append({"index":i,"url":t.url or "","title":strip_owner(t.title or ""),
                        "active":(t is self.tab),"tag":by_id.get(t.target_id),
                        "owner":self.owners.get(t.target_id),"target_id":t.target_id})
        return {"tabs":out,"count":len(out)}
    def _forget_tags(self, ids):
        """Tombstone (tag -> None) the tags of closed tabs and drop their owners. A tombstone is
        re-opened on use (#14), unlike an unknown tag; the oldest beyond 128 are dropped."""
        ids = {i for i in ids if i}
        for tag, tid in list(self.tags.items()):
            if tid in ids:
                self.tags[tag] = None
        for tid in ids:
            self.owners.pop(tid, None)
        dead = [t for t, v in self.tags.items() if v is None]
        for tag in dead[:max(0, len(dead) - 128)]:      # dicts keep insertion order: oldest first
            del self.tags[tag]

    async def _await_closed(self, ids):
        """Wait until Chrome has torn the closed targets down (closeTarget acks first), so
        `remaining` is true. Teardown takes ms; past CLOSE_SETTLE the tab is reported still open."""
        if not ids: return
        deadline = time.monotonic() + CLOSE_SETTLE
        while time.monotonic() < deadline:
            await self._refresh()
            live = {t.target_id for t in self.browser.tabs}
            if not (ids & live): return
            await asyncio.sleep(0.05)
        log("close_timeout", ids=",".join(str(i) for i in ids))
    async def _closetab(self, index=None, url=None, keep_first=True, tag=None):
        """Close by tag (preferred: a target id never shifts), index or url substring. Never the
        last tab; keep_first protects the base tab 0."""
        await self._ensure(); await self._refresh()
        tabs=list(self.browser.tabs)
        if len(tabs)<=1: return {"closed":0,"reason":"only one tab; refusing to close the last"}
        want_id = self.tags.get(tag) if tag else None
        targets=[]
        for i, t in enumerate(tabs):
            if keep_first and i==0: continue
            if want_id is not None:
                if t.target_id == want_id:
                    targets.append((i,t))
                continue
            if index is not None and i==index: targets.append((i,t))
            elif url is not None and url in (t.url or ""): targets.append((i,t))
        closed=0; ids=set()
        for i, t in targets:
            try:
                await self._close_tab_obj(t)
                closed+=1; ids.add(t.target_id)
            except Exception as e:
                log("closetab_err",i=i,e=str(e)[:80])
        await self._await_closed(ids)  # so "remaining" is the truth, not a mid-teardown snapshot
        self._forget_tags(ids)         # by target id: any tag pointing at a closed tab (#14)
        if self.tab not in self.browser.tabs:
            await self._repoint("closetab")     # not tabs[0]: that may be another agent's tab
        return {"closed":closed,"remaining":len(self.browser.tabs)}
    async def _closeextra(self, owner=None, force=False):
        """Close leaked tabs except the base tab (0), never a live agent's (#13, #15).
        - Closes: tabs owned by `owner` (or unlabelled ones when no owner), and tabs whose owner
          process has exited. `force` closes everything.
        - Skipped tabs are reported with their owner."""
        await self._ensure(); await self._refresh()
        n=0; ids=set(); skipped=[]
        for t in list(self.browser.tabs)[1:]:
            own = self.owner_of(t)
            mine = (own == owner) if owner else (own is None)
            if not (force or mine or not self.owner_alive(own)):
                skipped.append({"owner": own, "url": (t.url or "")[:120]})
                continue
            try:
                ids.add(t.target_id)
                await self._close_tab_obj(t); n+=1
            except Exception: pass
        await self._await_closed(ids)
        self._forget_tags(ids)   # else the owners keep handles to tabs this just destroyed (#14)
        self.tab=self.browser.tabs[0] if self.browser.tabs else None
        out={"closed":n,"remaining":len(self.browser.tabs)}
        if skipped:
            out["skipped"]=skipped
            out["note"]=(f"{len(skipped)} tab(s) left alone: they belong to other agents that are "
                         f"still running. Pass force=true only if you mean to close their pages.")
        return out
    async def _js(self, expr, tab=None):
        """Evaluate in the page; promises resolved, values returned as JSON (JS_WRAP)."""
        t = await self._tab(tab)
        try:
            # Main world on purpose: callers read page globals. Engine-internal reads are isolated (#49).
            raw = await t.evaluate(JS_WRAP % expr.strip().rstrip(";"), await_promise=True)
        except cdp.JSError as e:
            # The wrapper catches runtime throws, so this is a syntax error in the expression.
            return {"error": f"js could not be evaluated: {str(e)[:300]}"}
        if not isinstance(raw, str):
            return {"error": f"js could not be evaluated: {str(raw)[:300]}"}
        owner = self.owner_of(t)
        try:
            val = json.loads(raw)
        except ValueError:
            return {"result": unlabel(raw, owner)}
        if isinstance(val, dict) and "__playwrong_error" in val:
            return {"error": f"js exception: {val['__playwrong_error']}"}
        return {"result": unlabel(val, owner)}
    # ── prefetch: load many urls in parallel tabs; collect each as it lands ─────────────────────
    async def _prefetch(self, urls, concurrency=8, solve=True, tries=20, timeout=30, owner=None):
        """Start a batch; returns a job id immediately."""
        await self._ensure()
        self._jobn += 1
        job = f"job{self._jobn}"
        urls = [u for u in (urls or []) if u]
        # Slots keyed by index, not url: a batch may repeat a url.
        self.jobs[job] = {"slots": {}, "total": len(urls), "delivered": 0, "t": time.monotonic()}
        self._reap_jobs()
        self.spawn(self._batch(job, urls, concurrency, solve, tries, timeout, owner))
        return {"job": job, "count": len(urls), "concurrency": concurrency, "timeout": timeout}

    def _reap_jobs(self):
        """Keep the last 20 drained jobs (so a poll answers "nothing left", not "no such job"); drop
        jobs untouched for JOB_IDLE (a client that died mid-batch never collects, #23)."""
        done = [j for j, d in self.jobs.items() if d.get("total", 0) <= d.get("delivered", 0)
                and not d.get("slots")]
        for j in done[:-20]:
            self.jobs.pop(j, None)
        now = time.monotonic()
        for j in [j for j, d in self.jobs.items() if now - d.get("t", now) > JOB_IDLE]:
            log("job_reaped", job=j, slots=len(self.jobs[j].get("slots", {})))
            self.jobs.pop(j, None)

    async def _batch(self, job, urls, concurrency, solve, tries, timeout=30, owner=None):
        """Load every url, `concurrency` at a time, each in its own tab.
        - Per-url deadline: one wedged page costs one slot, never the batch.
        - `timeout` covers the load AND any Turnstile solve (10-30 s): raise it for walled sites."""
        sem = asyncio.Semaphore(max(1, int(concurrency)))
        slots = self.jobs[job]["slots"]

        async def load(i, url, tabs):
            check_headroom("a prefetch tab")
            t = await self.browser.new_tab("about:blank")
            tabs[i] = t                         # recorded so a timeout can still close it
            if owner:
                self.owners[t.target_id] = owner   # a batch tab is short-lived but still someone's
            await t.navigate(url)
            await self._label(t)
            await asyncio.sleep(SETTLE)
            state = await self._loaded(t)
            title = await t.evaluate("document.title", isolated=True)
            html = await t.content()
            passed = None                       # None = never challenged
            if solve and connect.title_is_challenge(title, html):
                passed, _ = await self._solve_on(t, tries)
                title = await t.evaluate("document.title", isolated=True)
                html = await t.content()
            try: href = await t.evaluate("location.href", isolated=True)
            except Exception: href = url
            slots[i] = {"status": "ready", "title": strip_owner(title), "html": html, "url": href,
                        "requested": url, "challenge": passed}
            if state not in (None, "complete"):
                slots[i]["partial"] = True

        async def one(i, url):
            async with sem:                     # at most `concurrency` tabs open at once
                slots[i] = {"status": "loading", "requested": url}
                tabs = {}
                try:
                    await asyncio.wait_for(load(i, url, tabs), timeout=timeout)
                except TimeoutError:
                    # A load that never "finishes" usually still rendered: keep what is there.
                    slots[i] = await self._partial(tabs.get(i), url, timeout)
                    log("prefetch_timeout", url=url[:60], salvaged=slots[i]["status"])
                except Exception as e:
                    slots[i] = {"status": "error", "requested": url, "error": repr(e)[:200]}
                    log("prefetch_err", url=url[:60], e=repr(e)[:80])
                finally:
                    t = tabs.get(i)
                    if t is not None:
                        try: await self._close_tab_obj(t)
                        except Exception: pass

        await asyncio.gather(*(one(i, u) for i, u in enumerate(urls)), return_exceptions=True)
        await self._refresh()
        log("prefetch_done", job=job, n=len(urls))

    async def _partial(self, t, url, timeout):
        """What a timed-out tab rendered, flagged partial; an error when there is no real page."""
        if t is not None:
            try:
                title = await asyncio.wait_for(t.evaluate("document.title", isolated=True),
                                               PARTIAL_READ_TIMEOUT)
                html = await asyncio.wait_for(t.content(), PARTIAL_READ_TIMEOUT)
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
            if d: d["t"] = time.monotonic()      # polled = someone still wants it
            return summarise(job, d) if d else {"error": f"no such job {job!r}"}
        return {"jobs": [summarise(j, d) for j, d in self.jobs.items()]}

    async def _collect(self, job, drain=True):
        """Every finished result; drained by default (pages are MBs of HTML). The job record stays,
        so a later call answers "nothing left"."""
        d = self.jobs.get(job)
        if not d:
            return {"error": f"no such job {job!r}"}
        d["t"] = time.monotonic()
        done = {i: v for i, v in d["slots"].items() if v.get("status") in ("ready", "error")}
        if drain:
            for i in done:
                d["slots"].pop(i, None)
            d["delivered"] = d.get("delivered", 0) + len(done)
        remaining = d["total"] - d.get("delivered", 0)
        return {"job": job, "results": list(done.values()), "remaining": max(0, remaining),
                "done": remaining <= 0}

    async def _window(self, bounds=None, tab=None):
        """The tab's browser window: {windowId, bounds}. With `bounds` ({left,top,width,height}),
        move/resize it first; a maximised window is made normal, since Chrome refuses a size then."""
        t = await self._tab(tab)
        w = await self.browser.send("Browser.getWindowForTarget", {"targetId": t.target_id})
        if bounds:
            want = {k: int(v) for k, v in bounds.items() if k in ("left", "top", "width", "height")}
            if w["bounds"].get("windowState") != "normal":
                await self.browser.send("Browser.setWindowBounds",
                                        {"windowId": w["windowId"], "bounds": {"windowState": "normal"}})
            await self.browser.send("Browser.setWindowBounds",
                                    {"windowId": w["windowId"], "bounds": want})
            w = await self.browser.send("Browser.getWindowForTarget", {"targetId": t.target_id})
        return {"windowId": w["windowId"], "bounds": w["bounds"]}
    async def _cookies(self):
        await self._ensure()
        cks = await self.browser.cookies()
        return {"cookies":[{"name":c["name"],"value":c["value"],"domain":c.get("domain")} for c in cks]}
    def do(self, op, a):
        # Driving ops take an optional `tab` (tag or index); without it they act on the active tab.
        m={"start":lambda:self._start(),
           "goto":lambda:self._goto(a["url"],a.get("tab"),a.get("no_js",False)),
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
           "closeextra":lambda:self._closeextra(a.get("owner"), a.get("force", False)),
           "cdp":lambda:self._cdp_op(a.get("method"),a.get("params"),a.get("tab"),
                                     a.get("browser",False)),
           "window":lambda:self._window(a.get("bounds"),a.get("tab")),
           "prefetch":lambda:self._prefetch(a.get("urls"),a.get("concurrency",8),
                                            a.get("solve",True),a.get("tries",20),
                                            a.get("timeout",30),a.get("owner")),
           "jobs":lambda:self._jobstatus(a.get("job")),
           "collect":lambda:self._collect(a.get("job"),a.get("drain",True))}
        if op not in m: return {"error":f"unknown {op}"}
        # Retried once, only for a dropped browser connection (the op did nothing; attempt 2
        # relaunches) or a lost active tab (#22). Anything else returns on the first attempt.
        for attempt in (1, 2):
            try: return self.run(m[op]())
            except Exception as e:
                dead = _dead_conn(e)
                if dead:
                    crash = self.run(self._crash_for(a.get("tab")))
                    if crash is not None:
                        dead, e = False, crash
                log("op_err",op=op,e=repr(e)[:120],dead_conn=dead,attempt=attempt,
                    where="|".join(traceback.format_exc().strip().splitlines()[-3:])[:300])
                if dead and attempt == 1:
                    self.forget(f"{op}: {type(e).__name__}")
                    continue
                if attempt == 1 and a.get("tab") is None and _lost_target(e):
                    try:
                        self.run(self._repoint(f"{op}: {str(e)[:60]}"))
                        continue
                    except Exception as e2:
                        log("repoint_failed", op=op, e=repr(e2)[:120])
                if dead:
                    return {"error": f"browser connection lost and the relaunch did not take: "
                                     f"{e!r}"[:200]}
                return {"error":repr(e)[:600]}      # 600: a whole multi-line launch error (#21)

B = ND()

# Graceful Chrome stop on shutdown: Browser.close, then SIGTERM, then SIGKILL, each STOP_TIMEOUT
# (cdp.py), plus margin. On expiry we exit anyway; scripts/cleanup_orphans.py collects the rest.
SHUTDOWN_TIMEOUT = 3 * cdp.STOP_TIMEOUT + 2

def _shutdown(code=0):
    """Stop the browser, THEN exit: exiting first left Chrome running with nothing driving it."""
    try:
        if B.browser is not None:
            fut = asyncio.run_coroutine_threadsafe(_stop_browser(), B.loop)
            fut.result(timeout=SHUTDOWN_TIMEOUT)
            log("browser_stopped")
    except Exception as e:
        log("shutdown_err", e=repr(e)[:120])
    if XVFB is not None:
        XVFB.terminate()          # after Chrome: it is Chrome's display
    os._exit(code)


async def _stop_browser():
    try:
        await B.browser.close()   # graceful (flushes the profile), then the process group goes
    except Exception:
        pass


class H(BaseHTTPRequestHandler):
    def _j(self,o,c=200):
        # default=str: one unserialisable value must never kill the response mid-flight.
        b=json.dumps(o,default=str).encode();self.send_response(c)
        self.send_header("Content-Type","application/json");self.send_header("Content-Length",str(len(b)))
        self.end_headers();self.wfile.write(b)
    def _raw(self,b,ct,c=200):
        self.send_response(c);self.send_header("Content-Type",ct)
        self.send_header("Content-Length",str(len(b)));self.end_headers();self.wfile.write(b)
    def log_message(self,*a):pass
    def do_GET(self):
        if self.path=="/status":
            # alive = a real round trip (#8: "launched once" read as healthy; #6: process up, socket
            # dead). None from probe = the loop is busy; then the cheap check stands.
            try: p = B.run_timeout(B.probe(), STATUS_PROBE_TIMEOUT)
            except Exception: p = None
            self._j({"server":True,"alive":B.ok() if p is None else bool(p),
                     "launched":B.tab is not None,"chrome_pid":B.chrome_pid(),
                     "code":CODE, **mem_snapshot(),
                     "memory_cap":MEMORY_CAP,"rss_limit_mb":RSS_LIMIT >> 20,"headless":HEADLESS,
                     "display":os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"),
                     "display_mode":DISPLAY_MODE or "desktop"})
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
            self._j({"error":f"invalid JSON body: {e}"},400); return
        if not isinstance(a, dict):
            self._j({"error":"body must be a JSON object"},400); return
        op=self.path.strip("/")
        if op=="shutdown":self._j({"ok":1});threading.Thread(target=_shutdown).start();return
        if op=="_test_stall_newtab" and os.environ.get("PH_TEST_HOOKS")=="1":
            B.test_stalls = int(a.get("n", 1)); self._j({"ok":1}); return
        if op=="_test_forget" and os.environ.get("PH_TEST_HOOKS")=="1":
            # recovery_test.py only: the websocket-dead-process-alive state (#6) nothing else can make.
            B.loop.call_soon_threadsafe(B.forget, "test hook"); B.run(asyncio.sleep(0))
            self._j({"ok":1}); return
        with _INFLIGHT_LOCK: _INFLIGHT[0]+=1
        try:
            t0=time.monotonic(); before=proc_status_mb("VmRSS"); r=B.do(op,a)
            after=check_rss(op)             # the self-restart trigger, applied after every op
            # One line per op with RSS before and after: the leaking op shows up as the delta (#23).
            log("op_done",op=op,ms=int((time.monotonic()-t0)*1000),rss_before_mb=before,
                rss_after_mb=after,delta_mb=(after or 0)-(before or 0),
                err=isinstance(r,dict) and "error" in r)
            self._j(r)
        finally:
            with _INFLIGHT_LOCK: _INFLIGHT[0]-=1

def _already_serving(port):
    """Is an engine already answering on this port? Then this process must not start a second one
    (backs up connect.ensure()'s spawn lock against anything else starting server.py)."""
    return connect.reachable(port, timeout=STATUS_PROBE_TIMEOUT)


if __name__=="__main__":
    if _already_serving(PORT):
        log("server_duplicate_exit", port=PORT)
        print(f"an engine is already serving 127.0.0.1:{PORT} — not starting a second one",
              file=sys.stderr)
        sys.exit(0)
    # May re-exec this process under a capped systemd scope; see engine/memcap.py (#23).
    MEMORY_CAP = memcap.enter("engine", MEMORY_MAX, log, oom_policy="continue")
    if DISPLAY_MODE == "xvfb":
        start_xvfb()              # before any thread: preexec_fn and fork want a single thread
    log("server_start",port=PORT,memory_cap=MEMORY_CAP,rss_limit_mb=RSS_LIMIT >> 20,headless=HEADLESS,
        chrome_max_tabs=CHROME_MAX_TABS,chrome_max_fetches=CHROME_MAX_FETCHES)
    threading.Thread(target=_memwatch, daemon=True, name="memwatch").start()
    SRV = ThreadingHTTPServer(("127.0.0.1",PORT),H)
    SRV.serve_forever()
    # Only _retire_engine() ends serve_forever(); its worker does the exit once in-flight ops drain.
    threading.Event().wait()
