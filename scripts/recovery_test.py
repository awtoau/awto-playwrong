"""recovery_test.py — kill the browser under a running engine and prove the engine comes back.

The regression test for issue #8: Chrome died, `_ensure()` kept short-circuiting on a stale
`self.tab`, and every op returned ConnectionClosedError until a human restarted the engine. /status
reported `alive: true` throughout, and doctor.py repeated it, so nothing said what was wrong. It
happened three times in two days before anyone traced it.

    python scripts/recovery_test.py                 # isolated engine on :8739, stopped after
    python scripts/recovery_test.py --port 8750     # somewhere else

ALWAYS runs against an isolated port, never the shared engine on 8731 — the test works by killing a
browser, and the shared one carries everyone's cleared Turnstile session. The pid it kills comes from
that engine's own /status, so it cannot pick the wrong Chrome.

States covered:
- the engine's current tab closed from outside while Chrome lives (#22)
- websocket dead, process alive (#6) -> `_reattach`; must keep the same Chrome, not orphan it (#33).
  Forced via the `_test_forget` op, which the engine only accepts with PH_TEST_HOOKS=1 (set here).
- a closed window (Chrome exits with its last tab), and a browser crash (SIGKILL)
- the ENGINE process itself killed (#19): later calls must say the engine is gone, not "timed out"

Results: tmp/logs/recovery-test.log
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from engine import connect  # noqa: E402

LOGDIR = os.path.join(REPO, "tmp", "logs")
LOG = os.path.join(LOGDIR, "recovery-test.log")
URL = "https://example.com"

# A relaunch is one uc.start(): measured at 0.8s (server_start -> nd_started in tmp/nd-server.log).
# 20s is 25x that, because the op that triggers it also loads a page over the network — the page,
# not the launch, is what makes this slow. On expiry the op is reported as failed with its elapsed
# time, which is the answer either way: recovery did not happen.
RELAUNCH_BUDGET = 20.0
# How long Chrome takes to die after SIGKILL. It is a local process kill; 5s is ~100x what it needs,
# and on expiry we say so rather than reporting a confusing "did not recover".
DEATH_BUDGET = 5.0

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


def status(port):
    try:
        return connect.call("status", port=port, method="GET")
    except Exception as e:
        return {"error": repr(e)[:120]}


def devtools(url):
    """Chrome's own DevTools HTTP endpoint. Local, and the only way to close a target from outside
    the engine — which is how the #6 failure is reproduced without killing the browser."""
    with urllib.request.urlopen(url, timeout=5) as r:      # localhost: 5s is already generous
        body = r.read().decode()
    try:
        return json.loads(body)
    except ValueError:
        return body


def kill_targets(port, cdp):
    """Close every page target, leaving Chrome running. The engine's tab websocket dies with them —
    process alive, socket dead, which a returncode check calls healthy."""
    base = f"http://{cdp['host']}:{cdp['port']}"
    killed = 0
    for t in devtools(f"{base}/json"):
        if t.get("type") == "page":
            devtools(f"{base}/json/close/{t['id']}")
            killed += 1
    return killed


def wait_dead(pid):
    """Block until the pid is gone, or DEATH_BUDGET passes. Returns how long it took.

    A ZOMBIE counts as dead. os.kill(pid, 0) succeeds against one — the process is finished but its
    exit status has not been collected — so waiting on that alone reported a killed engine as still
    running for the full budget. What the caller means by "dead" is "not serving", and a zombie is
    not serving.
    """
    t0 = time.monotonic()
    while time.monotonic() - t0 < DEATH_BUDGET:
        try:
            os.kill(pid, 0)
        except OSError:
            return time.monotonic() - t0
        try:
            with open(f"/proc/{pid}/stat") as f:
                if f.read().rsplit(")", 1)[1].split()[0] == "Z":
                    return time.monotonic() - t0
        except OSError:
            return time.monotonic() - t0
        time.sleep(0.05)      # a local process death, polled ~20x/s
    return None


