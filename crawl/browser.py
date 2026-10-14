"""crawl.browser — attach a CDP client to the shared engine's Chrome. Zero project refs.

- `await attach(port)` starts the engine and Chrome if needed (connect.ensure, which also recovers a
  dead browser), then returns an engine.cdp.Browser on the same Chrome.
- Open your own tabs on it and close them; never stop the shared engine.

    python -m crawl.browser --sweep [--port N]    # close leaked tabs (yours and dead agents')
"""
import argparse
import asyncio

from engine import cdp, connect


async def attach(port=None):
    """An engine.cdp.Browser attached to the shared engine's Chrome, both started if down."""
    port = await asyncio.to_thread(connect.ensure, port)    # blocking: off the event loop
    ep = connect.call("cdp", port=port)
    return await cdp.attach(ep["host"], ep["port"])


def close_extra_tabs(port=None):
    """closeextra: unlabelled tabs and tabs of agents that have exited; never a live agent's (#15).
    Best effort: returns the engine's answer, or None."""
    try:
        return connect.call("closeextra", port=port)
    except connect.EngineError:
        return None


def _main(argv=None):
    ap = argparse.ArgumentParser(description="Shared-browser maintenance.")
    ap.add_argument("--sweep", action="store_true",
                    help="close leaked tabs: unlabelled ones and those of agents that have exited")
    ap.add_argument("--port", type=int, help="engine port (default PH_PORT or 8731)")
    a = ap.parse_args(argv)
    if a.sweep:
        print(close_extra_tabs(a.port))
    else:
        ap.print_help()


if __name__ == "__main__":
    _main()
