"""crawl.ratelimit — per-host politeness + 429/503 backoff for the crawler. Zero project refs.

The crawler runs many tabs in parallel across hosts (host-affinity). Without a limiter, several tabs
can pile onto one origin and trip its rate limit (HTTP 429 / "Too many requests") — which is what bit
wbtools. This adds two things, shared across all tabs of a run:

  1. PER-HOST GATE — a minimum gap between two fetches to the SAME host (default 1.5s). Tabs on other
     hosts are unaffected, so throughput across many sites stays high; a single origin is never hammered.
  2. 429/503 BACKOFF — when a host returns 429/503 (or Retry-After), its gate widens exponentially
     (1.5s → 3 → 6 → 12 … capped) and the URL is requeued. On the next clean fetch the gate relaxes.

Async, lock-per-host. `acquire(host)` waits until it's polite to fetch that host; `on_response(host,
status, retry_after)` feeds status back so the gate adapts. `note_slow(host)` lets the caller widen a
host that's merely getting slow (climbing latency) before it hard-fails.

Usage in a worker:
    rl = RateLimiter(base_delay=1.5)          # one shared instance per run
    await rl.acquire(host)                     # blocks until polite
    ... fetch ...
    rl.on_response(host, status, retry_after)  # 429/503 -> widens; 2xx -> relaxes
"""
from __future__ import annotations
import asyncio
import time


class _Host:
    __slots__ = ("lock", "next_ok", "delay", "strikes")

    def __init__(self, base):
        self.lock = asyncio.Lock()
        self.next_ok = 0.0     # monotonic time this host may be fetched again
        self.delay = base      # current min gap for this host (grows on 429)
        self.strikes = 0       # consecutive rate-limit hits


class RateLimiter:
    def __init__(self, base_delay=1.5, max_delay=60.0, backoff=2.0):
        self.base = float(base_delay)
        self.max = float(max_delay)
        self.backoff = float(backoff)
        self._hosts: dict[str, _Host] = {}

    def _h(self, host: str) -> _Host:
        h = self._hosts.get(host)
        if h is None:
            h = self._hosts.setdefault(host, _Host(self.base))
        return h

    async def acquire(self, host: str) -> None:
        """Block until it is polite to fetch `host`, then reserve the next slot. Per-host serialised so
        two tabs on the same host never fetch inside the delay window; different hosts run freely."""
        h = self._h(host)
        async with h.lock:
            now = time.monotonic()
            wait = h.next_ok - now
            if wait > 0:
                await asyncio.sleep(wait)
            # reserve the slot: the NEXT fetch to this host must wait `delay` from now.
            h.next_ok = time.monotonic() + h.delay

    def on_response(self, host: str, status: int | None, retry_after: float | None = None) -> bool:
        """Feed the response status back. Returns True if the host was RATE-LIMITED (caller should
        requeue the URL and NOT treat the body as real content). 2xx relaxes the gate a step."""
        h = self._h(host)
        if status in (429, 503):
            h.strikes += 1
            # honour Retry-After if given, else exponential backoff, capped.
            grow = h.delay * self.backoff
            h.delay = min(self.max, max(grow, retry_after or 0.0))
            h.next_ok = time.monotonic() + h.delay
            return True
        # clean response — relax one step toward base (but never below base).
        if h.strikes > 0 or h.delay > self.base:
            h.strikes = 0
            h.delay = max(self.base, h.delay / self.backoff)
        return False

    def note_slow(self, host: str) -> None:
        """A host whose latency is climbing (not yet 429) — nudge its gate up a little, pre-emptively."""
        h = self._h(host)
        h.delay = min(self.max, h.delay * 1.3)

    def snapshot(self) -> dict:
        """Current per-host delay/strikes — for the run summary."""
        return {host: {"delay": round(h.delay, 2), "strikes": h.strikes}
                for host, h in self._hosts.items() if h.delay > self.base or h.strikes}
