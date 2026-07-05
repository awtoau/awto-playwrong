# awto-playwrong

**One place to go when an app or agent needs browser automation.** A generic, shareable browser-
capture engine — port-based, so multiple agents/apps can hit the same running infrastructure. It
documents **several methods** (plain Playwright style + the "powders" nodriver style that beats
Cloudflare Turnstile), so you pick the right one for the job.

Extracted from the powderhounds project, where it was built + battle-tested (it beats Cloudflare
Turnstile on a real site and recovered thousands of bot-blocked pages).

## Why this exists
Browser automation kept getting rebuilt per-project. This is the shared home: a **running server**
you drive over **IP:port** — `goto a page → get back {html, cookies, status, screenshot, metadata}` —
that any number of apps/agents can share. No DB, no project specifics: pure capture. Wire your own
data layer on top (the project keeps its DB code; this stays generic).

## The methods (pick one)
| Method | What | When to use |
|---|---|---|
| **engine/ (nodriver, "powders" style)** ⭐ | raw-CDP real Chrome via [nodriver]; **beats Cloudflare Turnstile** (Playwright is the detection tell — see docs) | anything behind Cloudflare/Turnstile/bot-protection; the default |
| **methods/playwright-\*** | classic Playwright server + client | sites with no bot protection; familiar Playwright API |

### engine/ — the recommended capture server
- `engine/server.py` — persistent **headed real Chrome** (nodriver), driven over HTTP on a port.
  Ops: `goto`, `solve` (Turnstile), `text` (html), `shot` (screenshot, base64). Stable, browser
  stays alive across requests; clean shutdown over the port.
- `engine/client.py` — the port client (`goto/solve/shot/text/...`).
- `engine/solve.py` — standalone Turnstile solve (find "verify you are human" inside the cross-origin
  iframe + click).
- `vendor/nodriver` — patched nodriver (fixes a non-UTF-8 byte that breaks import under Python 3.14t
  free-threaded; upstream issue ultrafunkamsterdam/nodriver#35).

### methods/ — alternative / historical
- `playwright-server.py` + `playwright-ctl.py` — the earlier Playwright-based server/client. **Note:
  Playwright is detectable by Cloudflare Turnstile** (it gets served a dead, never-rendering
  challenge) — kept for non-protected sites + reference. Use the nodriver engine for anything behind
  Cloudflare.
- `playwright-crawl.py` — a one-shot Playwright crawler (headed).

## Usage (engine)
```
# 1. start the server (headed real Chrome, stays alive)
PYTHONPATH=vendor python engine/server.py        # listens on PH_PORT (default 8731)

# 2. drive it over the port (from any app/agent/script)
python engine/client.py goto https://example.com
python engine/client.py solvecf                  # clear a Turnstile challenge
python engine/client.py text                     # get the page HTML
python engine/client.py shot frame.png           # screenshot
```
Or POST directly: `POST http://127.0.0.1:8731/goto {"url": "..."}` → returns the capture.

## The capture contract (what you get back)
`goto` / `capture` returns: **html, title, status, cookies, screenshot (base64), and metadata**
(timing, passed-challenge flag, request counts). Wire your own storage/DB on top — this engine never
touches a database.

## Key lessons baked in (see docs/)
- **Playwright is the Turnstile tell.** Cloudflare detects Playwright's CDP instrumentation and serves
  a dead challenge; **nodriver** (raw CDP, no Playwright) gets the real interactive widget and passes.
- **Site-isolation flags** + real Chrome channel matter for reaching cross-origin challenge iframes.
- **Torn-frame guard** for image/cam grabs (validate JPEG ends FFD9 / PNG ends IEND; retry on
  mid-write).
- **Clean shutdown over the command port** — never pkill the browser.
- **Python 3.14t free-threaded** — sync Playwright segfaults; use async / nodriver. Vendored nodriver
  is patched for it.

## Status
Public on GitHub at https://github.com/awtoau/awto-playwrong.
Local working checkout remains on your machine.

_Multi-method browser automation: nodriver engine (Turnstile-beating) + Playwright methods, driven
over a port, capture-only (no DB). The shared home for any app/agent that needs a browser._
