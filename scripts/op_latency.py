"""op_latency.py — per-op duration percentiles from the engine log, for deriving timeouts.

- Reads `op_done op=<op> ms=<n> ... err=<bool>` lines from tmp/nd-server.log (every engine writes there).
- Successful ops only by default: a failed op's duration is its timeout, not its cost.

    python scripts/op_latency.py                    # every op
    python scripts/op_latency.py --op newtab closetab
    python scripts/op_latency.py --errors           # include failed ops

Results: tmp/logs/op-latency.log
"""
import argparse
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE_LOG = os.path.join(REPO, "tmp", "nd-server.log")
LOG = os.path.join(REPO, "tmp", "logs", "op-latency.log")
LINE = re.compile(r" op_done op=(\S+) ms=(\d+) .*err=(True|False)")


def pct(xs, p):
    return xs[min(len(xs) - 1, int(p / 100 * len(xs)))]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--op", nargs="*", help="only these ops")
    ap.add_argument("--errors", action="store_true", help="include failed ops")
    a = ap.parse_args()
    by = {}
    with open(ENGINE_LOG, errors="replace") as f:
        for line in f:
            m = LINE.search(line)
            if not m or (m.group(3) == "True" and not a.errors):
                continue
            if a.op and m.group(1) not in a.op:
                continue
            by.setdefault(m.group(1), []).append(int(m.group(2)))
    rows = [f"{'op':<12} {'n':>7} {'p50_ms':>8} {'p99_ms':>8} {'p99.9_ms':>9} {'max_ms':>8}"]
    for op, xs in sorted(by.items()):
        xs.sort()
        rows.append(f"{op:<12} {len(xs):>7} {pct(xs, 50):>8} {pct(xs, 99):>8} {pct(xs, 99.9):>9} "
                    f"{xs[-1]:>8}")
    out = "\n".join(rows)
    print(out)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "w") as f:
        f.write(out + "\n")


if __name__ == "__main__":
    main()
