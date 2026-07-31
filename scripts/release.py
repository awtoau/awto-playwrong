"""release.py — build, PROVE the artifacts work, then optionally upload.

The first wheel this project ever built was broken: it declared `nodriver` as a dependency, so pip
installed the unpatched upstream copy, which shadowed the vendored patched one and killed the engine
on its first run with a SyntaxError. `twine check` passed the whole time — it only validates
metadata. The only thing that catches that class of bug is installing the artifact into a clean
environment and running it, which is what this script does before it will upload anything.

    python scripts/release.py                 # build + verify wheel AND sdist. No upload.
    python scripts/release.py --test-pypi     # ...then upload to TestPyPI
    python scripts/release.py --pypi          # ...then upload to PyPI (asks first)
    python scripts/release.py --from-index test-pypi
                                              # install the PUBLISHED package from an index and
                                              # smoke-test it, to check what users actually get

Credentials are never handled here: twine reads ~/.pypirc (or TWINE_* env vars) itself, so no token
passes through this script or its output.

Log: tmp/logs/release.log
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(REPO, "tmp", "dist")
LOGDIR = os.path.join(REPO, "tmp", "logs")
SMOKE_URL = "https://example.com"
# An isolated engine on its own port. The smoke test must NEVER reuse the shared default engine:
# doing so drove the user's live browser mid-session and read THEIR page as our result.
SMOKE_PORT = "8799"
_log = []


def say(*a):
    line = " ".join(str(x) for x in a)
    print(line, flush=True)
    _log.append(line)


def run(cmd, **kw):
    say("$", " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, text=True, **kw)


def build():
    say("\n── build ───────────────────────────────────────────────────")
    shutil.rmtree(DIST, ignore_errors=True)
    r = run(["uvx", "--from", "build", "pyproject-build", "--outdir", DIST, REPO],
            capture_output=True)
    if r.returncode != 0:
        say((r.stdout or "")[-1500:], (r.stderr or "")[-1500:])
        return None
    arts = sorted(os.path.join(DIST, f) for f in os.listdir(DIST))
    for a in arts:
        say(f"  {os.path.basename(a)}  {os.path.getsize(a)//1024}K")
    return arts


def twine_check(arts):
    say("\n── twine check (metadata only — does NOT prove it runs) ─────")
    r = run(["uvx", "twine", "check", *arts], capture_output=True)
    say((r.stdout or "").strip()[-800:])
    return r.returncode == 0


def verify_artifact(artifact):
    """Install ONE artifact into a throwaway venv and actually run the command it ships.

    Run from a temp cwd, not the repo: from inside the checkout a broken install can still import
    the local source tree and appear to work.
    """
    name = os.path.basename(artifact)
    say(f"\n── verify {name} ───────────────────────────────")
    with tempfile.TemporaryDirectory(prefix="playwrong-verify-") as tmp:
        venv = os.path.join(tmp, "venv")
        r = run([sys.executable, "-m", "venv", venv], capture_output=True)
        if r.returncode != 0:
            say("venv creation failed:", (r.stderr or "")[-400:])
            return False
        pip = os.path.join(venv, "bin", "pip")
        r = run([pip, "install", "-q", artifact], capture_output=True)
        if r.returncode != 0:
            say("install FAILED:", ((r.stdout or "") + (r.stderr or ""))[-1200:])
            return False
        deps = run([pip, "list", "--format=freeze"], capture_output=True).stdout
        if "nodriver==" in deps:
            # The exact bug that shipped once: the vendored patched copy is the point, and a PyPI
            # nodriver in site-packages shadows it.
            say("FAIL: an unpatched `nodriver` came in as a dependency — it will shadow the "
                "vendored patched copy")
            return False
        exe = os.path.join(venv, "bin", "playwrong")
        if not os.path.exists(exe):
            say("FAIL: no `playwrong` command was installed")
            return False
        env = {**os.environ, "PH_PORT": SMOKE_PORT}
        r = run([exe, SMOKE_URL, "--max-chars", "60", "-q"], capture_output=True, cwd=tmp, env=env)
        out = (r.stdout or "") + (r.stderr or "")
        run([exe, "--stop"], capture_output=True, cwd=tmp, env=env)
        if r.returncode != 0 or "Example Domain" not in out:
            say("FAIL: the installed command could not fetch a page:")
            say(out[-1200:])
            return False
        say("  fetched a live page through the installed package")
        # An installed package writing into site-packages is a packaging bug, not a preference.
        sp = os.path.join(venv, "lib")
        strays = [os.path.join(dp, d) for dp, dn, _ in os.walk(sp) for d in dn if d == "tmp"]
        if strays:
            say(f"FAIL: wrote into site-packages: {strays[0]}")
            return False
        say("  wrote nothing into site-packages")
    return True


def upload(arts, repository):
    say(f"\n── upload -> {repository} ──────────────────────────────────")
    say("twine reads ~/.pypirc or TWINE_USERNAME/TWINE_PASSWORD itself; no token passes through here.")
    r = run(["uvx", "twine", "upload", "--repository", repository, *arts])
    return r.returncode == 0


def from_index(repository):
    """Install the PUBLISHED package and smoke-test it — what a user actually gets."""
    index = {"test-pypi": "https://test.pypi.org/simple/",
             "pypi": "https://pypi.org/simple/"}[repository]
    say(f"\n── install from {index} ────────────────────────────────")
    with tempfile.TemporaryDirectory(prefix="playwrong-index-") as tmp:
        venv = os.path.join(tmp, "venv")
        run([sys.executable, "-m", "venv", venv], capture_output=True)
        pip = os.path.join(venv, "bin", "pip")
        cmd = [pip, "install", "-q", "--index-url", index]
        if repository == "test-pypi":
            # TestPyPI does not mirror real dependencies; let pip fall back to PyPI for those.
            cmd += ["--extra-index-url", "https://pypi.org/simple/"]
        cmd += ["awto-playwrong"]
        r = run(cmd, capture_output=True)
        if r.returncode != 0:
            say("install from index FAILED:", ((r.stdout or "") + (r.stderr or ""))[-1200:])
            return False
        exe = os.path.join(venv, "bin", "playwrong")
        env = {**os.environ, "PH_PORT": SMOKE_PORT}
        r = run([exe, SMOKE_URL, "--max-chars", "60", "-q"], capture_output=True, cwd=tmp, env=env)
        run([exe, "--stop"], capture_output=True, cwd=tmp, env=env)
        ok = r.returncode == 0 and "Example Domain" in ((r.stdout or "") + (r.stderr or ""))
        say("  published package works" if ok else "  FAIL: published package could not fetch")
        return ok


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--test-pypi", action="store_true", help="upload to TestPyPI after verifying")
    g.add_argument("--pypi", action="store_true", help="upload to PyPI after verifying (asks first)")
    g.add_argument("--from-index", choices=["test-pypi", "pypi"],
                   help="skip building; install the PUBLISHED package and smoke-test it")
    a = ap.parse_args()
    os.makedirs(LOGDIR, exist_ok=True)

    try:
        if a.from_index:
            return 0 if from_index(a.from_index) else 1

        arts = build()
        if not arts or not twine_check(arts):
            say("\nBUILD/CHECK FAILED — nothing uploaded")
            return 1
        if not all(verify_artifact(x) for x in arts):
            say("\nVERIFY FAILED — nothing uploaded. Fix the package, do not publish it: a released "
                "version cannot be replaced, only yanked.")
            return 1
        say("\nboth artifacts install clean and run.")

        if a.pypi:
            say("\nThis uploads to REAL PyPI. A released version can never be re-uploaded, only "
                "yanked.")
            if input("type the version to confirm (e.g. 0.1.0): ").strip() not in _version():
                say("no match — aborted")
                return 1
            return 0 if upload(arts, "pypi") else 1
        if a.test_pypi:
            return 0 if upload(arts, "testpypi") else 1

        say("\n(no upload requested — add --test-pypi or --pypi)")
        return 0
    finally:
        with open(os.path.join(LOGDIR, "release.log"), "w") as f:
            f.write("\n".join(_log) + "\n")


def _version():
    for line in open(os.path.join(REPO, "pyproject.toml")):
        if line.startswith("version"):
            return line.split("=")[1].strip().strip('"')
    return "?"


if __name__ == "__main__":
    sys.exit(main())
