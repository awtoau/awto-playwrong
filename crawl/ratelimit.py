"""crawl.ratelimit — per-host politeness + 429/503 backoff, shared by all tabs of a run.

- Per-host gate: a minimum gap between two fetches to the SAME host (default 1.5 s); other hosts
  run freely.
- Backoff: a 429/503 (or Retry-After) widens that host's gap exponentially, capped, and the url is
  requeued; each clean response relaxes it one step back toward the base.

    rl = RateLimiter(base_delay=1.5)           # one per run
    await rl.acquire(host)                     # blocks until polite
    ... fetch ...
    rl.on_response(host, status, retry_after)  # True = rate-limited: requeue, don't store the body
"""
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
            h = self._hosts[host] = _Host(self.base)
        return h

    def delay(self, host: str) -> float:
        """The host's current minimum gap, in seconds."""
        return self._h(host).delay

    async def acquire(self, host: str) -> None:
        """Block until it is polite to fetch `host`, then reserve the next slot (per-host lock, so
        two tabs on one host never fetch inside the gap)."""
        h = self._h(host)
        async with h.lock:
            wait = h.next_ok - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            h.next_ok = time.monotonic() + h.delay

    def on_response(self, host: str, status: int | None, retry_after: float | None = None) -> bool:
        """Feed a response status back. True = rate-limited (requeue, don't store the body)."""
        h = self._h(host)
        if status in (429, 503):
            h.strikes += 1
            h.delay = min(self.max, max(h.delay * self.backoff, retry_after or 0.0))
            h.next_ok = time.monotonic() + h.delay
            return True
        if h.strikes > 0 or h.delay > self.base:       # clean: relax one step, never below base
            h.strikes = 0
            h.delay = max(self.base, h.delay / self.backoff)
        return False

    def snapshot(self) -> dict:
        """Hosts currently backed off: {host: {delay, strikes}}, for the run summary."""
        return {host: {"delay": round(h.delay, 2), "strikes": h.strikes}
                for host, h in self._hosts.items() if h.delay > self.base or h.strikes}
