# Agent API — connect to awto-playwrong (and start it if needed)

How an app or agent uses the shared capture engine: it's an **HTTP server on a port**; you POST ops
and get JSON back. If the server isn't running, start it first. No SDK needed — plain HTTP.

## TL;DR
```
Base URL:  http://127.0.0.1:8731     (PH_PORT env overrides the port)
Check up:  GET  /status              -> {"alive": true|false}
Drive:     POST /goto   {"url": "..."}     -> {status, url, title}
           POST /solve  {"tries": 20}      -> {passed, iter}     (clear Cloudflare Turnstile)
           POST /text   {}                 -> {html, title, url}
           POST /shot   {}                 -> {b64}              (PNG screenshot, base64)
           POST /clearcookies {}           -> {cleared}
           GET  /frame                     -> image/png          (latest screenshot bytes)
           POST /shutdown {}               -> {ok}
```

## Connect, auto-starting the server if needed
The pattern: ping `/status`; if it's not reachable, launch `engine/server.py` and wait for the port.

```python
import os, sys, json, time, subprocess, urllib.request
REPO = "$REPO_ROOT"
PORT = int(os.environ.get("PH_PORT", "8731"))
BASE = f"http://127.0.0.1:{PORT}"

def up():
    try:
        urllib.request.urlopen(f"{BASE}/status", timeout=3); return True
    except Exception:
        return False

def ensure_server():
    """Start the capture server if it isn't already running, then wait for the port."""
    if up(): return
    subprocess.Popen([sys.executable, f"{REPO}/engine/server.py"],
                     env={**os.environ, "PYTHONPATH": f"{REPO}/vendor", "PH_PORT": str(PORT)},
                     stdout=open("/tmp/playwrong-server.log", "a"), stderr=subprocess.STDOUT)
    for _ in range(60):                 # wait up to ~30s for it to bind
        if up(): return
        time.sleep(0.5)
    raise RuntimeError("playwrong server did not start")

def call(op, **body):
    req = urllib.request.Request(f"{BASE}/{op}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=120).read())

# --- usage ---
ensure_server()                                  # start if needed
call("goto", url="https://example.com")           # navigate
# behind Cloudflare? clear the challenge once; the cleared session is reused:
if "just a moment" in call("text")["title"].lower():
    call("solve", tries=20)
page = call("text")                               # {html, title, url}
shot = call("shot")["b64"]                         # base64 PNG
```

Shell equivalent (the bundled client):
```
PYTHONPATH=$REPO_ROOT/vendor \
  python $REPO_ROOT/engine/server.py &     # start (headed real Chrome)
python $REPO_ROOT/engine/client.py goto https://example.com
python .../engine/client.py solvecf      # solve Turnstile
python .../engine/client.py text         # html
python .../engine/client.py shutdown     # clean stop (never pkill the browser)
```

## Endpoints (the real contract — nodriver engine/server.py)
| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/status` | — | `{alive: bool}` |
| POST | `/goto` | `{url}` | `{status, url, title}` — navigates (2s settle) |
| POST | `/solve` | `{tries?}` | `{passed, iter}` — finds + clicks the Turnstile "verify you are human" iframe, polls until clear |
| POST | `/text` | — | `{html, title, url}` — current page |
| POST | `/shot` | — | `{b64}` — PNG screenshot base64 |
| GET | `/frame` | — | `image/png` — latest screenshot bytes (for live viewing) |
| POST | `/clearcookies` | — | `{cleared}` |
| GET | `/markers`, POST `/setmarkers` | — | overlay markers (for the /viz debug page) |
| GET | `/viz` | — | side-by-side debug viewer (mirror + overlay) |
| POST | `/shutdown` | — | `{ok}` — clean stop |

## Notes for agents
- **One browser, shared.** The server holds ONE headed Chrome, alive across requests, so the cleared
  Turnstile session persists — solve once, many agents/calls reuse it. Don't launch a second browser
  (causes orphan-window conflicts).
- **Capture-only, no DB.** You get html/cookies/screenshot back; store it yourself. This engine never
  touches a database.
- **Cookies:** read via the page after `goto` (the cleared cf_clearance is on the context). A
  `cookies` field on the response is a small planned addition; until then use `text`/CDP.
- **Clean shutdown over the port** (`/shutdown`), never `pkill` — that orphans Chrome + loses the
  session.
- **Concurrency:** multiple agents can POST to the same server; calls are serialised on the single
  browser. For true parallelism run multiple servers on different `PH_PORT`s.

## Method note (client has extra verbs)
`engine/client.py` also exposes `move/click/key/inject/detect/newtab/rightmon` — these were built for
the **Playwright method** (`methods/playwright-server.py`) which implements those primitives. The
**nodriver engine/server.py** implements the core set above (`goto/solve/text/shot/clearcookies`).
Use the core set against the nodriver engine; the extra primitives against the Playwright server.
(Consolidating these is a TODO.)

_Connect over HTTP:port, auto-start with ensure_server(), drive with goto/solve/text/shot. The engine
beats Cloudflare Turnstile (nodriver) and stays capture-only so any app/agent can share it._
