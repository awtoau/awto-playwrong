"""crawl.netblock — CDP Fetch resource-type blocking. Zero project refs.

Fail heavy sub-resource requests at the network layer so Chrome never downloads them (images/media/
fonts/css, and optionally scripts). Document/XHR/Fetch always pass so pages + their JSON/link data
still load. The caller chooses what to block via `block_types`.

Scoped per blocked type, not url_pattern="*": pausing every request and continuing the page-critical
ones serialised 20-30 round trips per page and stalled loads at readyState='loading'. Only blocked
types pause, and the handler only ever fails them.

Usage:
    blk = ResourceBlocker(tab, block_types=netblock.TEXT_ONLY)   # or TEXT_ONLY_KEEP_JS
    await blk.enable()
    ... navigate/capture ...
    await blk.disable()      # blk.n_blocked = how many requests were failed
"""

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
        patterns = [{"urlPattern": "*", "resourceType": t, "requestStage": "Request"}
                    for t in self.blocked_types]
        if not patterns:
            return
        self.tab.on("Fetch.requestPaused", self._on_paused)
        # Fetch accepts only a subset of Network.ResourceType; an exotic one refuses the whole
        # enable. Best-effort: no blocking rather than a broken crawl.
        try:
            await self.tab.send("Fetch.enable", {"patterns": patterns})
        except Exception:
            pass

    async def _on_paused(self, p):
        # Only blocked types ever reach here (scoped patterns) — always fail, never continue.
        try:
            self.n_blocked += 1
            await self.tab.send("Fetch.failRequest", {"requestId": p["requestId"],
                                                      "errorReason": "BlockedByClient"})
        except Exception:
            pass

    async def disable(self):
        self.tab.off("Fetch.requestPaused", self._on_paused)
        try:
            await self.tab.send("Fetch.disable")
        except Exception:
            pass
