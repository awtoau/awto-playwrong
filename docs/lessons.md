# Lessons (hard-won, from the powderhounds build)

## Playwright is the Cloudflare Turnstile tell
A Playwright-launched Chrome — even with anti-detect flags, real Chrome channel, fixed UA/geometry —
gets served a DEAD Turnstile challenge: 0 iframes ever render, the checkbox never appears, so it can
never be clicked. Raw CDP (no Playwright instrumentation) gets the real interactive widget, finds
"verify you are human" in the cross-origin iframe, clicks it, and PASSES. The detected component is
Playwright itself. => use the engine for anything behind Cloudflare.

## Site isolation must be OFF to reach the challenge iframe
`--disable-features=IsolateOrigins,site-per-process` (engine/cdp.py) + real Chrome. Keeps the
cross-origin Turnstile iframe in-process, so `DOM.getDocument(pierce)` reaches its checkbox.

## Torn-frame guard (image/cam grabs)
A frame grabbed mid-write is torn (partial JPEG). Validate: JPEG ends FFD9, PNG ends IEND; if torn,
wait ~1.5s and retry (source finishes its write). Never store a half-updated frame.

## Clean shutdown over the command port — never pkill
The server exposes /shutdown. pkill orphans the Chrome window + loses session/clearance. Two browsers
running at once = orphan-window pileups + solve failures; run ONE.

## Python 3.14t (free-threaded)
Sync Playwright SEGFAULTS under 3.14t (greenlet/sync API). Use async Playwright or raw CDP.

## Our own CDP client, not nodriver
- nodriver: AGPL, unimportable on Python 3.14+ as released, and its internals leaked per tab (#23).
- `engine/cdp.py` replaced the vendored copy (#41): ~30 commands, one socket, flat sessions.

## In-memory response bodies get evicted
CDP get_response_body races eviction under site-isolation ("No resource with given identifier").
For image bytes: grab on loadingFinished, or read Chrome's disk Simple Cache, or just re-download the
URL (small images, server-cached). Don't assume in-memory capture is reliable.
