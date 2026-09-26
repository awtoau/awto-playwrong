"""display_test.py — prove ensure_display() adopts the desktop session, or fails naming itself.

Guards the regression where an engine spawned from a shell with no DISPLAY (ssh, remote editor,
after a reboot) failed every launch with nodriver's misleading "running as root" hint.

No browser, no real display: fake X11/run dirs under tmp/, driven with an explicit env dict.

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
    spec = importlib.util.spec_from_file_location("pw_server", os.path.join(REPO, "engine", "server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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

    say(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
