"""stress_test.py — the edge cases the happy-path tests never reach.

mcp_selftest.py proves the tools work. This proves they don't come apart: duplicate and bad urls,
empty batches, collect called twice, ops naming a tab that doesn't exist, a batch running while
someone else drives the browser, malformed requests, and whether tabs and memory come back to where
they started.

    python scripts/stress_test.py                 # everything
    python scripts/stress_test.py --port 8797     # against an isolated engine (recommended)

Log: tmp/logs/stress-test.log
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from engine import connect  # noqa: E402

PASS, FAIL, _fh = [], [], None
PORT = None


def say(*a):
    line = " ".join(str(x) for x in a)
    print(line, flush=True)
    if _fh:
        _fh.write(line + "\n"); _fh.flush()


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    say(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return cond


def tabs():
    return connect.call("tabs", port=PORT, method="GET").get("count", -1)


# ── prefetch edge cases ─────────────────────────────────────────────────────────────────────────

def t_duplicate_urls():
    """The SAME url twice in one batch. Results are stored per-url, so a duplicate can silently
    collapse two entries into one and leave the batch looking permanently incomplete."""
    urls = ["https://example.com", "https://example.org", "https://example.com"]
    job = connect.prefetch(urls, concurrency=3, timeout=20, port=PORT)
    got, t0 = [], time.monotonic()
    while len(got) < len(urls) and time.monotonic() - t0 < 45:
        got += connect.collect(job, max_chars=50, port=PORT, wait=3)["results"]
    ok("duplicate urls in a batch all come back", len(got) == len(urls),
       f"asked {len(urls)}, got {len(got)} in {time.monotonic()-t0:.0f}s")


def t_bad_urls():
    """A batch of things that cannot load must not hang or poison the good ones."""
    urls = ["https://example.com", "https://no-such-host-zzz-playwrong.invalid",
            "https://example.org/404-definitely-missing"]
    job = connect.prefetch(urls, concurrency=3, timeout=15, port=PORT)
    got, t0 = [], time.monotonic()
    while len(got) < len(urls) and time.monotonic() - t0 < 60:
        got += connect.collect(job, max_chars=50, port=PORT, wait=3)["results"]
    good = [g for g in got if not g.get("error") and "Example Domain" in (g.get("text") or "")]
    ok("a batch with bad urls still returns every slot", len(got) == len(urls),
       f"{len(got)}/{len(urls)}")
    ok("the good url in a bad batch still works", good, f"{len(good)} good")


def t_empty_and_unknown():
    job = connect.prefetch([], concurrency=4, port=PORT)
    r = connect.collect(job, port=PORT)
    ok("empty batch is not an error", isinstance(r.get("results"), list), json.dumps(r)[:80])
    try:
        r = connect.call("collect", port=PORT, job="job-does-not-exist")
        ok("collect on an unknown job errors cleanly", False, json.dumps(r)[:80])
    except connect.EngineError as e:
        ok("collect on an unknown job errors cleanly", "no such job" in str(e), str(e)[:70])


def t_drain_once():
    """A result must be handed over exactly once, or an agent double-processes pages."""
    job = connect.prefetch(["https://example.com"], concurrency=1, timeout=20, port=PORT)
    first, t0 = [], time.monotonic()
    while not first and time.monotonic() - t0 < 40:
        first = connect.collect(job, max_chars=50, port=PORT, wait=3)["results"]
    second = connect.collect(job, max_chars=50, port=PORT)["results"]
    ok("collect drains: a page is delivered exactly once", len(first) == 1 and len(second) == 0,
       f"first={len(first)} second={len(second)}")


def t_more_than_concurrency():
    """12 urls through 3 slots — the semaphore must queue them, not drop or deadlock."""
    urls = [f"https://example.com/?q={i}" for i in range(12)]
    t0 = time.monotonic()
    job = connect.prefetch(urls, concurrency=3, timeout=20, port=PORT)
    got = []
    while len(got) < len(urls) and time.monotonic() - t0 < 120:
        got += connect.collect(job, max_chars=20, port=PORT, wait=5)["results"]
    ok("12 urls through 3 slots all complete", len(got) == len(urls),
       f"{len(got)}/12 in {time.monotonic()-t0:.0f}s")


# ── tab discipline ──────────────────────────────────────────────────────────────────────────────

def t_tabs_return_to_baseline():
    base = tabs()
    urls = ["https://example.com", "https://example.org"] * 3
    job = connect.prefetch(urls, concurrency=6, timeout=20, port=PORT)
    peak, got, t0 = base, [], time.monotonic()
    while len(got) < len(urls) and time.monotonic() - t0 < 60:
        peak = max(peak, tabs())
        got += connect.collect(job, max_chars=20, port=PORT, wait=2)["results"]
    time.sleep(2)                      # Chrome destroys targets asynchronously after close
    ok("a finished batch leaves no tabs behind", tabs() == base,
       f"base={base} peak={peak} now={tabs()}")


def t_bad_tag():
    try:
        connect.call("text", port=PORT, tab="no-such-tag-at-all")
        ok("an op naming a missing tab errors instead of using the wrong one", False, "no error")
    except connect.EngineError as e:
        ok("an op naming a missing tab errors instead of using the wrong one",
           "no such tab" in str(e), str(e)[:70])


def t_close_twice():
    tag = "stress-close-twice"
    connect.call("newtab", port=PORT, url="about:blank", tag=tag)
    a = connect.call("closetab", port=PORT, tag=tag)
    b = connect.call("closetab", port=PORT, tag=tag)
    ok("closing an already-closed tab is a no-op, not an error",
       a.get("closed") == 1 and b.get("closed") == 0, f"{a.get('closed')} then {b.get('closed')}")


# ── mixed workload: a batch running while someone else drives ───────────────────────────────────

def t_batch_plus_interactive():
    """The whole point of tagging: a background batch must not move an interactive caller's page."""
    tag = "stress-interactive"
    connect.call("newtab", port=PORT, url="about:blank", tag=tag)
    connect.call("goto", port=PORT, url="https://example.org", tab=tag, timeout=60)
    job = connect.prefetch(["https://example.com"] * 4 + ["https://en.wikipedia.org/wiki/Cloudflare"],
                           concurrency=5, timeout=25, port=PORT)
    wrong, checks, t0 = 0, 0, time.monotonic()
    while time.monotonic() - t0 < 25:
        title = connect.call("js", port=PORT, expr="document.title", tab=tag).get("result") or ""
        checks += 1
        if "Example Domain" not in title:
            wrong += 1
        st = connect.call("jobs", port=PORT, job=job)
        if st.get("ready", 0) + st.get("errors", 0) >= 5:
            break
        time.sleep(0.5)
    connect.call("closetab", port=PORT, tag=tag)
    ok("a background batch never moves an interactive caller's page", wrong == 0,
       f"{checks} checks, {wrong} wrong")


