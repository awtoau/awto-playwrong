"""upstream_check.py — has anything changed around the vendored nodriver since the last review?

Run on every review (CLAUDE.md). Compares against vendor/UPSTREAM.json, the last reviewed state:
- upstream (ultrafunkamsterdam/nodriver) and our fork (awto-au/nodriver): new commits since baseline
- vendor/nodriver against the fork's head: files that differ (local patches not yet in the fork)
- PyPI: latest nodriver release, and whether its wheel's cdp/network.py compiles (#1)
- GitHub: repos doing the same work (nodriver forks/successors, stealth-browser MCP servers) that
  are not in the baseline's known list

    python scripts/upstream_check.py            # report; exit 1 if anything needs a look
    python scripts/upstream_check.py --update   # after reviewing: record the current state

GitHub via `gh`; PyPI through playwrong (connect.download), per the web-access rule.
Results: tmp/logs/upstream-check.log
"""
import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import time
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from engine import connect  # noqa: E402

BASELINE = os.path.join(REPO, "vendor", "UPSTREAM.json")
VENDOR = os.path.join(REPO, "vendor", "nodriver")
LOG = os.path.join(REPO, "tmp", "logs", "upstream-check.log")
WORK = os.path.join(REPO, "tmp", "upstream-check")
# Searches for "the same work". A hit counts when it is active and not a toy.
QUERIES = ["nodriver", "zendriver", "undetected chromedriver", "stealth browser mcp",
           "cloudflare turnstile browser automation"]
MIN_STARS = 25
ACTIVE_DAYS = 180

_fh = None
NOTES = []        # things a reviewer must look at; non-empty -> exit 1


def say(*a):
    line = " ".join(str(x) for x in a)
    print(line, flush=True)
    if _fh:
        _fh.write(line + "\n"); _fh.flush()


def note(msg):
    NOTES.append(msg)
    say(f"  ! {msg}")


def gh(*args, raw=False):
    r = subprocess.run(["gh", *args], capture_output=True)
    if r.returncode:
        raise RuntimeError(f"gh {' '.join(args)}: {r.stderr.decode(errors='replace')[:200]}")
    return r.stdout if raw else json.loads(r.stdout)


def commits_since(repo, base):
    """(head_sha, [one-line commits after base]) on the default branch."""
    head = gh("api", f"repos/{repo}/commits/main")["sha"]
    if head == base:
        return head, []
    cmp = gh("api", f"repos/{repo}/compare/{base}...{head}")
    return head, [f"{c['sha'][:8]} {c['commit']['committer']['date'][:10]} "
                  f"{c['commit']['message'].splitlines()[0][:70]}" for c in cmp.get("commits", [])]


def tree_hashes(files):
    """{relative path: sha256 of LF-normalised bytes} — the fork's upstream history is CRLF (#1)."""
    return {p: hashlib.sha256(b.replace(b"\r\n", b"\n")).hexdigest() for p, b in files.items()}


def vendor_files():
    out = {}
    for root, dirs, names in os.walk(VENDOR):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for n in names:
            if n.endswith(".py"):
                p = os.path.join(root, n)
                out[os.path.relpath(p, VENDOR)] = open(p, "rb").read()
    return out


def fork_files(repo, sha):
    tgz = gh("api", f"repos/{repo}/tarball/{sha}", raw=True)
    out = {}
    with tarfile.open(fileobj=io.BytesIO(tgz)) as t:
        for m in t.getmembers():
            parts = m.name.split("/", 1)
            if m.isfile() and len(parts) == 2 and parts[1].startswith("nodriver/") \
                    and parts[1].endswith(".py"):
                out[parts[1][len("nodriver/"):]] = t.extractfile(m).read()
    return out


