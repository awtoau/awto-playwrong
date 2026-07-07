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
REPO = os.environ.get("PLAYWRONG_REPO", os.getcwd())
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
                     stdout=open(os.path.join(REPO, "tmp", "playwrong-server.log"), "a"),
                     stderr=subprocess.STDOUT)
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
REPO_ROOT="$(pwd)"  # set to your local checkout root if needed
PYTHONPATH="$REPO_ROOT/vendor" \
  python "$REPO_ROOT/engine/server.py" &     # start (headed real Chrome)
python "$REPO_ROOT/engine/client.py" goto https://example.com
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

## Full verb set (now all on the nodriver engine — tested)
The nodriver `engine/server.py` now implements the full surface (verified live):

| Op | Body | Returns |
|---|---|---|
| `goto` | `{url}` | `{status,url,title}` |
| `solve` | `{tries?}` | `{passed,iter}` |
| `text` | — | `{html,title,url}` |
| `shot` | — | `{b64}` |
| `frame` (GET) | — | `image/png` |
| `move` | `{x,y}` | `{ok,x,y}` — CDP synthetic mouse move (no real-cursor jump) |
| `click` | `{x,y}` | `{ok,x,y}` — CDP synthetic click |
| `key` | `{key}` | `{ok,key}` — named (Enter/Tab/Escape/…) or a printable char |
| `newtab` | `{url?}` | `{ok,url,index}` — fresh tab; **index** is its position (track it, close it later) |
| `tabs` | — (also `GET /tabs`) | `{tabs:[{index,url,title,active}],count}` — enumerate every open tab |
| `closetab` | `{index?}` or `{url?}` `{keep_first?}` | `{closed,remaining}` — close by index OR url-substring; won't close tab 0 or the last tab |
| `closeextra` | — | `{closed,remaining}` — close ALL tabs except the base tab (leak cleanup) |
| `js` | `{expr}` | `{result}` — evaluate JS in the page |
| `cookies` | — | `{cookies:[{name,value,domain}]}` |
| `clearcookies` | — | `{cleared}` |
| `shutdown` | — | `{ok}` |

`engine/client.py` (CLI) wraps these; some client-only helpers (`inject`, `detect`, `rightmon`,
ollama vision) are convenience layers on top of the core ops.

## Sharding contract — ONE server, many agents, tabs are the unit of work

**playwrong is a single long-running process that many agents SHARE by opening their own tabs.** This
is the core operating model — treat it accordingly:

- **Never launch your own browser** (`uc.start()`) or `pkill` Chrome to "get a clean slate." That
  defeats the shared server (orphan windows, lost Turnstile session, competing browsers). Connect to
  the running server on its port; if it isn't up, `ensure_server()` starts THE one server.
- **Never `shutdown` the server** to end your work. Shutdown stops it for everyone. Close YOUR tabs
  instead (`closetab`), leave the server running.
- **A tab is your shard.** `newtab` → do your work on it → **`closetab` when done.** The returned
  `index` is your handle. An agent that opens tabs and never closes them leaks tabs and renderer
  processes (a crawler that opened 8 tabs/run and never closed them left ~20 orphan renderers — the bug
  these verbs fix). Track what you open; close what you opened.
- **Cleanup after a crash:** `closeextra` closes every tab except the base tab — the panic button when
  an aborted run left orphan tabs. It never touches the server process.
- **The base tab (index 0) is protected** — `closetab` won't close it and `closeextra` keeps it, so the
  server always has a live tab (its `/status` stays alive).

Multiple agents can POST concurrently; ops are serialised on the single browser. For true parallel
browsers, run multiple servers on different `PH_PORT`s — but within one server, shard by tab.

_Connect over HTTP:port, auto-start with ensure_server(), drive with goto/solve/text/shot. The engine
beats Cloudflare Turnstile (nodriver) and stays capture-only so any app/agent can share it._
