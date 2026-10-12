"""logroll_test.py — engine logs are size-capped (#39). Offline: no browser, no network.

- logroll.roll: under the cap nothing moves; past it the file becomes `.1` (replacing an old `.1`).
- server.log(): writing past the cap rolls nd-server.log instead of growing it.
- connect._log_search: same for search.log.

    python scripts/logroll_test.py

Log: tmp/logs/logroll-test.log
"""
import os
import shutil
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "engine"))
import logroll  # noqa: E402

LOG = os.path.join(REPO, "tmp", "logs", "logroll-test.log")
WORK = os.path.join(REPO, "tmp", "logroll-test")
PASS, FAIL = [], []


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    line = f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else "")
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def size(p):
    return os.path.getsize(p) if os.path.exists(p) else None


def main():
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    open(LOG, "w").close()
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)

    p = os.path.join(WORK, "a.log")
    ok("missing file: nothing to roll", logroll.roll(p, 100) is False)
    open(p, "w").write("x" * 99)
    ok("under the cap: not rolled", logroll.roll(p, 100) is False and size(p) == 99)
    open(p, "a").write("x")
    ok("at the cap: rolled to .1", logroll.roll(p, 100) is True and size(p) is None
       and size(p + ".1") == 100)
    open(p, "w").write("y" * 150)
    logroll.roll(p, 100)
    ok("a second roll replaces the old .1 (one backup, bounded)", size(p + ".1") == 150
       and not os.path.exists(p + ".2"))

    # server.log() and connect._log_search write through roll(). server imports logroll bare and
    # connect as engine.logroll: two module objects, so the small test cap is set on each.
    import server

    from engine import connect
    server.logroll.MAX_BYTES = connect.logroll.MAX_BYTES = 100
    server.LOG = os.path.join(WORK, "nd-server.log")
    open(server.LOG, "w").write("z" * 200)
    server.log("logroll_test", n=1)
    ok("server.log() rolls a full nd-server.log", size(server.LOG + ".1") == 200
       and "logroll_test" in open(server.LOG).read(), f"{size(server.LOG)} bytes now")
    connect.SEARCH_LOG = os.path.join(WORK, "search.log")
    open(connect.SEARCH_LOG, "w").write("s" * 200)
    connect._log_search("test", "q", "results", 1)
    ok("search log rolls when full", size(connect.SEARCH_LOG + ".1") == 200
       and '"engine": "test"' in open(connect.SEARCH_LOG).read())

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
