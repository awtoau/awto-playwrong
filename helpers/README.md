# helpers/

CLI shims for hand-driving the shared browser. The code lives in [`crawl/drive.py`](../crawl/drive.py):
it drives its own tagged tab (`drive`), starts the engine if it's down, and settles after each action
by polling `document.readyState` (no fixed sleep).

| Shim | Module form | What |
|---|---|---|
| `python3 helpers/click.py X Y [OUT]` | `python -m crawl.drive click X Y [OUT]` | click, settle, screenshot |
| `python3 helpers/scroll.py [bottom\|top\|<px>] [OUT]` | `python -m crawl.drive scroll [to] [OUT]` | scroll, settle, screenshot |
| — | `python -m crawl.drive goto URL` | navigate the drive tab |
| — | `python -m crawl.drive shot [OUT]` | screenshot |

Env: `PH_PORT` (default 8731), `PH_OUT_DIR` (default `./tmp`).

```python
from crawl import drive
drive.goto("https://awto.au")
drive.scroll("bottom", "step1")     # -> tmp/step1.png
drive.click(1643, 115, "step2")
info = drive.where()                # {'title': …, 'url': …}
```
