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
SERVER = os.path.join(REPO, "mcp", "server.py")
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
    entry = {"command": sys.executable, "args": [SERVER]}
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


def install_deps():
    say("\n── dependencies ────────────────────────────────────────────")
    if not os.path.exists(REQS):
        say(f"missing {REQS} — re-clone the repo")
        return False
    cmd = ([shutil.which("uv"), "pip", "install", "-r", REQS] if shutil.which("uv")
           else [sys.executable, "-m", "pip", "install", "-r", REQS])
    say("running:", " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True)
    say((r.stdout or "").rstrip()[-2000:])
    if r.returncode != 0:
        say((r.stderr or "").rstrip()[-2000:])
        say("dependency install FAILED")
        # uv pip outside a venv refuses by design; say so instead of leaving them guessing.
        if "virtual environment" in (r.stderr or ""):
            say("hint: uv wants a venv. Either create one "
                f"({sys.executable} -m venv .venv && . .venv/bin/activate) "
                "or force the system env with: uv pip install --system -r requirements-engine.txt")
        return False
    say("dependencies installed")
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
    healthy = run_doctor()

    say("\n── MCP registration ────────────────────────────────────────")
    say(json.dumps({"mcpServers": {name: entry}}, indent=2))

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

    with open(os.path.join(LOGDIR, "install.log"), "w") as f:
        f.write("\n".join(_log) + "\n")
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
