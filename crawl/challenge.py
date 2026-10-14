"""crawl.challenge — bot-wall detection and the click-the-checkbox solve; soft-404s. Zero project refs.

`is_challenge` is engine.connect's one check (#53); `is_soft_404` is a title test.
"""
import asyncio

from engine.connect import title_is_challenge as is_challenge  # noqa: F401  (re-exported API)


def is_soft_404(title, markers=("page not found", "not found", "404")):
    """A 200 page whose title says not-found (many CMSes serve a styled 404 at 200). Pass site
    `markers` (lower-case, matched as prefix or exact) when the default set is too broad."""
    t = (title or "").strip().lower()
    return any(t == m or t.startswith(m) for m in markers)


async def solve(tab, wait_after=5.0, find_timeout=15.0):
    """Click the "verify you are human" checkbox if present; True once the page is no longer a
    challenge. Safe on a page that isn't challenged.
    - find_timeout: the widget can render well after load (seconds).
    - wait_after: Turnstile verifies and reloads after a click (~3 s)."""
    try:
        hit = await tab.find_text("verify you are human", timeout=find_timeout)
        if hit:
            await hit.click()
            await asyncio.sleep(wait_after)
    except Exception:
        pass
    try:
        title = await tab.evaluate("document.title", isolated=True)
        return not is_challenge(title, await tab.content())
    except Exception:
        return False