def main():
    global _fh
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8739, help="isolated engine port (never 8731)")
    a = ap.parse_args()
    if a.port == 8731:
        sys.exit("refusing to run on 8731: this test kills the browser, and that one is shared")

    os.makedirs(LOGDIR, exist_ok=True)
    _fh = open(LOG, "w")
    os.environ["PH_TEST_HOOKS"] = "1"        # inherited by the engine this test starts (case 1c)
    say(f"recovery test — engine :{a.port}, killing its browser and expecting it back\n")

    try:
        # 1. A working engine with a live browser.
        r = connect.capture(URL, port=a.port, on_start=lambda m: say("   ", m), max_chars=200)
        ok("baseline fetch works", "Example Domain" in r["text"], f"{len(r['text'])} chars")
        st = status(a.port)
        pid = st.get("chrome_pid")
        ok("status reports a live browser and its pid", st.get("alive") is True and bool(pid),
           json.dumps(st))
        if not pid:
            say("\nno chrome_pid to kill — cannot run the rest")
            return 1

        # 1b. The engine's CURRENT tab disappears while Chrome lives on (#22): closed from outside,
        #     or a hung page's target torn down. Every untagged op then failed "Session with given
        #     id not found" forever and /status said alive:false, while the browser was fine.
        tag = "recovery-22"
        connect.call("newtab", port=a.port, url="about:blank", tag=tag)    # keeps Chrome up
        cur = [t for t in connect.call("tabs", port=a.port, method="GET")["tabs"] if t["active"]]
        cdp = connect.call("cdp", port=a.port)
        if cur:
            devtools(f"http://{cdp['host']}:{cdp['port']}/json/close/{cur[0]['target_id']}")
            time.sleep(0.5)          # Chrome tears a target down in ms; 0.5s is plenty, not a wait
        ok("current tab closed from outside", bool(cur), f"{cur[0]['target_id'] if cur else '-'}")
        st = status(a.port)
        ok("status stays alive after losing the current tab", st.get("alive") is True, json.dumps(st))
        try:
            r = connect.call("js", port=a.port, expr="1+1")
            ok("untagged op recovers onto a live tab", r.get("result") == 2, json.dumps(r))
        except connect.EngineError as e:
            ok("untagged op recovers onto a live tab", False, str(e)[:140])
        try:
            connect.call("closetab", port=a.port, tag=tag)
            r = connect.call("js", port=a.port, expr="2+2")
            ok("untagged op still works after the tagged tab closes", r.get("result") == 4,
               json.dumps(r))
        except connect.EngineError as e:
            ok("untagged op still works after the tagged tab closes", False, str(e)[:140])

        # 1c. Socket dead, process alive (#6) -> _reattach. The attached handle owned no process, so
        #     gone() read True and the NEXT op launched a second Chrome, orphaning this one (#33).
        pid = status(a.port).get("chrome_pid")
        try:
            connect.call("_test_forget", port=a.port)
            hooked = True
        except connect.EngineError as e:
            hooked = False
            ok("test hook available (engine started with PH_TEST_HOOKS=1)", False, str(e)[:140])
        if hooked:
            for i in (1, 2):                 # 1 reattaches; 2 is where the relaunch used to happen
                r = connect.capture(URL, port=a.port, max_chars=200)
                ok(f"fetch {i} after a reattach works", "Example Domain" in r["text"])
            st = status(a.port)
            ok("reattach keeps the same Chrome", st.get("chrome_pid") == pid and
               st.get("alive") is True, f"was {pid}, now {st.get('chrome_pid')}")

        # 2. Someone closes the window — the likeliest real cause. Closing every page target from
        #    the DevTools endpoint is what that does, and Chrome exits with its last tab.
        cdp = connect.call("cdp", port=a.port)
        n = kill_targets(a.port, cdp)
        ok("closing the last window ends the browser", n > 0 and wait_dead(pid) is not None,
           f"{n} target(s) closed, pid {pid} gone")
        st = status(a.port)
        ok("status sees the closed browser as dead", st.get("alive") is False, json.dumps(st))
        r = connect.capture(URL, port=a.port, max_chars=200)
        ok("fetch recovers from a closed window", "Example Domain" in r["text"])
        st = status(a.port)
        ok("no orphan left behind", wait_dead(pid) is not None and st.get("chrome_pid") != pid,
           f"old pid {pid} gone, now {st.get('chrome_pid')}")

        # 3. A crash: SIGKILL, no clean close — exactly the 'no close frame received or sent' the
        #    real failure logged. Re-read the pid; step 2 replaced the browser.
        pid = status(a.port).get("chrome_pid")
        if not pid:
            ok("browser is gone", False, "no chrome_pid after recovery")
            return 1
        os.kill(pid, signal.SIGKILL)
        took = wait_dead(pid)
        ok("browser is gone", took is not None,
           f"pid {pid} died in {took*1000:.0f}ms" if took else f"pid {pid} still alive")

        # 4. The bug: status kept saying alive against exactly this state.
        st = status(a.port)
        ok("status now reports the browser as dead", st.get("alive") is False, json.dumps(st))
        ok("status still distinguishes 'was launched'", st.get("launched") is True)

        # 5. The fix: the next op relaunches instead of failing forever.
        t0 = time.monotonic()
        try:
            r = connect.capture(URL, port=a.port, max_chars=200)
            dt = time.monotonic() - t0
            ok("next fetch recovers by itself", "Example Domain" in r["text"],
               f"{dt:.1f}s, budget {RELAUNCH_BUDGET:.0f}s")
            ok("recovery is prompt", dt < RELAUNCH_BUDGET, f"{dt:.1f}s")
        except Exception as e:
            ok("next fetch recovers by itself", False,
               f"{repr(e)[:120]} after {time.monotonic()-t0:.1f}s")

        # 6. And it is a real new browser, not the corpse.
        st = status(a.port)
        ok("status reports live again", st.get("alive") is True, json.dumps(st))
        ok("it is a different browser process", st.get("chrome_pid") not in (None, pid),
           f"was {pid}, now {st.get('chrome_pid')}")
        # 7. The ENGINE process itself dies (#19), not just its browser. Every op used to fail
        #    "engine op 'newtab' failed: timed out" for the rest of the session — an engine-level
        #    fault reported as a tab-level one, which read as permanent and sent a session off to
        #    work around the tool.
        st = status(a.port)
        epid = None
        for line in subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True,
                                   text=True).stdout.splitlines():
            # The port is in the process ENV, not its argv, so the environ check below is what
            # actually identifies it — this only narrows the candidates.
            if "engine/server.py" in line and "mcp_server" not in line:
                cand = int(line.split()[0])
                try:
                    env = open(f"/proc/{cand}/environ", "rb").read().decode(errors="replace")
                except PermissionError:
                    # The engine's python carries cap_net_bind_service, so the process is
                    # non-dumpable and /proc/<pid>/environ is closed even to its own user (#23).
                    env = subprocess.run(["sudo", "-n", "cat", f"/proc/{cand}/environ"],
                                         capture_output=True).stdout.decode(errors="replace")
                except OSError:
                    continue
                if f"PH_PORT={a.port}" in env:
                    epid = cand
        if epid:
            os.kill(epid, signal.SIGKILL)
            ok("engine process killed", wait_dead(epid) is not None, f"pid {epid}")
            t0 = time.monotonic()
            try:
                r = connect.capture(URL, port=a.port, max_chars=200)
                ok("an op after the engine died restarts it and succeeds",
                   "Example Domain" in r["text"], f"{time.monotonic()-t0:.1f}s")
            except connect.EngineError as e:
                ok("an op after the engine died restarts it and succeeds", False, repr(e)[:140])
            # A SIGKILLed engine can't close its Chrome. This test made that orphan, so it closes it;
            # every run used to leave one behind.
            orphan = st.get("chrome_pid")
            if orphan:
                try:
                    os.kill(orphan, signal.SIGTERM)
                except OSError:
                    pass
                ok("the killed engine's Chrome is closed", wait_dead(orphan) is not None,
                   f"pid {orphan}")
        else:
            ok("engine process found for the kill test", False, "no engine pid for this port")

    finally:
        try:
            connect.call("shutdown", port=a.port, timeout=15.0)
            say(f"\nengine on :{a.port} shut down")
        except Exception as e:
            say(f"\ncould not shut down :{a.port} — {repr(e)[:80]}")
        say(f"\n{len(PASS)} passed, {len(FAIL)} failed")
        say(f"log: {LOG}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
