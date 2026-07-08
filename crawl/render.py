"""crawl.render — get a JS-SPA page to actually show its content before capture. Zero project refs.

Modern sites ship a script bundle + build the DOM client-side, often behind a cookie-consent wall.
Capturing too early yields an empty shell. This module: dismisses the consent popup, then polls until
the visible text mounts (or settles / times out). Consumers call these on the tab after navigating.
"""

# Accept-buttons for the common consent platforms + a generic text matcher (EN + DE, for .at/.de).
DISMISS_JS = r"""(() => {
  let clicked = [];
  const SEL = [
    '#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll',   // Cookiebot
    '#CybotCookiebotDialogBodyButtonAccept',
    '#onetrust-accept-btn-handler',                              // OneTrust
    '.cc-allow', '.cc-dismiss', '.cc-btn.cc-allow',              // cookieconsent (osano)
    'button[aria-label*="accept" i]', '.usercentrics-button',    // Usercentrics
    '.didomi-continue-without-agreeing', '#didomi-notice-agree-button', // Didomi
    '.iubenda-cs-accept-btn',                                    // iubenda
    '#truste-consent-button',                                    // TrustArc
    '[data-testid="uc-accept-all-button"]',
  ];
  for (const s of SEL) { const b = document.querySelector(s);
    if (b) { try { b.click(); clicked.push(s); } catch(e){} } }
  const RX = /^(accept all|accept|agree|allow all|got it|i agree|ok|zustimmen|akzeptieren|alle akzeptieren|einverstanden|verstanden)$/i;
  document.querySelectorAll('button, a, [role="button"]').forEach(b => {
    const t = (b.textContent||'').trim();
    if (t && RX.test(t)) { try { b.click(); clicked.push('text:'+t.slice(0,20)); } catch(e){} }
  });
  return clicked;
})()"""


async def dismiss_overlays(tab):
    """Click cookie-consent 'accept' + Escape leftover modals so the real content shows. Best-effort."""
    try:
        clicked = await tab.evaluate(DISMISS_JS)
        try:
            from nodriver import cdp as _cdp
            await tab.send(_cdp.input_.dispatch_key_event(type_="keyDown", key="Escape",
                           windows_virtual_key_code=27))
            await tab.send(_cdp.input_.dispatch_key_event(type_="keyUp", key="Escape",
                           windows_virtual_key_code=27))
        except Exception:
            pass
        return clicked
    except Exception:
        return None


async def wait_for_render(tab, min_chars=200, max_wait=8.0, poll=0.5):
    """Poll until the page's visible body text reaches min_chars OR stops growing OR max_wait. For
    JS-SPA sites that build the DOM client-side (else we'd capture the empty pre-render shell).
    Returns the final body-text length. Best-effort."""
    import time as _t
    last = -1
    t0 = _t.monotonic()
    while _t.monotonic() - t0 < max_wait:
        try:
            n = await tab.evaluate("document.body ? document.body.innerText.length : 0")
            n = int(n) if isinstance(n, (int, float)) else 0
        except Exception:
            n = 0
        if n >= min_chars and n == last:   # reached the bar AND settled
            return n
        if n == last and n > 0:            # settled below the bar — no more coming
            return n
        last = n
        await tab.sleep(poll)
    return last


async def body_text_len(tab):
    """Current visible body-text length (used to decide if a page is empty). Best-effort -> 0."""
    try:
        return int(await tab.evaluate("document.body ? document.body.innerText.length : 0") or 0)
    except Exception:
        return 0


async def wait_ready(tab, max_wait=10.0, poll=0.25):
    """Wait until document.readyState is 'interactive' or 'complete' before capturing. CRITICAL: some
    driver navigations return while the doc is still 'loading' — grabbing get_content() then yields a
    partial stub (empty <body>), which parses to near-zero text. Poll readyState first, THEN the
    caller can wait_for_render for client-rendered content. Returns the final readyState."""
    import time as _t
    t0 = _t.monotonic()
    state = "loading"
    while _t.monotonic() - t0 < max_wait:
        try:
            state = await tab.evaluate("document.readyState") or "loading"
        except Exception:
            state = "loading"
        if state in ("interactive", "complete"):
            return state
        await tab.sleep(poll)
    return state
