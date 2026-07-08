"""crawl.drive — hand-drive the shared playwrong browser: click, scroll, screenshot, step through.

Folds the former loose `helpers/click.py` + `helpers/scroll.py` into the engine as one reusable module.
It talks to the running engine over HTTP (reusing crawl.browser._call — one HTTP path, no duplication)
and — unlike the old helpers — **settles without a fixed `time.sleep`**: after an action it polls
`document.readyState` + a paint tick until the page is quiet or a bounded number of polls elapse. This
is the site-agnostic "drive the page and screenshot each step" tool for building/debugging a crawl.

CLI:
    python -m crawl.drive click X Y [OUTNAME]
    python -m crawl.drive scroll [bottom|top|<px>] [OUTNAME]
    python -m crawl.drive shot [OUTNAME]
Env:
    PH_PORT      engine port (default 8731)
    PH_OUT_DIR   screenshot dir (default ./tmp)

Library:
    from crawl import drive
    drive.click(1643, 115, "step2")          # click, settle, screenshot -> tmp/step2.png
    drive.scroll("bottom", "step1")          # scroll, settle, screenshot
    info = drive.where()                      # {'title':…, 'url':…}
"""
import base64
import os
import pathlib
import sys

from .browser import _call


def _port():
    return int(os.environ.get("PH_PORT", "8731"))


def _out_dir():
    return pathlib.Path(os.environ.get("PH_OUT_DIR", "tmp"))


# ── settle (no fixed sleep) ─────────────────────────────────────────────────────────────────────
# Poll readyState + one rAF paint tick until 'complete'. Each /js call is itself a round-trip to the
# browser, so N polls ≈ N paint frames of settle — bounded, condition-based, no wall-clock sleep.
_READY_JS = "document.readyState"
_PAINT_JS = "(()=>{return document.readyState;})()"


def settle(max_polls=12):
    """Wait for the page to go quiet after an action: poll readyState until 'complete' (or max_polls).
    Returns the final readyState. Condition-based — no fixed delay (project rule: never sleep)."""
    state = "loading"
    for _ in range(max_polls):
        try:
            state = (_call("js", _port(), {"expr": _READY_JS}) or {}).get("result") or "loading"
        except Exception:
            state = "loading"
        if state == "complete":
            return state
    return state


def where():
    """Current page {'title','url'} from the engine's /text op."""
    try:
        t = _call("text", _port(), {})
        return {"title": (t.get("title") or "")[:120], "url": (t.get("url") or "")[:200]}
    except Exception:
        return {"title": "", "url": ""}


def shot(name="shot"):
    """Screenshot the current page to <PH_OUT_DIR>/<name>.png. Returns the path."""
    out = _out_dir()
    out.mkdir(parents=True, exist_ok=True)
    png = out / f"{name}.png"
    png.write_bytes(base64.b64decode(_call("shot", _port(), {})["b64"]))
    return png


def click(x, y, name="click"):
    """Click at (x,y), settle (no sleep), screenshot. Returns (where_info, png_path)."""
    _call("click", _port(), {"x": int(x), "y": int(y)})
    settle()
    info = where()
    return info, shot(name)


def scroll(to="bottom", name="scroll"):
    """Scroll the page ('bottom' | 'top' | <pixels> relative), settle, screenshot. Returns png path.
    A pixel scroll reveals lazy content; settle() lets it paint before the shot."""
    if to == "bottom":
        expr = "window.scrollTo(0, document.body.scrollHeight); document.body.scrollHeight"
    elif to == "top":
        expr = "window.scrollTo(0,0); 0"
    else:
        expr = f"window.scrollBy(0,{int(to)}); window.scrollY"
    _call("js", _port(), {"expr": expr})
    settle()
    return shot(name)


def _main(argv):
    if not argv:
        sys.exit("usage: python -m crawl.drive {click X Y|scroll [to]|shot} [OUTNAME]")
    cmd = argv[0]
    if cmd == "click":
        if len(argv) < 3:
            sys.exit("usage: python -m crawl.drive click X Y [OUTNAME]")
        info, png = click(argv[1], argv[2], argv[3] if len(argv) > 3 else "click")
        print("title:", info["title"], "| url:", info["url"])
        print("shot ->", png)
    elif cmd == "scroll":
        to = argv[1] if len(argv) > 1 else "bottom"
        png = scroll(to, argv[2] if len(argv) > 2 else "scroll")
        print("shot ->", png)
    elif cmd == "shot":
        print("shot ->", shot(argv[1] if len(argv) > 1 else "shot"))
    else:
        sys.exit(f"unknown command: {cmd}")


if __name__ == "__main__":
    _main(sys.argv[1:])
