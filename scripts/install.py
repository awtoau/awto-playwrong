"""install.py — set playwrong up on a fresh machine and register it as an MCP server.

    python scripts/install.py                 # check + show exactly what would happen (no changes)
    python scripts/install.py --deps          # install the two runtime deps
    python scripts/install.py --register      # register with Claude Code (user scope)
    python scripts/install.py --deps --register --scope user
    python scripts/install.py --print-config  # the JSON block, for any other MCP client

Nothing is written unless you pass --deps / --register / --write-config: the bare command only
inspects. Registration prefers the `claude` CLI (`claude mcp add`) because it writes whatever config
file that version of Claude Code actually reads; the direct-file path is only a fallback.

Log: tmp/logs/install.log
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGDIR = os.path.join(REPO, "tmp", "logs")
SERVER = os.path.join(REPO, "engine", "mcp_server.py")
REQS = os.path.join(REPO, "requirements-engine.txt")
_log = []


def say(*a):
    line = " ".join(str(x) for x in a)
    print(line, flush=True)
    _log.append(line)


def mcp_entry(name="playwrong", port=None):
    """The MCP server definition. sys.executable is deliberate: the interpreter running install.py is
    the one that has the deps, and an MCP client starts the server with no shell and no PATH of
    yours — a bare "python" would be a coin flip."""
    env = {}
    if port:
        env["PH_PORT"] = str(port)
    # normpath, not realpath: it cleans a "../venv/bin/python" invocation into an absolute path,
    # while realpath would resolve the venv symlink back to the BASE interpreter — which does not
    # have the venv's packages, so the server would start and then fail to launch a browser.
    entry = {"command": os.path.normpath(os.path.abspath(sys.executable)), "args": [SERVER]}
    if env:
        entry["env"] = env
    return name, entry


def run_doctor():
    say("── preflight ───────────────────────────────────────────────")
    r = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "doctor.py")],
                       capture_output=True, text=True)
    say(r.stdout.rstrip())
    if r.stderr.strip():
        say(r.stderr.rstrip())
    return r.returncode == 0


def _dep_commands():
    """Ways to install into THIS interpreter, best first.

    pip is tried first and uv second, deliberately. `uv pip install` resolves its target from the
    ACTIVE environment, not from the python running this script — so when install.py runs as
    <venv>/bin/python without VIRTUAL_ENV exported (exactly how you'd invoke it from another
    directory), plain `uv pip install` aborts with "No virtual environment found" and, worse, a
    `--system` retry would install into the wrong interpreter. `--python <sys.executable>` pins uv to
    the right one. pip needs no such care, so it leads; uv is the fallback for interpreters that
    ship without pip.
    """
    uv = shutil.which("uv")
    cmds = [[sys.executable, "-m", "pip", "install", "-r", REQS]]
    if uv:
        cmds.append([uv, "pip", "install", "--python", sys.executable, "-r", REQS])
    return cmds


def install_deps():
    say("\n── dependencies ────────────────────────────────────────────")
    if not os.path.exists(REQS):
        say(f"missing {REQS} — re-clone the repo")
        return False
    cmds, last = _dep_commands(), ""
    for i, cmd in enumerate(cmds):
        say("running:", " ".join(cmd))
        r = subprocess.run(cmd, capture_output=True, text=True)
        last = ((r.stdout or "") + (r.stderr or "")).rstrip()
        if r.returncode == 0:
            say(last[-1500:])
            say("dependencies installed")
            return True
        if i + 1 < len(cmds):
            say(f"  -> failed (exit {r.returncode}); trying the next method")
    say(last[-2000:])
    say("dependency install FAILED")
    if "externally-managed-environment" in last:
        # Debian/Fedora system pythons refuse writes by policy (PEP 668). A venv is the right answer.
        say(f"hint: this is a distro-managed Python. Make a venv and use that interpreter:\n"
            f"  {sys.executable} -m venv .venv\n"
            f"  .venv/bin/python scripts/install.py --deps --register")
    else:
        say(f"hint: install them yourself, then re-run doctor:\n"
            f"  {sys.executable} -m pip install -r {REQS}")
    return False


def write_log_file(path, lines):
    with open(path, "w", encoding="utf-8") as f:
        if lines:
            f.write("\n".join(lines) + "\n")
        else:
            f.write("")


def _write_windows_cmd_wrapper(dest_dir, src):
    wrapper = os.path.join(dest_dir, "playwrong.cmd")
    if os.path.exists(wrapper) or os.path.islink(wrapper):
        if os.path.realpath(wrapper) == os.path.realpath(src):
            say(f"already linked: {wrapper}")
            return True
        say(f"refusing to overwrite existing {wrapper} (points at {os.path.realpath(wrapper)})")
        return False
    cmdline = subprocess.list2cmdline([sys.executable, src])
    with open(wrapper, "w", encoding="utf-8") as f:
        f.write("@echo off\r\nsetlocal\r\n" + cmdline + " %*\r\n")
    say(f"linked {wrapper} -> {src}")
    return True


def link_cli(dest_dir=None):
    """Put a `playwrong` launcher on PATH. On POSIX we symlink; on Windows we fall back to a
    `.cmd` wrapper when symlink creation is blocked by privilege policy."""
    src = os.path.join(REPO, "playwrong")
    dest_dir = os.path.expanduser(dest_dir or "~/.local/bin")
    dest = os.path.join(dest_dir, "playwrong")
    try:
        os.makedirs(dest_dir, exist_ok=True)
        os.chmod(src, 0o755)
        if os.path.islink(dest) or os.path.exists(dest):
            if os.path.realpath(dest) == os.path.realpath(src):
                say(f"already linked: {dest}")
                return True
            say(f"refusing to overwrite existing {dest} (points at {os.path.realpath(dest)})")
            return False
        if os.name == "nt":
            try:
                os.symlink(src, dest)
            except OSError as e:
                if getattr(e, "winerror", None) in {5, 1314} or "privilege" in str(e).lower():
                    return _write_windows_cmd_wrapper(dest_dir, src)
                raise
        else:
            os.symlink(src, dest)
        say(f"linked {dest} -> {src}")
    except OSError as e:
        say(f"could not link into {dest_dir}: {e}")
        return False
    if dest_dir not in (os.environ.get("PATH") or "").split(os.pathsep):
        say(f"NOTE: {dest_dir} is not on your PATH — add it, or run {src} directly")
    else:
        say("try it:  playwrong https://example.com")
    return True


def register_via_cli(name, entry, scope):
    claude = shutil.which("claude")
    if not claude:
        return False, "the `claude` CLI is not on PATH"
    subprocess.run([claude, "mcp", "remove", name, "-s", scope],
                   capture_output=True, text=True)          # idempotent: replace any previous entry
    cmd = [claude, "mcp", "add", name, "-s", scope]
    for k, v in (entry.get("env") or {}).items():
        cmd += ["-e", f"{k}={v}"]
    cmd += ["--", entry["command"]] + entry["args"]
    say("running:", " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    return r.returncode == 0, out


# VSCode has its OWN MCP registry, entirely separate from Claude Code's. `claude mcp add` writes
# ~/.claude.json and the server works in Claude Code — but VSCode's MCP panel reads these files and
# will show nothing, which reads as "the install failed" when it hasn't. Different schema too:
# {"servers": {...}} with an explicit "type", not {"mcpServers": {...}}.
VSCODE_USER_DIRS = {
    "Linux": ["~/.config/Code - Insiders/User", "~/.config/Code/User", "~/.config/VSCodium/User"],
    "Darwin": ["~/Library/Application Support/Code - Insiders/User",
               "~/Library/Application Support/Code/User"],
    "Windows": [r"~\AppData\Roaming\Code - Insiders\User", r"~\AppData\Roaming\Code\User"],
}


def vscode_profile_names(user_dir):
    """id -> human name, from VSCode's globalStorage, so we can say "the python profile" rather than
    "2d9cffbd"."""
    try:
        d = json.load(open(os.path.join(user_dir, "globalStorage", "storage.json")))
        return {p["location"]: p.get("name", p["location"]) for p in d.get("userDataProfiles") or []}
    except Exception:
        return {}


def vscode_targets(explicit=None):
    """Every VSCode MCP config: the default profile's, AND one per named profile.

    **VSCode MCP config is per-profile.** A workspace bound to the "python" profile reads
    User/profiles/<id>/mcp.json and never looks at User/mcp.json — so registering only at the user
    level leaves the panel empty in exactly the window you're working in, which is indistinguishable
    from a broken install. Writing every profile is deliberate: you want the browser available
    whichever profile a project happens to use.
    """
    if explicit:
        return [os.path.expanduser(explicit)]
    import glob
    import platform
    out = []
    for d in VSCODE_USER_DIRS.get(platform.system(), VSCODE_USER_DIRS["Linux"]):
        d = os.path.expanduser(d)
        if not os.path.isdir(d):
            continue
        names = vscode_profile_names(d)
        out.append((os.path.join(d, "mcp.json"), "default profile"))
        for p in sorted(glob.glob(os.path.join(d, "profiles", "*", "mcp.json"))):
            pid = os.path.basename(os.path.dirname(p))
            out.append((p, f"profile {names.get(pid, pid)!r}"))
    return out


def write_vscode(path, name, entry):
    """Merge into a VSCode mcp.json, preserving its existing inputs/servers and indent style."""
    data, indent = {}, "\t"
    if os.path.exists(path):
        raw = open(path).read()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = f"{path}.bak-{stamp}"
        shutil.copy2(path, backup)
        say(f"backed up {path} -> {backup}")
        try:
            data = json.loads(raw) if raw.strip() else {}
        except ValueError as e:
            say(f"refusing to write: {path} is not valid JSON ({e})")
            return False
        for line in raw.splitlines():          # keep whatever indent the file already uses
            if line[:1] in (" ", "\t"):
                indent = "  " if line[0] == " " else "\t"
                break
    data.setdefault("servers", {})[name] = {"type": "stdio", **entry}
    with open(path, "w") as f:
        json.dump(data, f, indent=indent)
        f.write("\n")
    say(f"wrote {name} into {path}")
    return True


def write_config(path, name, entry):
    """Fallback: merge the entry into a client's JSON config, keeping a timestamped backup."""
    path = os.path.expanduser(path)
    data = {}
    if os.path.exists(path):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = f"{path}.bak-{stamp}"
        shutil.copy2(path, backup)
        say(f"backed up {path} -> {backup}")
        try:
            data = json.load(open(path))
        except ValueError as e:
            say(f"refusing to write: {path} is not valid JSON ({e})")
            return False
    data.setdefault("mcpServers", {})[name] = entry
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    say(f"wrote {name} into {path}")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deps", action="store_true", help="install runtime deps")
    ap.add_argument("--register", action="store_true", help="register with Claude Code")
    ap.add_argument("--link", nargs="?", const="~/.local/bin", metavar="DIR",
                    help="symlink the `playwrong` command onto PATH (default ~/.local/bin)")
    ap.add_argument("--vscode", nargs="?", const=True, metavar="PATH",
                    help="also register in VSCode's OWN mcp.json (its MCP panel is a separate "
                         "registry from Claude Code's — a server registered only with `claude mcp "
                         "add` will not appear there). Defaults to every VSCode config found.")
    ap.add_argument("--scope", default="user", choices=["local", "user", "project"],
                    help="claude mcp scope (default user = available in every project)")
    ap.add_argument("--name", default="playwrong", help="MCP server name (tools appear as "
                                                        "mcp__<name>__fetch etc.)")
    ap.add_argument("--port", type=int, help="pin the engine to a non-default PH_PORT")
    ap.add_argument("--print-config", action="store_true", help="print the JSON block and exit")
    ap.add_argument("--write-config", metavar="PATH",
                    help="merge the entry into an arbitrary MCP client config JSON")
    a = ap.parse_args()

    os.makedirs(LOGDIR, exist_ok=True)
    name, entry = mcp_entry(a.name, a.port)

    if a.print_config:
        print(json.dumps({"mcpServers": {name: entry}}, indent=2))
        return 0

    say(f"playwrong install — repo {REPO}")
    say(f"python {sys.version.split()[0]} at {sys.executable}\n")

    if a.deps:
        install_deps()
    if a.link:
        say("\n── playwrong command ───────────────────────────────────────")
        link_cli(a.link)
    healthy = run_doctor()

    say("\n── MCP registration ────────────────────────────────────────")
    say(json.dumps({"mcpServers": {name: entry}}, indent=2))

    if a.vscode:
        say("\n── VSCode MCP registry (separate from Claude Code's) ───────")
        targets = vscode_targets(None if a.vscode is True else a.vscode)
        if not targets:
            say("no VSCode install found. Create a config via the Command Palette "
                "(\"MCP: Open User Configuration\"), then re-run with --vscode.")
        for t in targets:
            path, label = t if isinstance(t, tuple) else (t, "")
            if write_vscode(path, name, entry):
                say(f"    ^ {label}")
        if targets:
            say(f"\nwrote {len(targets)} VSCode config(s) — one per profile, because VSCode MCP "
                f"config is PER-PROFILE and a workspace only reads its own profile's file.")
            say("VSCode reads mcp.json at startup — reload the window "
                "(Command Palette -> Developer: Reload Window) to see it in the MCP panel.")

    if a.write_config:
        write_config(a.write_config, name, entry)
    elif a.register:
        okd, out = register_via_cli(name, entry, a.scope)
        if out:
            say(out)
        if okd:
            say(f"\nregistered as '{name}' ({a.scope} scope). Restart Claude Code, then check with:"
                f"\n  claude mcp list")
        else:
            say(f"\nCLI registration failed ({out or 'unknown'}). Paste the JSON above into your "
                f"client's config, or: python scripts/install.py --write-config ~/.claude.json")
    else:
        say("\n(no changes made — add --register to register with Claude Code, or --print-config "
            "to get the block for another client)")

    if not healthy:
        say("\nNOTE: preflight FAILED above — fix those first or the tools will error at call time.")
    say("\nprove it works:  python scripts/mcp_selftest.py")

    write_log_file(os.path.join(LOGDIR, "install.log"), _log)
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
