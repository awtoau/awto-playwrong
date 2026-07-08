# helpers/

Thin CLI shims for **hand-driving** the shared playwrong browser (click, scroll, screenshot each
step). The implementation lives in the engine — [`crawl/drive.py`](../crawl/drive.py) — which reuses
the engine's HTTP client (one code path) and settles **condition-based (no fixed `sleep`)** by polling
`document.readyState` after each action. These scripts just forward to it, so the old invocation keeps
working. Prefer the module form in new code.

The engine must be running (see [`../docs/AGENT-API.md`](../docs/AGENT-API.md)).

| Script (shim) | Module form | What it does |
|---|---|---|
| `python3 helpers/click.py X Y [OUT]` | `python -m crawl.drive click X Y [OUT]` | click (x,y), settle, screenshot |
| `python3 helpers/scroll.py [bottom\|top\|<px>] [OUT]` | `python -m crawl.drive scroll [to] [OUT]` | scroll, settle, screenshot |
| — | `python -m crawl.drive shot [OUT]` | screenshot the current page |

Env:
- `PH_PORT` — engine port (default `8731`)
- `PH_OUT_DIR` — screenshot output dir (default `./tmp`)

Library use:
```python
from crawl import drive
drive.scroll("bottom", "step1")     # -> tmp/step1.png
drive.click(1643, 115, "step2")     # click, settle, screenshot
info = drive.where()                 # {'title':…, 'url':…}
```
