"""crawl.netblock — CDP Fetch resource-type blocking. Zero project refs.

Fail heavy sub-resource requests at the network layer so Chrome never downloads them (images/media/
fonts/css, and optionally scripts). Document/XHR/Fetch always pass so pages + their JSON/link data
still load. The caller chooses what to block via `block_types`.

WHY the pattern is scoped per-blocked-type (not url_pattern="*"):
    An earlier version intercepted EVERY request at the REQUEST stage and, in the RequestPaused
    handler, awaited fail_request OR continue_request. On a real page (20-30 sub-resources) that
    serialises 20-30 `await tab.send()` round-trips through the single CDP event pump — the page's
    load lifecycle stalls at readyState='loading' forever and get_content() returns an empty stub.
    (nd_crawl's own image capture warns: "MUST continue_request every time or the page stalls" — it
    gets away with it only because it pauses IMAGE responses alone, a tiny set.)
    Fix: register ONE Fetch pattern PER BLOCKED resource_type. Only those types ever pause, and the
    handler ONLY fails them (never continues) — the page-critical Document/Script/XHR/Fetch requests
    are never intercepted, so nothing serialises behind the pump and the load completes normally.

Usage:
    blk = ResourceBlocker(tab, block_types=netblock.TEXT_ONLY)   # or TEXT_ONLY_KEEP_JS
    await blk.enable()
    ... navigate/capture ...
    await blk.disable()      # blk.n_blocked = how many requests were failed
"""
from nodriver import cdp

# Heavy types the CDP *Fetch* filter accepts (Network enumerates more, but Fetch rejects the exotic
# ones like TextTrack/CSPViolationReport). Document/XHR/Fetch stay unblocked — they carry the page
# and its link/JSON data.
TEXT_ONLY_KEEP_JS = ["Image", "Media", "Font", "Stylesheet", "Ping", "Prefetch"]
# Same, plus Script (the leanest — for sites that don't need JS to render/link).
TEXT_ONLY = TEXT_ONLY_KEEP_JS + ["Script"]


class ResourceBlocker:
    """Block the given resource types on a tab via CDP Fetch interception. Best-effort; never raises
    into the caller's fetch loop. Only the blocked types are intercepted (one pattern each), so the
    page's own Document/Script/XHR requests flow untouched and the load never stalls."""

    def __init__(self, tab, block_types=None):
        self.tab = tab
        self.blocked_types = list(block_types if block_types is not None else TEXT_ONLY)
        self.n_blocked = 0

    async def enable(self):
        self.tab.add_handler(cdp.fetch.RequestPaused, self._on_paused)
        patterns = []
        for t in self.blocked_types:
            rt = getattr(cdp.network.ResourceType, _RT_ATTR.get(t, t.upper()), None)
            if rt is None:
                continue
            patterns.append(cdp.fetch.RequestPattern(
                url_pattern="*", resource_type=rt, request_stage=cdp.fetch.RequestStage.REQUEST))
        if not patterns:
            return
        # CDP's Fetch filter accepts only a subset of the Network ResourceType enum. TEXT_ONLY* are
        # pre-trimmed to accepted types; if a caller passes an exotic one CDP refuses the whole
        # enable() — treat that as best-effort (no blocking) rather than breaking the crawl.
        try:
            await self.tab.send(cdp.fetch.enable(patterns=patterns))
        except Exception:
            pass

    async def _on_paused(self, ev):
        # Only blocked types ever reach here (scoped patterns) — always fail, never continue.
        try:
            self.n_blocked += 1
            await self.tab.send(cdp.fetch.fail_request(
                request_id=ev.request_id, error_reason=cdp.network.ErrorReason.BLOCKED_BY_CLIENT))
        except Exception:
            pass

    async def disable(self):
        try:
            await self.tab.send(cdp.fetch.disable())
        except Exception:
            pass


# Friendly name -> CDP ResourceType enum member name (nodriver SCREAMING_CASE).
_RT_ATTR = {
    "Image": "IMAGE", "Media": "MEDIA", "Font": "FONT", "Stylesheet": "STYLESHEET",
    "Script": "SCRIPT", "Ping": "PING", "Prefetch": "PREFETCH",
}
