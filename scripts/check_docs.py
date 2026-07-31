"""check_docs.py — verify the docs describe the code that actually exists.

Docs rot silently: a file gets renamed and every snippet that names it keeps looking plausible. This
repo shipped `mcp/server.py` in four documents after the file moved, and told people to run
`PYTHONPATH=vendor` for a step that had not been needed in months. So the check is mechanical:

  1. every repo-relative path mentioned in a markdown file exists
  2. no doc references a file the repo no longer has (a stale-name blocklist)
  3. every `playwrong` CLI flag shown in the docs is a real flag
  4. every MCP tool named in docs/MCP.md's table is a real registered tool

    python scripts/check_docs.py

Exit 0 when clean, 1 otherwise. Findings also land in tmp/logs/check-docs.log.
"""
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGDIR = os.path.join(REPO, "tmp", "logs")
DOCS = ["README.md"] + [os.path.join("docs", f) for f in sorted(os.listdir(os.path.join(REPO, "docs")))
                        if f.endswith(".md")]

# Files that USED to exist. Naming one in a doc is always a bug now — this is the check that would
# have caught the mcp/server.py rename immediately.
GONE = ["mcp/server.py", "engine/solve.py", "engine/shared_browser.py", "methods/",
        "nd_server.py", "nd_solve.py", "start_server.py", "browser_ctl.py", "ph_common"]

# Markdown link targets and inline-code paths that look repo-relative.
LINK = re.compile(r"\[[^\]]*\]\((?!https?:|#|mailto:)([^)#]+)")
CODEPATH = re.compile(r"`([A-Za-z0-9_./-]+\.(?:py|md|txt|toml|sql|json))`")

problems = []


def note(kind, doc, detail):
    problems.append(f"{kind}: {doc}: {detail}")


# Paths that belong to OTHER tools (VSCode's config tree, a user's home). Naming them is correct;
# expecting them inside this repo is not.
EXTERNAL = ("User/", ".vscode/", "~", "/home", "/mnt", "site-packages", "AppData")


def _resolves(doc, target):
    """A path resolves if it exists relative to the doc, the repo root, or vendor/ — or, for a bare
    basename mentioned in prose, if the repo contains a file with that name anywhere."""
    roots = [os.path.dirname(os.path.join(REPO, doc)), REPO, os.path.join(REPO, "vendor"),
             os.path.join(REPO, "vendor", "nodriver")]
    return any(os.path.exists(os.path.join(r, target)) for r in roots)


def check_paths():
    for doc in DOCS:
        text = open(os.path.join(REPO, doc)).read()
        for m in list(LINK.finditer(text)) + list(CODEPATH.finditer(text)):
            target = m.group(1).strip()
            if target.startswith(("http", "<", "$", "%")) or " " in target:
                continue
            if any(x in target for x in EXTERNAL):
                continue
            # A bare filename in prose ("edit mcp.json", "server.py holds the browser") is not a
            # path claim. Renames are caught by the GONE list instead, which is the case that
            # actually rots.
            if "/" not in target:
                continue
            if not _resolves(doc, target):
                note("missing path", doc, target)


def check_gone():
    for doc in DOCS:
        text = open(os.path.join(REPO, doc)).read()
        for name in GONE:
            for i, line in enumerate(text.splitlines(), 1):
                # A line that also says the name is obsolete is documentation, not a stale reference.
                if name in line and not re.search(
                        r"used to|no longer|obsolete|removed|renamed|was |formerly|stale|dead", line,
                        re.I):
                    note("stale name", doc, f"line {i}: {name!r} in {line.strip()[:70]}")


def check_cli_flags():
    help_text = subprocess.run([sys.executable, os.path.join(REPO, "engine", "cli.py"), "--help"],
                               capture_output=True, text=True).stdout
    real = set(re.findall(r"--[a-z][a-z-]+", help_text))
    for doc in DOCS:
        text = open(os.path.join(REPO, doc)).read()
        for line in text.splitlines():
            # Only lines that actually INVOKE the command. "playwrong" appears in plenty of prose,
            # and install.py has its own flags that would otherwise be reported as CLI flags.
            if not re.search(r"(^|[`\s./])playwrong\s+-", line) or "install.py" in line:
                continue
            for flag in re.findall(r"(--[a-z][a-z-]+)", line):
                if flag not in real:
                    note("unknown CLI flag", doc, f"{flag} in {line.strip()[:70]}")


def check_mcp_tools():
    sys.path.insert(0, REPO)
    from engine import mcp_server
    real = set(mcp_server.BY_NAME)
    text = open(os.path.join(REPO, "docs", "MCP.md")).read()
    documented = set()
    for line in text.splitlines():
        if line.startswith("|") and "`" in line:
            for name in re.findall(r"`([a-z_]+)`", line.split("|")[1]):
                documented.add(name)
    for name in documented - real:
        note("documented tool does not exist", "docs/MCP.md", name)
    for name in real - documented:
        note("tool exists but is undocumented", "docs/MCP.md", name)


def main():
    os.makedirs(LOGDIR, exist_ok=True)
    check_paths()
    check_gone()
    check_cli_flags()
    check_mcp_tools()
    for p in problems:
        print(p)
    print(f"\n{len(problems)} problem(s) across {len(DOCS)} documents")
    with open(os.path.join(LOGDIR, "check-docs.log"), "w") as f:
        f.write("\n".join(problems) + "\n")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
