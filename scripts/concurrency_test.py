"""concurrency_test.py — prove many agents can share ONE browser without stealing each other's page.

This exists because they could not. Every engine op used to act on the engine's single ACTIVE tab,
so two callers driving one engine interleaved: process A issued `goto`, process B's `goto` moved the
active tab, and A's `text` returned B's page — as a plausible-looking answer to A's url. It was
caught in the wild when a release check fetched example.com and got the page the user happened to be
browsing.

The fix is tab tagging: each caller opens a tagged tab and names it on every op. This script is the
regression guard. It launches N processes at once against ONE engine and asserts each got the url it
asked for, and that no tabs leaked.

It also checks the other half of sharing one browser: that one agent's cleanup cannot close another
agent's page. close_extra used to close every tab regardless of owner, so the documented recovery for
a wedged session took out every other agent on the browser (#15).

    python scripts/concurrency_test.py            # 6 processes, 2 urls
    python scripts/concurrency_test.py -n 12

Log: tmp/logs/concurrency-test.log
"""
import argparse
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from engine import connect  # noqa: E402

URLS = ["https://example.com", "https://nowsecure.nl"]


def check_ownership(port):
    """close_extra must spare a live agent's tab, and still reap one whose owner has exited.

    Three tabs are planted directly on the engine: one owned by a live process (this one, under a
    different label), one owned by a pid that cannot exist, and one unlabelled. Then close_extra runs
    as a DIFFERENT agent. The live one has to survive — that is the whole property.
    """
    live = f"other-agent@elsewhere:{os.getpid()}"          # a real, running pid
    dead = "crashed-agent@elsewhere:2147483646"            # above pid_max: cannot be running
    fails = []
    try:
        connect.call("newtab", port=port, url="about:blank", tag="own-live", owner=live)
        connect.call("newtab", port=port, url="about:blank", tag="own-dead", owner=dead)
        r = connect.call("closeextra", port=port, owner="sweeper@here:1", force=False)
        tabs = connect.call("tabs", port=port, method="GET").get("tabs", [])
        owners = [t.get("owner") for t in tabs]
        if live not in owners:
            fails.append("close_extra closed a LIVE agent's tab")
        if dead in owners:
            fails.append("close_extra left a dead agent's orphan tab behind")
        if not r.get("skipped"):
            fails.append("close_extra did not report the tab it skipped")
        print(f"\n  ownership: closed={r.get('closed')} skipped={len(r.get('skipped') or [])} "
              f"— live agent's tab {'SURVIVED' if live in owners else 'WAS CLOSED'}, "
              f"dead agent's tab {'reaped' if dead not in owners else 'LEFT'}")
        # Clean up the planted tab we deliberately spared.
        connect.call("closetab", port=port, tag="own-live")
    except connect.EngineError as e:
        fails.append(f"ownership check could not run: {e}")
    for f in fails:
        print(f"  OWNERSHIP FAIL: {f}")
    return fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=6, help="how many concurrent processes")
    ap.add_argument("--port", type=int, help="engine port (default: the shared one)")
    a = ap.parse_args()
    logdir = os.path.join(connect.DATA, "logs")
    os.makedirs(logdir, exist_ok=True)

    urls = [URLS[i % len(URLS)] for i in range(a.n)]
    cmd = [sys.executable, os.path.join(REPO, "engine", "cli.py")]
    port = ["--port", str(a.port)] if a.port else []
    print(f"launching {a.n} processes against one engine…")
    procs = [subprocess.Popen(cmd + [u, "--json", "-q"] + port,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for u in urls]

    bad, lines = 0, []
    for u, p in zip(urls, procs, strict=True):   # one process per url, always
        out, err = p.communicate()
        want = u.split("//")[1].split("/")[0]
        try:
            got = json.loads(out[out.index("{"):]).get("url", "")
        except Exception:
            got = f"(no json) {err.strip()[:80]}"
        ok = want in got
        bad += 0 if ok else 1
        lines.append(f"  asked {u:26} -> {got:34} {'OK' if ok else 'WRONG PAGE'}")
    print("\n".join(lines))

    ownership_failures = check_ownership(a.port)

    # Every capture opens and closes its own tab, so the engine should be back to just its base tab.
    try:
        left = connect.call("tabs", port=a.port, method="GET").get("count", -1)
    except connect.EngineError:
        left = -1
    leaked = max(0, left - 1)
    print(f"\n{a.n - bad}/{a.n} got the page they asked for; {leaked} tab(s) leaked")
    with open(os.path.join(logdir, "concurrency-test.log"), "w") as f:
        f.write("\n".join(lines) + f"\nwrong:{bad} leaked:{leaked}\n")
    if bad or leaked or ownership_failures:
        print("FAIL — concurrent callers are interfering with each other")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
