"""crawl.challenge — detect + clear Cloudflare "verify you are human" interstitials; spot soft-404s.

Zero project refs. The shared playwrong Chrome (nodriver, raw CDP) passes Turnstile where Playwright
fails, so most challenges self-clear on load; `solve()` handles the residual click-the-checkbox case.
`is_challenge`/`is_soft_404` are pure string checks a consumer runs on the captured title+html to
decide whether a 200 response is actually usable content.
"""

# Substrings that mark a Cloudflare / bot-wall interstitial (title or body).
CHALLENGE_MARKERS = ("just a moment", "verify you are human", "cf-chl", "challenge-platform",
                     "checking your browser", "attention required")


def is_challenge(title, html):
    """True if the page is a bot-wall interstitial rather than real content."""
    t = (title or "").lower()
    h = (html or "").lower()
    return any(k in t for k in CHALLENGE_MARKERS) or "verify you are human" in h


def is_soft_404(title, markers=("page not found", "not found", "404")):
    """True if the title looks like a soft-404 (HTTP 200 with a 'not found' template). Many CMSes
    serve a full styled 404 page at 200, so a length check misses it — the title is the tell. Pass
    site-specific `markers` (lower-case, matched as prefix or exact) when the default set is too broad."""
    t = (title or "").strip().lower()
    return any(t == m or t.startswith(m) for m in markers)


async def solve(tab, wait_after=5.0, find_timeout=15.0):
    """Click the in-iframe 'verify you are human' checkbox if present; return True once the page is
    no longer a challenge. Best-effort — safe to call on a page that isn't challenged (returns True)."""
    try:
        el = await tab.find("verify you are human", best_match=True, timeout=find_timeout)
        if el:
            await el.mouse_click()
            await tab.sleep(wait_after)
    except Exception:
        pass
    try:
        title = await tab.evaluate("document.title")
        html = await tab.get_content()
        return not is_challenge(title, html)
    except Exception:
        return False