# ── protocol robustness ─────────────────────────────────────────────────────────────────────────

def t_malformed():
    base = connect.base_url(PORT)
    try:
        req = urllib.request.Request(f"{base}/goto", data=b"{not json",
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=10).read()
        ok("malformed JSON doesn't take the engine down", True, "handled")
    except urllib.error.HTTPError:
        ok("malformed JSON doesn't take the engine down", True, "http error, survived")
    except Exception as e:
        ok("malformed JSON doesn't take the engine down", False, repr(e)[:70])
    try:
        connect.call("no_such_op", port=PORT)
        ok("unknown op is reported as an error", False)
    except connect.EngineError as e:
        ok("unknown op is reported as an error", "unknown" in str(e), str(e)[:60])
    try:
        connect.call("goto", port=PORT)          # required arg missing
        ok("a missing required arg errors instead of crashing", False)
    except connect.EngineError as e:
        ok("a missing required arg errors instead of crashing", True, str(e)[:60])
    ok("engine still healthy after malformed input",
       connect.call("status", port=PORT, method="GET").get("alive") is True)


def t_parallel_ops():
    """Many HTTP ops at once from different threads — the engine serialises onto one loop."""
    errs, n = [], 12
    def worker(i):
        try:
            connect.call("js", port=PORT, expr=f"1+{i}")
        except Exception as e:
            errs.append(repr(e)[:60])
    ths = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    ok("concurrent ops from many threads all succeed", not errs, f"{len(errs)} errors")


def t_processes():
    """Real OS processes, not threads — the multi-agent case."""
    r = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "concurrency_test.py"),
                        "-n", "10"] + (["--port", str(PORT)] if PORT else []),
                       capture_output=True, text=True)
    ok("10 concurrent processes each get their own page", r.returncode == 0,
       (r.stdout or "").strip().splitlines()[-1][:80] if r.stdout else "")


def main():
    global _fh, PORT
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, help="engine port (use an isolated one)")
    a = ap.parse_args()
    PORT = a.port
    os.makedirs(os.path.join(connect.DATA, "logs"), exist_ok=True)
    _fh = open(os.path.join(connect.DATA, "logs", "stress-test.log"), "w")

    connect.ensure(PORT, on_start=lambda m: say(f"… {m}"))
    say(f"stress test against port {PORT or connect.default_port()}\n")

    for fn in (t_duplicate_urls, t_bad_urls, t_empty_and_unknown, t_drain_once,
               t_more_than_concurrency, t_tabs_return_to_baseline, t_bad_tag, t_close_twice,
               t_batch_plus_interactive, t_malformed, t_parallel_ops, t_processes):
        try:
            fn()
        except Exception as e:
            ok(fn.__name__, False, f"raised {e!r}"[:160])

    say(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        say("failed: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
