"""frontier_test.py — prove --max-per-host actually caps, without crawling anything.

The runaway this guards against was measured: a 58-site run stored 800 pages and left 10,195 urls
queued, because nothing stopped a single host contributing forever. The cap has to hold across all
three claim() ordering modes, and it has to survive a RESUME — a second run against the same db must
count what the first one already took, or the cap silently doubles.

No browser and no network: the frontier is seeded directly and claim() is driven on its own.

    python scripts/frontier_test.py

Log: tmp/logs/frontier-test.log
"""
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


def seed(d, hosts=3, per_host=25):
    """A frontier of `hosts` sites with `per_host` urls each — the shape that runs away."""
    d.init_schema()                 # open_db() does not create tables; the runner calls this
    urls = [f"https://site{h}.test/page{i}" for h in range(hosts) for i in range(per_host)]
    d.enqueue_many([(u, 1, None) for u in urls])
    return urls


def host_of(u):
    from urllib.parse import urlsplit
    return urlsplit(u).netloc.lower()


def run_claims(d, cap, mode, batches=20, n=10):
    """Claim repeatedly the way run.py does, honouring the cap, and report what each host gave up."""
    per_host = d.host_counts() if cap else {}
    taken = []
    for _ in range(batches):
        kw = {"shuffle": mode == "shuffle", "host_diverse": mode == "host_diverse",
              "max_per_host": cap, "host_counts": per_host}
        batch = d.claim(n, **kw)
        if not batch:
            break
        for u, _dep, _lc in batch:
            per_host[host_of(u)] = per_host.get(host_of(u), 0) + 1
            taken.append(u)
    return taken, per_host


def main():
    global _fh
    from crawl import db as dbmod
    logdir = os.path.join(REPO, "tmp", "logs")
    os.makedirs(logdir, exist_ok=True)
    _fh = open(os.path.join(logdir, "frontier-test.log"), "w")

    for mode in ("host_diverse", "shuffle", "default"):
        with tempfile.TemporaryDirectory() as tmp:
            d = dbmod.open_db(f"sqlite:///{os.path.join(tmp, 'f.sqlite')}")
            seed(d, hosts=3, per_host=25)
            taken, per_host = run_claims(d, cap=5, mode=mode)
            over = {h: c for h, c in per_host.items() if c > 5}
            ok(f"[{mode}] no host exceeds --max-per-host=5", not over, f"{per_host} over={over}")
            ok(f"[{mode}] every host still contributed", len(per_host) == 3, str(per_host))
            left = d.counts().get("frontier_queued", -1)
            ok(f"[{mode}] over-cap urls stay QUEUED, not discarded", left == 75 - len(taken),
               f"{left} queued, {len(taken)} taken")
            d.close()

    # Resume: a second run against the same db must count what the first already took.
    with tempfile.TemporaryDirectory() as tmp:
        path = f"sqlite:///{os.path.join(tmp, 'r.sqlite')}"
        d = dbmod.open_db(path)
        seed(d, hosts=2, per_host=25)
        run_claims(d, cap=4, mode="host_diverse")
        d.close()
        d2 = dbmod.open_db(path)
        d2.init_schema()
        counts = d2.host_counts()
        ok("resume: host_counts sees the previous run's pages",
           all(c >= 4 for c in counts.values()) and len(counts) == 2, str(counts))
        second, _ = run_claims(d2, cap=4, mode="host_diverse")
        ok("resume: a second run takes nothing more once the cap is met", not second,
           f"{len(second)} extra claimed")
        d2.close()

    say(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        say("failed: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
