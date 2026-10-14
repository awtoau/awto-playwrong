# Lessons

Design decisions that came from real failures. Each issue number links to the evidence.

## Playwright is the Cloudflare Turnstile tell
- A Playwright-launched Chrome, even with anti-detect flags, the real Chrome channel and a fixed
  UA/geometry, is served a **dead** challenge: no iframe renders, so there's nothing to click.
- A real Chrome over raw CDP gets the interactive widget and passes.
- Consequence: no Playwright, no chromedriver, no `--enable-automation`, never `Runtime.enable`.
  Browser runs headed.

## Our own CDP client, not nodriver (#41)
- nodriver: AGPL, unimportable on Python 3.14+ as released, and its internals leaked per tab (#23).
- `engine/cdp.py`: one websocket, flattened sessions, only the commands the engine uses.

## The Turnstile checkbox lives in an out-of-process iframe (#45)
- Even with `--disable-features=IsolateOrigins,site-per-process`, the challenges.cloudflare.com
  iframe is its own target, and `DOM.getDocument(pierce)` has no `contentDocument` for it.
- Its target id equals the `frameId` of the page's `<iframe>` node. Attach to it, search its DOM,
  and click on **its** session, in its own viewport coordinates.
- `scripts/turnstile_click_test.py` proves this against Cloudflare's test sitekey.

## The engine's own reads go in an isolated world (#49)
- A page can hook `document.title`, `location` and friends in the main world.
- readyState, title, location, the owner label and render polling run in a
  `Page.createIsolatedWorld` context. Only the caller's `js` op runs in the main world.

## The DevTools port is the browser (#47, #52)
- `--remote-debugging-port=0` silently set `navigator.webdriver = true`. Launch on a concrete free
  port instead.
- `--remote-allow-origins=*` let any web page drive the browser. The flag now allows only the
  DevTools frontend's own origin.

## A diagnostic must not be optimistic (#8)
- `/status` once reported `alive` from a stale handle, and `doctor.py` certified a dead engine as
  healthy for three days.
- `alive` is now a live CDP round trip, and doctor drives a real op.

## Many callers, one browser: tags, not the active tab (#14, #15, #22)
- Ops on the "active tab" meant one caller's read returned another caller's page, with no error.
- Every caller tags its tab. Cleanup is owner-scoped.

## Memory: measure, cap, fail one tab (#23, #28)
- A leak reached 39.5 GB and forced a hard reset.
- systemd scope with `OOMPolicy=continue`: running out kills one renderer, and that tab fails by
  name.

## Clean shutdown over the port, never `pkill`
- `pkill` orphans Chrome and loses the cleared session. Two browsers on one profile cause solve
  failures.

## In-memory response bodies get evicted
- `Network.getResponseBody` races eviction under site isolation ("No resource with given
  identifier").
- For image bytes: read Chrome's disk cache (`assets.cache`) or re-download.

## A status code doesn't tell you the content is right
- A 200 can carry a login page, a PDF can be 10 of 45 pages, and DuckDuckGo answers HTTP 202 with a
  CAPTCHA.
- Hence magic-byte checks, page counts, sha256 and `Content-Length` verification on every download.
