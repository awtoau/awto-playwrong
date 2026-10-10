"""aclose_race_test.py — two concurrent Connection.aclose() calls must not raise (#29).

- Vendored nodriver checked `self.socket`, then awaited `socket.close()`; a second aclose() running
  meanwhile set `self.socket = None`, so the first hit `None.wait_closed()` -> AttributeError.
- Fake socket whose close() yields, so the interleaving happens every run. No browser, no network.

    python scripts/aclose_race_test.py

Results: tmp/logs/aclose-race-test.log
"""
import asyncio
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "vendor"))
from nodriver.core.connection import Connection  # noqa: E402

LOGDIR = os.path.join(REPO, "tmp", "logs")
LOG = os.path.join(LOGDIR, "aclose-race-test.log")


class YieldingSocket:
    """Stands in for a websocket: close() yields to the loop, like a real close handshake."""

    def __init__(self):
        self.closed = 0

    async def close(self):
        for _ in range(3):      # longer than wait_closed: one aclose finishes mid-way through the other
            await asyncio.sleep(0)
        self.closed += 1

    async def wait_closed(self):
        await asyncio.sleep(0)


async def race():
    conn = Connection.__new__(Connection)       # no __init__: it wants a target and a parent
    conn.socket, conn._listener_task, conn._mapper = YieldingSocket(), None, {}
    sock = conn.socket

    async def late():           # one loop step behind: still sees the socket, finishes after
        await asyncio.sleep(0)
        await conn.aclose()
    results = await asyncio.gather(conn.aclose(), late(), return_exceptions=True)
    return results, sock, conn


def main():
    os.makedirs(LOGDIR, exist_ok=True)
    results, sock, conn = asyncio.run(race())
    errors = [r for r in results if isinstance(r, BaseException)]
    lines = [
        f"[{'PASS' if not errors else 'FAIL'}] concurrent aclose() raises nothing"
        + (f" — {errors[0]!r}" if errors else ""),
        f"[{'PASS' if sock.closed == 1 else 'FAIL'}] the socket is closed exactly once — {sock.closed}",
        f"[{'PASS' if conn.socket is None else 'FAIL'}] socket attribute cleared",
    ]
    failed = sum(line.startswith("[FAIL") for line in lines)
    lines.append(f"\n{len(lines) - failed} passed, {failed} failed")
    out = "\n".join(lines)
    print(out)
    with open(LOG, "w") as f:
        f.write(out + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
