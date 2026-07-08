#!/usr/bin/env python3
"""scroll.py — thin shim: scroll the shared page and screenshot.

The implementation now lives in the engine (`crawl.drive`) — one HTTP path, condition-based settle
(no fixed sleep). This wrapper keeps the documented `helpers/scroll.py [to] [OUT]` invocation working.
Equivalent: `python -m crawl.drive scroll [bottom|top|<px>] [OUT]`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from crawl.drive import _main   # noqa: E402

if __name__ == "__main__":
    _main(["scroll", *sys.argv[1:]])