def pypi():
    """(latest version, wheel compiles?) through playwrong."""
    os.makedirs(WORK, exist_ok=True)
    meta = connect.download("https://pypi.org/pypi/nodriver/json",
                            path=os.path.join(WORK, "nodriver.json"))
    info = json.load(open(meta["path"]))
    ver = info["info"]["version"]
    wheel = next((u for u in info.get("urls", []) if u.get("packagetype") == "bdist_wheel"), None)
    if not wheel:
        return ver, None
    w = connect.download(wheel["url"], path=os.path.join(WORK, wheel["filename"]),
                         expect_sha256=wheel.get("digests", {}).get("sha256"))
    with zipfile.ZipFile(w["path"]) as z:
        src = z.read("nodriver/cdp/network.py")
    try:
        compile(src, "nodriver/cdp/network.py", "exec")
        return ver, True
    except SyntaxError:
        return ver, False


def alternatives(known):
    cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - ACTIVE_DAYS * 86400))
    found = {}
    for q in QUERIES:
        for r in gh("search", "repos", q, "--sort", "stars", "--limit", "20", "--json",
                    "fullName,stargazersCount,pushedAt,description"):
            if r["stargazersCount"] >= MIN_STARS and r["pushedAt"][:10] >= cutoff:
                found[r["fullName"]] = r
    return found, sorted(set(found) - set(known))


def main():
    global _fh
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--update", action="store_true",
                    help="record the current state as reviewed (vendor/UPSTREAM.json)")
    a = ap.parse_args()
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    _fh = open(LOG, "w")
    base = json.load(open(BASELINE))
    say(f"upstream check against baseline reviewed {base['reviewed']}")

    up_head, up_new = commits_since(base["upstream"], base["upstream_sha"])
    say(f"\n{base['upstream']}: head {up_head[:8]}, {len(up_new)} new commit(s)")
    for c in up_new:
        say(f"    {c}")
    if up_new:
        note(f"upstream has {len(up_new)} commit(s) since review: port what we need into the fork")

    fk_head, fk_new = commits_since(base["fork"], base["fork_sha"])
    say(f"\n{base['fork']}: head {fk_head[:8]}, {len(fk_new)} new commit(s)")
    for c in fk_new:
        say(f"    {c}")
    if fk_new:
        note(f"the fork has {len(fk_new)} commit(s) not yet vendored: re-vendor")

    v, f = tree_hashes(vendor_files()), tree_hashes(fork_files(base["fork"], fk_head))
    diff = sorted(p for p in set(v) | set(f) if v.get(p) != f.get(p))
    say(f"\nvendor/nodriver vs {base['fork']}@{fk_head[:8]}: {len(diff)} file(s) differ")
    for p in diff:
        say(f"    {p}" + ("" if p in v and p in f else " (only in " +
                         ("vendor" if p in v else "fork") + ")"))
    if sorted(diff) != sorted(base.get("vendor_only", [])):
        note("vendor/nodriver differs from the fork in files not recorded as local patches: push "
             "them to the fork, or record them")

    ver, ok_wheel = pypi()
    say(f"\nPyPI nodriver: {ver}; wheel cdp/network.py compiles: {ok_wheel}")
    if ver != base["pypi_version"]:
        note(f"PyPI release changed: {base['pypi_version']} -> {ver}")

    found, new = alternatives(base["known_alternatives"])
    say(f"\nrepos doing the same work (>= {MIN_STARS} stars, pushed in {ACTIVE_DAYS} days): "
        f"{len(found)}, {len(new)} new")
    for n in new:
        r = found[n]
        say(f"    NEW {n} ({r['stargazersCount']} stars, pushed {r['pushedAt'][:10]}): "
            f"{(r['description'] or '')[:90]}")
    if new:
        note(f"{len(new)} repo(s) doing the same work since review: compare before re-vendoring")

    if a.update:
        base.update(reviewed=time.strftime("%Y-%m-%d"), upstream_sha=up_head, fork_sha=fk_head,
                    pypi_version=ver, vendor_only=diff,
                    known_alternatives=sorted(set(base["known_alternatives"]) | set(found)))
        with open(BASELINE, "w") as fh:
            json.dump(base, fh, indent=2)
            fh.write("\n")
        say(f"\nbaseline updated: {BASELINE}")
        return 0
    say(f"\n{len(NOTES)} item(s) to review" + ("" if NOTES else ": nothing changed"))
    say(f"log: {LOG}")
    return 1 if NOTES else 0


if __name__ == "__main__":
    sys.exit(main())
