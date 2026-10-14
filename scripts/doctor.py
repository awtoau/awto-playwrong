"""doctor.py — preflight for a fresh playwrong install: tells you exactly what is missing and the
one command that fixes it.

Run this FIRST on a new machine (and whenever the engine "hangs" or the MCP server reports a dead
engine):

    python scripts/doctor.py

Every check prints PASS / WARN / FAIL plus, on failure, the literal command to fix it. Exit code is
0 when nothing FAILed (WARNs are survivable), 1 otherwise. Output also lands in tmp/logs/doctor.log.
"""
import importlib.util
import json
import os
import platform
import py_compile
import socket
import sys
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGDIR = os.path.join(REPO, "tmp", "logs")
PORT = int(os.environ.get("PH_PORT", "8731"))
# See check_engine_op(): 20s separates "busy" from "broken", it is not a hang budget.
OP_BUDGET = 20

# Everything else the engine + MCP server touch is stdlib: this is the entire dependency surface.
DEPS = [("websockets", "websockets", "CDP transport")]

sys.path.insert(0, os.path.join(REPO, "engine"))
import cdp  # noqa: E402  (stdlib-only at import; the engine's own Chrome lookup)

RESULTS = []


def check(name, status, detail, fix=None):
    RESULTS.append((name, status, detail, fix))
    mark = {"PASS": "PASS", "WARN": "WARN", "FAIL": "FAIL"}[status]
    print(f"[{mark}] {name}: {detail}")
    if fix and status != "PASS":
        print(f"       fix: {fix}")


def check_python():
    """3.14+ required. Free-threaded recommended: the engine runs an HTTP thread per request beside
    its asyncio loop, and crawls parse pages in parallel; a GIL build works, serialised."""
    where = f"{platform.python_implementation()} {platform.python_version()} at {sys.executable}"
    if sys.version_info < (3, 14):
        check("python", "FAIL", f"{where} — needs Python 3.14+", "install Python 3.14+ (3.14t recommended)")
    elif sys._is_gil_enabled():
        check("python", "WARN", f"{where} — works; a free-threaded build (3.14t) is recommended")
    else:
        check("python", "PASS", f"{where} (free-threaded)")


def check_deps():
    """One check, not one per package: three FAIL lines for a single `pip install` is noise, and the
    whole point of this script is that each failure maps to exactly one command you can run."""
    missing = [pkg for mod, pkg, _ in DEPS if importlib.util.find_spec(mod) is None]
    if missing:
        check("dependencies", "FAIL", f"missing: {', '.join(missing)}",
              f"{sys.executable} -m pip install -r {os.path.join(REPO, 'requirements-engine.txt')}")
    else:
        check("dependencies", "PASS", ", ".join(pkg for _, pkg, _ in DEPS) + " importable")
    return not missing


def check_chrome():
    """The engine's own lookup (engine/cdp.py), so this cannot pass while the launch would fail."""
    try:
        check("chrome", "PASS", cdp.find_chrome())
    except cdp.CDPLaunchError:
        fix = ("install Google Chrome or Chromium (e.g. dnf install google-chrome-stable, "
               "apt install chromium-browser)" if platform.system() not in ("Darwin", "Windows")
               else "install Google Chrome from google.com/chrome")
        check("chrome", "FAIL", f"no browser found (looked for: "
              f"{', '.join(cdp.chrome_candidates()[:3])}…); or set PH_CHROME", fix)


def check_display():
    """The engine runs Chrome HEADED on purpose — headless is a Turnstile tell. On Linux that needs
    a display; without one, Chrome launches and immediately dies."""
    if platform.system() != "Linux":
        return
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        check("display", "PASS", os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    else:
        check("display", "FAIL", "no DISPLAY/WAYLAND_DISPLAY — headed Chrome cannot start",
              "run on a desktop session, or start a virtual display (Xvfb :99 & export DISPLAY=:99)")


def check_tmp():
    d = os.path.join(REPO, "tmp", "logs")
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".doctor-probe")
        open(probe, "w").write("ok")
        os.remove(probe)
        check("tmp/ writable", "PASS", d)
    except OSError as e:
        check("tmp/ writable", "FAIL", f"{d}: {e}", "check permissions on the checkout")


def check_mcp():
    f = os.path.join(REPO, "engine", "mcp_server.py")
    if not os.path.exists(f):
        check("mcp server", "FAIL", f"{f} missing", "re-clone the repo")
        return
    try:
        py_compile.compile(f, doraise=True, cfile=os.path.join(LOGDIR, "mcp-server-check.pyc"))
        check("mcp server", "PASS", f"{f} compiles (stdlib only — no install needed)")
    except py_compile.PyCompileError as e:
        check("mcp server", "FAIL", str(e).splitlines()[-1], "file is corrupt; re-clone")


def engine_status():
    try:
        return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/status", timeout=3).read())
    except Exception:
        return None


