"""shared_browser.py — a single persistent browser that many processes ATTACH to.

Generic + project-agnostic: NO identifying data, NO hardcoded paths, NO block lists. Everything a
caller needs to customise (extra Chrome args, marker location) is passed in. One process launches
the browser here; other processes attach and open their OWN tab, so N crawls/tools share ONE Chrome
(one window, no competing browsers, no orphans).

nodriver detail: passing host+port to uc.start() means CONNECT to an existing browser (not launch).
So the launcher lets nodriver pick the debug port, reads it back, and publishes it in a small marker
JSON; attachers read the marker and connect to that host/port.

Marker JSON shape: {"host": str, "port": int, "extra": <caller-supplied dict>}. `extra` is opaque to
this module — callers put whatever they need there (e.g. a flag recording that blocking is on) and
read it back on attach to make their own safety decisions.

Typical use:
    # launcher process
    import shared_browser, nodriver as uc
    browser = await shared_browser.launch(browser_args=[...my flags...], extra={"block": True})
    # ... idle forever ...

    # attaching process
    browser, tab = await shared_browser.attach()   # opens its own tab
    info = shared_browser.read_marker()             # inspect extra, decide, etc.
"""
import os
import json
import tempfile

import nodriver as uc


def default_marker_path() -> str:
    """A neutral marker location (OS temp dir). Override via SHARED_BROWSER_MARKER or the arg."""
    return os.environ.get(
        "SHARED_BROWSER_MARKER",
        os.path.join(tempfile.gettempdir(), "shared_browser.json"),
    )


def write_marker(host: str, port: int, extra: dict | None = None, marker_path: str | None = None) -> str:
    path = marker_path or default_marker_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"host": host, "port": int(port), "extra": extra or {}}, f, indent=2)
    return path


def read_marker(marker_path: str | None = None) -> dict | None:
    """Return the marker dict {host, port, extra} or None if no shared browser is published."""
    path = marker_path or default_marker_path()
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def remove_marker(marker_path: str | None = None) -> None:
    path = marker_path or default_marker_path()
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except Exception:
        pass


async def launch(browser_args=None, headless: bool = False, extra: dict | None = None,
                 marker_path: str | None = None):
    """Launch THE shared browser and publish a marker so others can attach. Returns the Browser.
    The caller owns the lifecycle (typically: launch, then idle forever; remove_marker on shutdown).
    browser_args/extra/marker_path are all caller-supplied — this module hardcodes nothing."""
    browser = await uc.start(headless=headless, browser_args=list(browser_args or []))
    await browser.get("about:blank")
    host = browser.config.host or "127.0.0.1"
    port = browser.config.port                    # the debug port nodriver actually launched on
    write_marker(host, port, extra=extra, marker_path=marker_path)
    return browser


async def attach(marker_path: str | None = None):
    """Attach to the shared browser named by the marker and open OUR OWN tab.
    Returns (browser, tab). Raises RuntimeError if no shared browser is published.
    Does NOT stop the shared browser — callers must not close it (other tabs may be in use)."""
    m = read_marker(marker_path)
    if not m:
        raise RuntimeError(
            f"no shared browser marker at {marker_path or default_marker_path()} — "
            f"start one with shared_browser.launch() first"
        )
    # host+port set => nodriver connects to the existing browser instead of launching one.
    browser = await uc.start(host=m["host"], port=int(m["port"]))
    tab = await browser.get("about:blank", new_tab=True)
    return browser, tab
