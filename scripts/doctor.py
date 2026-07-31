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
import shutil
import socket
import sys
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGDIR = os.path.join(REPO, "tmp", "logs")
PORT = int(os.environ.get("PH_PORT", "8731"))

# nodriver's own runtime deps. Everything else the engine + MCP server touch is stdlib, and nodriver
# itself is vendored in-tree (vendor/nodriver), so this is the entire dependency surface.
DEPS = [("websockets", "websockets", "CDP transport"),
        ("deprecated", "Deprecated", "used by nodriver's generated CDP modules")]

# Where Chrome actually lives, per platform. nodriver finds it itself; we check so a missing browser
# is reported here instead of as a launch that silently never comes up.
CHROME = {
    "Linux": ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
              "/usr/bin/google-chrome", "/snap/bin/chromium"],
    "Darwin": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
               "/Applications/Chromium.app/Contents/MacOS/Chromium", "google-chrome"],
    "Windows": [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe", "chrome"],
}

RESULTS = []


def check(name, status, detail, fix=None):
    RESULTS.append((name, status, detail, fix))
    mark = {"PASS": "PASS", "WARN": "WARN", "FAIL": "FAIL"}[status]
    print(f"[{mark}] {name}: {detail}")
    if fix and status != "PASS":
        print(f"       fix: {fix}")


def check_python():
    v = sys.version_info
    where = f"{platform.python_implementation()} {platform.python_version()} at {sys.executable}"
    if v < (3, 11):
        check("python", "FAIL", f"{where} — the engine needs 3.11+", "install a newer Python")
    elif v < (3, 13):
        check("python", "WARN", f"{where} — engine ok; the crawl/ library wants 3.13+ (pyproject)")
    else:
        check("python", "PASS", where)


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


def check_nodriver(deps_ok):
    vend = os.path.join(REPO, "vendor", "nodriver")
    if not os.path.isdir(vend):
        check("vendored nodriver", "FAIL", f"{vend} missing",
              "re-clone the repo — vendor/nodriver is committed, not downloaded")
        return
    if not deps_ok:
        # It cannot import without those packages; reporting it as its own failure would send you
        # chasing a second problem that does not exist.
        check("vendored nodriver", "WARN", "not checked — install the dependencies above first")
        return
    sys.path.insert(0, os.path.join(REPO, "vendor"))
    try:
        import nodriver                                    # noqa: F401
        check("vendored nodriver", "PASS", f"imports from {vend}")
    except Exception as e:
        check("vendored nodriver", "FAIL", f"import failed: {e!r}",
              f"the checkout may be damaged; try: git -C {REPO} status")


def check_chrome():
    cands = CHROME.get(platform.system(), CHROME["Linux"])
    for c in cands:
        p = shutil.which(c) if not os.path.isabs(c) else (c if os.path.exists(c) else None)
        if p:
            check("chrome", "PASS", p)
            return
    fix = {"Linux": "install Google Chrome or Chromium (e.g. dnf install google-chrome-stable, "
                    "apt install chromium-browser)",
           "Darwin": "install Google Chrome from google.com/chrome",
           "Windows": "install Google Chrome from google.com/chrome"}[platform.system()
                                                                      if platform.system() in CHROME
                                                                      else "Linux"]
    check("chrome", "FAIL", f"no browser found (looked for: {', '.join(cands[:3])}…)", fix)


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


def check_engine_port():
    try:
        st = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/status", timeout=2).read())
        check(f"engine :{PORT}", "PASS",
              f"already running (chrome alive: {st.get('alive')}) — tools will reuse it")
        return
    except Exception:
        pass
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", PORT))
        check(f"engine :{PORT}", "PASS", "not running; port is free (it will auto-start)")
    except OSError:
        check(f"engine :{PORT}", "FAIL", "port is taken by something that is not the engine",
              f"pick another port: export PH_PORT=8732")
    finally:
        s.close()


def main():
    os.makedirs(LOGDIR, exist_ok=True)
    print(f"playwrong doctor — repo {REPO}\n")
    check_python()
    check_nodriver(check_deps())
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
