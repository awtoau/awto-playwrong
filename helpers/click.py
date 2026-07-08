#!/usr/bin/env python3
"""click.py — thin shim: click at (x,y) in the shared browser, settle, screenshot.

The implementation now lives in the engine (`crawl.drive`) — one HTTP path, condition-based settle
(no fixed sleep). This wrapper keeps the documented `helpers/click.py X Y [OUT]` invocation working.
Equivalent: `python -m crawl.drive click X Y [OUT]`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from crawl.drive import _main   # noqa: E402

if __name__ == "__main__":
    _main(["click", *sys.argv[1:]])
