"""display_test.py — engine startup without a usable display fails by name, and fast.

- ensure_display() adopts the user's X/Wayland session when DISPLAY is unset, or raises "no display"
  (fake X11/run dirs under tmp/).
- An engine that dies while starting fails connect.ensure() at once with its own error (#53),
  on isolated port 8738. No browser either way.

    python scripts/display_test.py

Log: tmp/logs/display-test.log
"""
import importlib.util
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

PASS, FAIL, _fh = [], [], None


def say(*a):
    line = " ".join(str(x) for x in a)
    print(line, flush=True)
    if _fh:
        _fh.write(line + "\n"); _fh.flush()


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    say(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return cond


def load_server():
    # server.py runs as a script and imports its siblings (cdp, memcap) bare: give it engine/ too.
    sys.path.insert(0, os.path.join(REPO, "engine"))
    spec = importlib.util.spec_from_file_location("pw_server", os.path.join(REPO, "engine", "server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def engine_dies_at_startup(port=8738):
    """#53: an engine that exits while starting (here: PH_DISPLAY=xvfb, no Xvfb on PATH) must fail
    connect.ensure() at once, with the engine's own error, not after the 30 s bind wait."""
    import time

    from engine import connect
    if connect.reachable(port):
        ok("engine startup failure is reported at once", False, f"something already on :{port}")
        return
    saved = {k: os.environ.get(k) for k in ("PATH", "PH_DISPLAY")}
    os.environ["PATH"] = os.path.dirname(sys.executable)   # python only: no Xvfb, no systemd-run
    os.environ["PH_DISPLAY"] = "xvfb"
    t0 = time.monotonic()
    try:
        connect.ensure(port, want_browser=False)
        ok("engine startup failure is reported at once", False, "ensure() returned")
    except connect.EngineError as e:
        dt = time.monotonic() - t0
        ok("engine startup failure is reported at once",
           "exited during startup" in str(e) and dt < 10, f"{dt:.1f}s: {str(e)[:90]}")
        ok("...with the engine's own error", "Xvfb" in str(e), str(e)[-120:])
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def dirs(base, x11=(), run=()):
    x, r = os.path.join(base, "x11"), os.path.join(base, "run")
    os.makedirs(x); os.makedirs(r)
    for n in x11: open(os.path.join(x, n), "w").close()
    for n in run: open(os.path.join(r, n), "w").close()
    return x, r


def main():
    global _fh
    os.makedirs(os.path.join(REPO, "tmp", "logs"), exist_ok=True)
    _fh = open(os.path.join(REPO, "tmp", "logs", "display-test.log"), "w")
    ensure = load_server().ensure_display
    me = os.getuid()
    scratch = os.path.join(REPO, "tmp")

    with tempfile.TemporaryDirectory(dir=scratch) as b:
        x, r = dirs(b, x11=["X0"])
        env = {"DISPLAY": ":5"}
        ok("existing DISPLAY is left alone", ensure(env, me, x, r) == {} and env == {"DISPLAY": ":5"})

    with tempfile.TemporaryDirectory(dir=scratch) as b:
        x, r = dirs(b, x11=["X10", "X2"], run=[".mutter-Xwaylandauth.ABC123"])
        env = {}
        got = ensure(env, me, x, r)
        ok("lowest own X socket, numeric order", env.get("DISPLAY") == ":2", str(got))
        ok("mutter Xwayland auth adopted", env.get("XAUTHORITY") == os.path.join(r, ".mutter-Xwaylandauth.ABC123"))

    with tempfile.TemporaryDirectory(dir=scratch) as b:
        x, r = dirs(b, x11=["X0"], run=["wayland-0"])
        env = {}
        got = ensure(env, me + 1, x, r)          # every file is ours, so it looks foreign to uid+1
        ok("another user's X socket is ignored; wayland used", got == {"WAYLAND_DISPLAY": "wayland-0"}, str(got))

    with tempfile.TemporaryDirectory(dir=scratch) as b:
        x, r = dirs(b)
        try:
            ensure({}, me, x, r)
            ok("no display raises", False, "returned instead of raising")
        except RuntimeError as e:
            ok("no display raises, naming itself", "no display" in str(e), str(e)[:80])

    engine_dies_at_startup()
    say(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
