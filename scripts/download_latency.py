#!/usr/bin/env python3
"""Measure what download()'s per-read socket timeout has to cover, on real downloads (#30).

- Per url: cleared session headers, then urllib with the measurement ceiling below.
- Records time to response headers (connect + TLS + redirects + TTFB) and the longest gap
  between socket reads (read1 = one recv, the unit the timeout applies to).
- CEILING: anything slower is reported as a stall, which is the case the timeout exists for.

Usage:
    python scripts/download_latency.py [--runs N]

Log: tmp/logs/download_latency.log
"""

import argparse
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine import connect  # noqa: E402

LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "tmp", "logs", "download_latency.log")
CEILING = 60.0
URLS = [
    ("ti-pdf", "https://www.ti.com/lit/pdf/SLVMEY3"),
    ("ti-zip", "https://www.ti.com/lit/zip/SLVMEY2"),
    ("adi-pdf", "https://www.analog.com/media/en/technical-documentation/data-sheets/LT3045.pdf"),
    ("python-tgz", "https://www.python.org/ftp/python/3.12.0/Python-3.12.0.tgz"),
    ("st-pdf", "https://www.st.com/resource/en/datasheet/vn9e30f.pdf"),
]
ONE_RUN = {"st-pdf"}   # known stall: one run shows it, more just burn CEILING each


def measure(url, headers):
    t0 = time.monotonic()
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=CEILING) as r:
            t_head = time.monotonic() - t0
            total, gap, last = 0, 0.0, time.monotonic()
            while True:
                chunk = r.read1(1 << 16)
                now = time.monotonic()
                gap, last = max(gap, now - last), now
                if not chunk:
                    break
                total += len(chunk)
        return {"head_s": t_head, "max_gap_s": gap, "bytes": total,
                "total_s": time.monotonic() - t0}
    except (urllib.error.URLError, OSError) as e:
        return {"error": str(e), "after_s": time.monotonic() - t0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a") as log:
        def out(msg):
            print(msg, flush=True)
            log.write(msg + "\n")
        out(f"=== {time.strftime('%Y-%m-%dT%H:%M:%S%z')} ceiling={CEILING}s runs={args.runs}")
        for name, url in URLS:
            headers = connect.session_headers(url)
            for i in range(1 if name in ONE_RUN else args.runs):
                m = measure(url, headers)
                if "error" in m:
                    out(f"{name} run{i}: ERROR after {m['after_s']:.2f}s: {m['error']}")
                else:
                    out(f"{name} run{i}: head {m['head_s']:.3f}s  max_gap {m['max_gap_s']:.3f}s  "
                        f"{m['bytes']:,} B in {m['total_s']:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
