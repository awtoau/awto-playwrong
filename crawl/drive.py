"""crawl.drive — hand-drive a page on the shared engine: click, scroll, screenshot each step.

- Drives its own tab ("drive"), never the shared active tab; the tab persists across runs.
- After an action it polls document.readyState until "complete", bounded by SETTLE.

    python -m crawl.drive goto URL
    python -m crawl.drive click X Y [OUTNAME]
    python -m crawl.drive scroll [bottom|top|<px>] [OUTNAME]
    python -m crawl.drive shot [OUTNAME]
Env: PH_PORT (engine port), PH_OUT_DIR (screenshot dir, default ./tmp)

    from crawl import drive
    drive.goto("https://awto.au"); drive.click(640, 120, "step2"); drive.where()
"""
import base64
import os
import pathlib
import sys
import time

from engine import connect

TAG, OWNER = "drive", "drive@cli"     # no pid in the owner: closeextra never sweeps it
# After a click/scroll: a page that reacts with a load reaches "complete" within seconds; past
# SETTLE we screenshot whatever is there.
SETTLE, SETTLE_POLL = 5.0, 0.1


def _op(name, **body):
    """An engine op on the drive tab, opening it on first use."""
    try:
        return connect.op(name, tab=TAG, **body)
    except connect.EngineError as e:
        if "no such tab" not in str(e):
            raise
        connect.op("newtab", url="about:blank", tag=TAG, owner=OWNER)
        return connect.op(name, tab=TAG, **body)


def settle():
    """Poll readyState until "complete" or SETTLE elapses; returns the last state."""
    state, t0 = "loading", time.monotonic()
    while time.monotonic() - t0 < SETTLE:
        state = _op("js", expr="document.readyState").get("result") or "loading"
        if state == "complete":
            break
        time.sleep(SETTLE_POLL)
    return state


def where():
    """The drive tab's {'title', 'url'}."""
    t = _op("text")
    return {"title": (t.get("title") or "")[:120], "url": (t.get("url") or "")[:200]}


def shot(name="shot"):
    """Screenshot the drive tab to <PH_OUT_DIR>/<name>.png; returns the path."""
    out = pathlib.Path(os.environ.get("PH_OUT_DIR", "tmp"))
    out.mkdir(parents=True, exist_ok=True)
    png = out / f"{name}.png"
    png.write_bytes(base64.b64decode(_op("shot", timeout=connect.GOTO_TIMEOUT)["b64"]))
    return png


def goto(url):
    """Navigate the drive tab; returns where()."""
    _op("goto", url=url, timeout=connect.GOTO_TIMEOUT)
    return where()


def click(x, y, name="click"):
    """Click at viewport (x, y), settle, screenshot. Returns (where(), png path)."""
    _op("click", x=int(x), y=int(y))
    settle()
    return where(), shot(name)


def scroll(to="bottom", name="scroll"):
    """Scroll to 'bottom', 'top', or by <px>; settle; screenshot. Returns the png path."""
    if to == "bottom":
        expr = "window.scrollTo(0, document.body.scrollHeight); document.body.scrollHeight"
    elif to == "top":
        expr = "window.scrollTo(0,0); 0"
    else:
        expr = f"window.scrollBy(0,{int(to)}); window.scrollY"
    _op("js", expr=expr)
    settle()
    return shot(name)


def _main(argv):
    usage = "usage: python -m crawl.drive {goto URL|click X Y|scroll [to]|shot} [OUTNAME]"
    if not argv:
        sys.exit(usage)
    cmd, args = argv[0], argv[1:]
    if cmd == "goto" and args:
        print(goto(args[0]))
    elif cmd == "click" and len(args) >= 2:
        info, png = click(args[0], args[1], args[2] if len(args) > 2 else "click")
        print("title:", info["title"], "| url:", info["url"])
        print("shot ->", png)
    elif cmd == "scroll":
        print("shot ->", scroll(args[0] if args else "bottom", args[1] if len(args) > 1 else "scroll"))
    elif cmd == "shot":
        print("shot ->", shot(args[0] if args else "shot"))
    else:
        sys.exit(usage)


if __name__ == "__main__":
    _main(sys.argv[1:])