def git(*args):
    import subprocess
    try:
        r = subprocess.run(["git", "-C", REPO, *args], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def check_engine_code(st):
    """Is the RUNNING engine the code in this checkout?

    A long-lived engine serves whatever it imported at startup. Nothing used to say so, so a
    checkout could sit commits ahead of the process on the port while every check reported healthy —
    the fixes under test simply were not in the running code. This is the check that answers it.
    """
    code = (st or {}).get("code") or {}
    running, head = code.get("sha"), git("rev-parse", "--short", "HEAD")
    if not running:
        check("engine code", "WARN", "engine did not report a source version",
              "restart it: playwrong --stop (the next command relaunches)")
        return
    if not head:
        check("engine code", "PASS", f"running {running} (not a git checkout to compare against)")
        return
    if running == head:
        detail = f"running {running}, matches HEAD"
        if code.get("dirty"):
            detail += " — with uncommitted changes at launch"
        check("engine code", "PASS", detail)
        return
    behind = git("rev-list", "--count", f"{running}..{head}")
    n = f"{behind} commit(s)" if behind else "an unknown number of commits"
    check("engine code", "WARN", f"running {running}, checkout is at {head} — {n} newer",
          "playwrong --stop   (the next command relaunches on current code)")


def check_engine_op(st):
    """Drive a real op, not a liveness flag.

    A diagnostic that only asks 'are you alive?' certified a dead engine as healthy for three days
    (#8), and its successor could still pass while every op failed. So: open a tab, read it, close
    it — the same path every tool takes.
    """
    if not st or not st.get("alive"):
        check("engine op", "WARN", "browser not up, so no live op was attempted",
              "any tool call launches it; or: playwrong https://example.com")
        return
    # 20s per request. A newtab is ~100ms on an idle engine; ops are serialised on one browser, so
    # under a running crawl (24 tabs, page loads allowed 90s each) this probe legitimately queues.
    # Expiry therefore means BUSY, not broken — reported as WARN, because a doctor that cries wolf
    # during every crawl gets ignored, which fails the same way as one that is optimistic.
    body = json.dumps({"url": "about:blank", "tag": "doctor-probe"}).encode()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/newtab", data=body,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=OP_BUDGET).read()
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/text",
                                     data=json.dumps({"tab": "doctor-probe"}).encode(),
                                     headers={"Content-Type": "application/json"})
        page = json.loads(urllib.request.urlopen(req, timeout=OP_BUDGET).read())
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/closetab",
                                     data=json.dumps({"tag": "doctor-probe"}).encode(),
                                     headers={"Content-Type": "application/json"})
        closed = json.loads(urllib.request.urlopen(req, timeout=OP_BUDGET).read())
        if page.get("error") or "html" not in page:
            check("engine op", "FAIL", f"newtab+text did not return a page: {str(page)[:90]}",
                  "playwrong --stop   (the next command relaunches)")
        else:
            check("engine op", "PASS",
                  f"opened a tab, read {len(page.get('html') or '')} bytes, closed "
                  f"{closed.get('closed', '?')} — the path every tool uses")
    except TimeoutError:
        check("engine op", "WARN", f"engine did not answer within {OP_BUDGET}s — it is busy, not "
              f"necessarily broken (a crawl holds the browser). Re-run when it is idle.")
    except Exception as e:
        check("engine op", "FAIL", f"a real op failed: {repr(e)[:90]}",
              "playwrong --stop   (the next command relaunches)")
    finally:
        try:                      # never leave the probe tab behind, whatever went wrong above
            req = urllib.request.Request(f"http://127.0.0.1:{PORT}/closetab",
                                         data=json.dumps({"tag": "doctor-probe"}).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5).read()
        except Exception:
            pass


def check_engine_port():
    st = engine_status()
    if st is not None:
        check(f"engine :{PORT}", "PASS",
              f"already running (chrome alive: {st.get('alive')}) — tools will reuse it")
        check_engine_code(st)
        check_engine_op(st)
        return
    s = socket.socket()
    # SO_REUSEADDR because that is what the engine's HTTPServer sets. Without it this probe fails on
    # a port merely sitting in TIME_WAIT after a recent shutdown — reporting "port is taken" and
    # sending you off to change PH_PORT when nothing is listening at all and the engine would bind
    # it fine. Test the condition the server will actually face, not a stricter one.
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", PORT))
        check(f"engine :{PORT}", "PASS", "not running; port is free (it will auto-start)")
    except OSError as e:
        check(f"engine :{PORT}", "FAIL", f"port is taken by something that is not the engine ({e})",
              "pick another port: export PH_PORT=8732")
    finally:
        s.close()


def main():
    os.makedirs(LOGDIR, exist_ok=True)
    print(f"playwrong doctor — repo {REPO}\n")
    check_python()
    check_deps()
    check_chrome()
    check_display()
    check_tmp()
    check_mcp()
    check_engine_port()

    fails = [r for r in RESULTS if r[1] == "FAIL"]
    warns = [r for r in RESULTS if r[1] == "WARN"]
    print(f"\n{len(RESULTS) - len(fails) - len(warns)} pass, {len(warns)} warn, {len(fails)} fail")
    if fails:
        print("\nfix these, then re-run:")
        for name, _, detail, fix in fails:
            print(f"  - {name}: {fix or detail}")
    else:
        print("\nready. Register the MCP server:  python scripts/install.py --register")

    with open(os.path.join(LOGDIR, "doctor.log"), "w") as f:
        json.dump([{"check": n, "status": s, "detail": d, "fix": x} for n, s, d, x in RESULTS],
                  f, indent=2)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
