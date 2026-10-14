"""crawl.render — let a client-rendered page show its content before capture. Zero project refs.

After navigating: wait_ready() (readyState), dismiss_overlays() (consent walls), wait_for_render()
(visible text mounted). All page reads run in an isolated world (#49).
"""
import asyncio
import time

_BODY_LEN = "document.body ? document.body.innerText.length : 0"
# Polls a below-min_chars length must hold before wait_for_render accepts it as final: a client
# render often pauses between script load and mount.
_STABLE_BELOW_MIN = 3

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
        clicked = await tab.evaluate(DISMISS_JS, isolated=True)   # DOM clicks; page can't hook it
        try:
            for ty in ("keyDown", "keyUp"):
                await tab.send("Input.dispatchKeyEvent", {"type": ty, "key": "Escape",
                                                          "windowsVirtualKeyCode": 27})
        except Exception:
            pass
        return clicked
    except Exception:
        return None


async def wait_for_render(tab, min_chars=200, max_wait=8.0, poll=0.5):
    """Poll the visible text length until it settles: one unchanged poll at or above min_chars,
    _STABLE_BELOW_MIN below it; else max_wait. Returns the last length (0 if unreadable)."""
    last, same, t0 = -1, 0, time.monotonic()
    while time.monotonic() - t0 < max_wait:
        n = await body_text_len(tab)
        same = same + 1 if n == last else 0
        if same >= (1 if n >= min_chars else _STABLE_BELOW_MIN):
            return n
        last = n
        await asyncio.sleep(poll)
    return max(last, 0)


async def body_text_len(tab):
    """Visible body-text length; 0 when unreadable."""
    try:
        n = await tab.evaluate(_BODY_LEN, isolated=True)
        return int(n) if isinstance(n, int | float) else 0
    except Exception:
        return 0


async def wait_ready(tab, max_wait=10.0, poll=0.25):
    """readyState "interactive" or "complete" (Page.navigate returns while still "loading", and
    reading then gets an empty stub), else max_wait. Returns the last state."""
    t0 = time.monotonic()
    state = "loading"
    while time.monotonic() - t0 < max_wait:
        try:
            state = await tab.evaluate("document.readyState", isolated=True) or "loading"
        except Exception:
            state = "loading"
        if state in ("interactive", "complete"):
            return state
        await asyncio.sleep(poll)
    return state
