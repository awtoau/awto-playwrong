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

Results: tmp/logs/recovery-test.log
"""
import argparse
import json
import os
import signal
import sys
import time

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


def wait_dead(pid):
    """Block until the pid is gone, or DEATH_BUDGET passes. Returns how long it took."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < DEATH_BUDGET:
        try:
            os.kill(pid, 0)
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

        # 2. Kill it the way a crash does: no clean close, exactly the 'no close frame received or
        #    sent' the real failure logged.
        os.kill(pid, signal.SIGKILL)
        took = wait_dead(pid)
        ok("browser is gone", took is not None,
           f"pid {pid} died in {took*1000:.0f}ms" if took else f"pid {pid} still alive")

        # 3. The bug: status kept saying alive against exactly this state.
        st = status(a.port)
        ok("status now reports the browser as dead", st.get("alive") is False, json.dumps(st))
        ok("status still distinguishes 'was launched'", st.get("launched") is True)

        # 4. The fix: the next op relaunches instead of failing forever.
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

        # 5. And it is a real new browser, not the corpse.
        st = status(a.port)
        ok("status reports live again", st.get("alive") is True, json.dumps(st))
        ok("it is a different browser process", st.get("chrome_pid") not in (None, pid),
           f"was {pid}, now {st.get('chrome_pid')}")
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
