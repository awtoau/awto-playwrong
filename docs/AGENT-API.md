# Agent API — connect to awto-playwrong (and start it if needed)

> **Before writing any of the code below, check whether you need to.** The
> goto → detect → solve → text → close-tab sequence on this page is already implemented:
>
> - **agent** → the MCP tools, [MCP.md](MCP.md): `fetch{url}` and you're done.
> - **shell** → `./playwrong <url>` — starts everything itself, prints the page as text.
> - **python** → `from engine import connect; connect.capture(url)` — same code path as both of the
>   above.
>
> None of them need a server started first, and none need `PYTHONPATH` (server.py puts `vendor/` on
> `sys.path` itself — snippets that export it are copying a step that hasn't been required in a long
> time). This page is the raw HTTP contract: read it when you're building something that is none of
> the above, or need a verb those layers don't expose.

How an app or agent uses the shared capture engine: it's an **HTTP server on a port**; you POST ops
and get JSON back. No SDK needed — plain HTTP. If the server isn't running, `connect.ensure()` (or
any of the three entry points above) starts it; starting it by hand is a fallback, not the workflow.

## TL;DR
```
Base URL:  http://127.0.0.1:8731     (PH_PORT env overrides the port)
Check up:  GET  /status              -> {"server": true, "alive": true|false,
                                        "launched": true|false, "chrome_pid": 1234|null,
                                        "code": {"sha": "4abc6e6", "dirty": false, "started": "..."}}
Warm up:   POST /start  {}                 -> {started: true}  (blocks until Chrome is launched)
Drive:     POST /goto   {"url": "..."}     -> {title, url, requested}   (url = where you LANDED)
           POST /solve  {"tries": 20}      -> {passed, iter}     (clear Cloudflare Turnstile)
           POST /text   {}                 -> {html, title, url}
           POST /shot   {}                 -> {b64}              (PNG screenshot, base64)
           POST /clearcookies {}           -> {cleared}
           GET  /frame                     -> image/png          (latest screenshot bytes)
           POST /shutdown {}               -> {ok}
```

**`server` vs `alive` vs `launched` — three different things, don't confuse them.** `server: true`
means the HTTP process is up and answering requests (true the instant it responds at all — if you got
JSON back, this is true). `alive` means Chrome is **there right now**, re-checked per call.
`launched` means a browser was started at some point, which is what `alive` used to report — it
stayed true after Chrome died, so `/status` and `doctor.py` called a wedged engine healthy while
every op failed (issue #8). `chrome_pid` is the browser's pid, or null.

Chrome launches **lazily**: nothing spawns a browser until the first real op
(`start`/`goto`/`newtab`/etc.) asks for one. Polling
`/status` in a loop **waiting for `alive` to turn true on its own will hang forever** if nothing
else ever calls a real op - this is not a bug to work around, it's the intended lazy-launch design
(no wasted Chrome startup if a caller never ends up driving the browser), but it has caught agents
off guard before. If you just want the browser up and ready before doing anything else, call `POST
/start` and wait for its response - it blocks until Chrome is launched, so there's no ambiguity
about what to poll for.

## Connect, auto-starting the server if needed

**This is now `engine/connect.py` — import it instead of copying the snippet:**

```python
from engine import connect
connect.ensure()                                  # engine + Chrome up, idempotent
page = connect.call("text")                       # or connect.op("text"), which ensures first
whole = connect.capture("https://example.com")    # own tab, solve, extract, close  <- usually this
```

The snippet below is kept as documentation of what `ensure()` does, for non-Python callers. Note it
does **not** need `PYTHONPATH` — that line has been redundant since `server.py` started inserting
`vendor/` into `sys.path` itself.

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
    """Start the capture server if it isn't already running, then wait for the port. This only
    waits for the HTTP PROCESS to answer - it does NOT wait for Chrome (that's /start, see below),
    and does not need to: up() checks reachability only, never the "alive" field."""
    if up(): return
    subprocess.Popen([sys.executable, f"{REPO}/engine/server.py"],
                     env={**os.environ, "PH_PORT": str(PORT)},   # no PYTHONPATH needed
                     stdout=open(os.path.join(REPO, "tmp", "playwrong-server.log"), "a"),
                     stderr=subprocess.STDOUT)
    for _ in range(60):                 # wait up to ~30s for the HTTP server to bind
        if up(): return
        time.sleep(0.5)
    raise RuntimeError("playwrong server did not start")

def call(op, **body):
    req = urllib.request.Request(f"{BASE}/{op}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=120).read())

# --- usage ---
ensure_server()                                  # start the HTTP process if needed
call("start")                                     # explicitly launch Chrome and wait for it (optional
                                                   # but recommended - see the alive/server note above;
                                                   # skippable since goto/etc. below trigger it anyway)
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
# no need to start the server: every client verb below starts it (and Chrome) if they're down
"$REPO_ROOT/playwrong" https://example.com      # one page, as text
python "$REPO_ROOT/engine/client.py" goto https://example.com
python .../engine/client.py solvecf      # solve Turnstile
python .../engine/client.py text         # html
python .../engine/client.py shutdown     # clean stop (never pkill the browser)
```

## Endpoints (the real contract — nodriver engine/server.py)
| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/status` | — | `{server: true, alive: bool, launched: bool, chrome_pid: int\|null, code: {sha, dirty, started}}` — `server` is always true if this responds at all; `alive` is a live check of the browser (false before the first op launches it, and false again if it dies); `launched` is the old "has one ever started" flag; `code` is the source this PROCESS is running, stamped at launch — a long-lived engine keeps serving what it imported, so compare `code.sha` with your checkout before concluding a fix is live |
| POST | `/start` | — | `{started: true}` — explicitly launches Chrome and blocks until ready; use this instead of polling `/status` for `alive` |
| POST | `/goto` | `{url}` | `{title, url, requested}` — navigates (2s settle). `url` is `location.href` *after* redirects/interstitials; `requested` is what you asked for. Storing the requested url records a page you may never have got. |
| POST | `/solve` | `{tries?}` | `{passed, iter}` — finds + clicks the Turnstile "verify you are human" iframe, polls until clear |
| POST | `/text` | — | `{html, title, url}` — current page |
| POST | `/shot` | — | `{b64}` — PNG screenshot base64 |
| GET | `/frame` | — | `image/png` — latest screenshot bytes (for live viewing) |
| POST | `/clearcookies` | — | `{cleared}` |
| GET | `/markers`, POST `/setmarkers` | — | overlay markers (for the /viz debug page) |
| GET | `/viz` | — | side-by-side debug viewer (mirror + overlay) |
| POST | `/shutdown` | — | `{ok}` — clean stop |

## Notes for agents
- **PDFs: don't open them in the browser, and don't curl them either.** Chrome's built-in PDF viewer
  (PDFViewerApplication) can't be driven via JS: page navigation, scrolling and thumbnail clicks all
  fail. And curl gets a bot wall's block page (often saved as a .pdf that won't open) — so for a
  protected file neither tool works alone.
  The working sequence is: clear the challenge in the browser → read `/cookies` → fetch the file over
  HTTP with that jar *and the browser's exact User-Agent*. That is implemented, so don't hand-roll
  it: `playwrong --pdf <url> -o sources/doc.pdf`, the MCP `pdf` tool, or `connect.pdf(url, path=…)` /
  `connect.download(url, path)` for any file type. `connect.session_headers(url)` returns just the
  `{Cookie, User-Agent, Referer}` if you want to drive the download yourself.
  **Use it even when the url looks unprotected**, because you cannot tell in advance and the failures
  are silent: a 200 whose body is a login page, or a 45-page datasheet that arrives as 10 pages. Both
  save without error. `connect.pdf` checks the magic bytes and returns `pages`, so both are visible;
  it also returns `final_url` (post-redirect) and `bytes` for a manifest. See docs/MCP.md.
- **Every url comes through this engine** — not curl, wget, urllib or a built-in web-fetch, and that
  includes a url you already have, a raw file, or one that looks simple. A fetch made any other way
  loses the only thing this provides, silently: the 200-that-is-a-login-page and the 10-of-45-pages
  above, plus DuckDuckGo answering HTTP 202 + an image CAPTCHA, and a client-rendered page arriving
  as an empty shell. All look like success. A tool here misbehaving is a bug to report, not a reason
  to fall back to curl.
- **A page behind a login: ask the user, don't route around it.** Chrome is headed and the person is
  in front of it, so the working move is `/goto` the page, then stop and ask them to sign in to that
  tab (tabs carry the caller's agent label), then `/text` when they confirm. Don't poll or sleep
  while you wait — end the turn. Never handle the credentials yourself: not pasted into chat, not
  typed via `/js`, not read from a file. 2FA, SSO and a CAPTCHA on the login form are theirs too, and
  they work precisely because the browser is real. Pair with `PH_PROFILE` below so it survives a
  restart.
- **Tab cleanup is owner-scoped.** `closeextra {owner, force}` closes the caller's own tabs plus any
  whose owning process has exited, spares other agents' live tabs and returns them in `skipped`.
  Pass the `owner` you opened tabs with (`newtab {owner}`) or your tabs look like someone else's;
  `force: true` closes everything but tab 0, as it always did. Advisory, not enforced — the failure
  it prevents is an accident (#15).
- **One browser, shared.** The server holds ONE headed Chrome, alive across requests, so the cleared
  Turnstile session persists — solve once, many agents/calls reuse it. Don't launch a second browser
  (causes orphan-window conflicts).
- **A dead browser heals itself; don't restart the engine for it.** An op that hits a dropped
  connection drops the dead handles and retries once, and `_ensure()` re-checks the browser rather
  than trusting a handle. Two outcomes:
  - **Socket died, Chrome still running** (issue #6) → **reattach** over `/cdp`. The cleared
    Turnstile session and every login survive, because it is the same browser.
  - **Chrome exited** — crash, or someone closed the window → **relaunch**. A still-running browser
    is stopped first so it is not orphaned. Cookies and the cleared session are lost unless the
    profile is persistent (`PH_PROFILE`).

  Tab tags are dropped with the browser they named either way, so re-open your tabs. Before the fix
  this state was permanent and invisible: every op returned `ConnectionClosedError` while `/status`
  said `alive: true` (issues #6, #7, #8). `python scripts/recovery_test.py` is the regression guard —
  it covers the closed-window and crash paths; the reattach path can't be forced from outside the
  engine and is untested.
- **Capture-only, no DB.** You get html/cookies/screenshot back; store it yourself. This engine never
  touches a database.
- **Cookies:** `POST /cookies` returns `{cookies:[{name,value,domain}]}` for the whole browser,
  including the cleared `cf_clearance`. (This used to be listed as "a small planned addition" — it
  has existed and worked for a while; the note was stale.)
- **Clean shutdown over the port** (`/shutdown`), never `pkill` — that orphans Chrome + loses the
  session.
- **Concurrency:** multiple agents can POST to the same server; calls are serialised on the single
  browser. For true parallelism run multiple servers on different `PH_PORT`s.
- **Persistent profiles: `PH_PROFILE=<name>` (or `PH_PROFILE_DIR=<path>`).** Unset = ephemeral, a
  throwaway profile per launch. A *name* is resolved to `$XDG_DATA_HOME/playwrong/profiles/<name>`
  and created for you, so logins and cleared sessions survive a restart. On the CLI:
  `playwrong --profile work <url>` — each name gets its own engine on its own stable, name-derived
  port, since a Chrome profile is fixed at browser launch and one engine therefore serves one
  profile.
- **Stale `SingletonLock` — now healed automatically, no longer a manual step.** If a
  persistent-profile engine died without a clean `/shutdown` (crash, `kill -9`, host reboot),
  Chrome's `SingletonLock`/`SingletonCookie`/`SingletonSocket` are left pointing at a long-dead PID,
  and a relaunch against that profile used to hang indefinitely — `/status` never reporting `alive`,
  nothing in `tmp/nd-server.log` past `server_start`. Not a crash: the launch itself was stuck on
  the lock. The engine now checks the lock's owning PID before launching and removes the three files
  only if that process is gone (a live lock is left alone), logging `profile_lock_cleared`. Session
  data is never touched.
- **Diagnosing a launch that "hangs":** check `tmp/nd-server.log` (structured, one line per
  `server_start`/`nd_started`/`op_err` event - NOT the same as the HTTP process's own redirected
  stdout, which stays empty during a normal lazy-launch wait since nothing calls `print()`). A
  `server_start` line with no matching `nd_started` after it, and no `op_err`, most often just
  means `/start` (or a real op) was never actually called yet - see the `server` vs `alive` note
  above - not that anything is broken.

## Full verb set (now all on the nodriver engine — tested)
The nodriver `engine/server.py` now implements the full surface (verified live):

| Op | Body | Returns |
|---|---|---|
| `start` | — | `{started:true}` — explicitly launch Chrome (otherwise lazy - see `/status` note above) and block until ready |
| `goto` | `{url}` | `{title,url,requested}` — `url` = landed, `requested` = asked |
| `solve` | `{tries?}` | `{passed,iter}` |
| `text` | — | `{html,title,url}` |
| `shot` | — | `{b64}` |
| `frame` (GET) | — | `image/png` |
| `move` | `{x,y}` | `{ok,x,y}` — CDP synthetic mouse move (no real-cursor jump) |
| `click` | `{x,y}` | `{ok,x,y}` — CDP synthetic click |
| `key` | `{key}` | `{ok,key}` — named (Enter/Tab/Escape/…) or a printable char |
| `newtab` | `{url?, tag?, owner?}` | `{ok,url,index,tag,target_id}` — fresh tab. **Pass a `tag`** and every later op can name it via `tab`, which is how concurrent callers avoid driving each other's page. `owner` ("agent@repo:pid") is prefixed to the tab's TITLE so a human can see who opened it, and stripped from every title the API returns |
| `tabs` | — (also `GET /tabs`) | `{tabs:[{index,url,title,active,tag,target_id}],count}` — enumerate every open tab, including who owns it. `url`/`title` come from each target and are populated for BACKGROUND tabs too |
| `closetab` | `{tag?}`, `{index?}` or `{url?}` `{keep_first?}` | `{closed,remaining}` — close by **tag** (exact, race-free), index, or url-substring; won't close tab 0 or the last tab. Prefer `tag`: an index goes stale the instant another caller closes a lower-numbered tab |
| `closeextra` | — | `{closed,remaining}` — close ALL tabs except the base tab (leak cleanup) |
| `prefetch` | `{urls,concurrency?,solve?,tries?,timeout?}` | `{job,count,concurrency,timeout}` — starts loading all of them in parallel tabs and returns IMMEDIATELY. Per-url `timeout` (30s) so one stalled page cannot hold the batch |
| `jobs` | `{job?}` | counts: `{ready,loading,pending,errors,urls}` — cheap to poll, never ships page bodies |
| `collect` | `{job,drain?}` | `{results:[{status,title,html,url,partial?}],remaining}` — every READY result, removed from the engine once handed over |
| `cdp` | — | `{host,port,http}` — the shared browser's CDP endpoint, so another process can ATTACH (`nodriver.start(host,port)`) to this SAME browser and open its own tabs (parallel sharding) |
| `js` | `{expr}` | `{result}` — evaluates in the page and **resolves promises**: `await …`, a `.then()` chain, objects and arrays all come back as plain JSON. A thrown JS error returns `{error}` with the message. (It used to return null for anything async.) |
| `cookies` | — | `{cookies:[{name,value,domain}]}` — the whole browser, incl. cleared `cf_clearance`. Feed these to an HTTP client to download a file the browser cleared for you. |
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
- **`closed`/`remaining` are now trustworthy — they weren't always.** nodriver's `browser.tabs` is a
  *cached* target list that `tab.close()` does not refresh, so the server used to answer
  `{"closed":1,"remaining":2}` after closing one of two tabs, and `tabs` reported blank urls for
  every tab. Cleanup was unverifiable and an MCP-layer `fetch` leaked a tab per call. The server now
  refreshes the target list before reading it and after closing; if you write a new verb that touches
  `browser.tabs`, call `_refresh()` first or you will reintroduce this.
- **The base tab (index 0) is protected** — `closetab` won't close it and `closeextra` keeps it, so the
  server always has a live tab (its `/status` stays alive).

Multiple agents can POST concurrently; ops are serialised on the single browser. For true parallel
browsers, run multiple servers on different `PH_PORT`s — but within one server, shard by tab.

### Tab tagging — the built-in answer for concurrent callers

**Every driving op takes an optional `tab`** (`goto`, `text`, `shot`, `js`, `click`, `key`, `move`,
`solve`). Omit it and you get the engine's active tab, which is fine for a single caller. Pass a tag
and the op acts on YOUR page no matter what anyone else is doing:

```python
connect.call("newtab", tag="agent-7", url="about:blank")
connect.call("goto", url="https://example.com", tab="agent-7")
page = connect.call("text", tab="agent-7")          # your page, guaranteed
connect.call("closetab", tag="agent-7")
```

Without this, two callers on one engine silently returned each other's pages: A's `goto`, then B's
`goto` moving the active tab, then A's `text` reading B's page. Tags resolve to CDP target ids, which
never shift — unlike indices, which move whenever a lower-numbered tab closes.

`engine/connect.py` does all of this for you (`capture()` tags per call), and the MCP server gives
each agent its own tagged tab. You only need the raw form when driving the port directly.

### Parallel sharding — attach to the same browser via `/cdp`
The HTTP ops (`goto`/`text`/…) drive ONE active tab, serialised — fine for a single agent driving one
page at a time. For a process that needs to drive MANY tabs in parallel (e.g. a crawler), don't fight
the HTTP serialisation: read the browser's CDP endpoint from `/cdp` and attach your own nodriver to the
same Chrome:

```python
cdp = call("cdp")                       # {host, port, http}
import nodriver as uc
browser = await uc.start(host=cdp["host"], port=cdp["port"])   # host+port set => ATTACH, don't launch
tab = await browser.get(url, new_tab=True)   # your own tab on the shared browser
...                                          # drive N tabs in parallel via nodriver
await tab.close()                            # CLOSE every tab you opened when done
```

You now drive the shared browser directly (parallel tabs) while still respecting the contract: you
launched no new browser, you close your own tabs, and you never shut the server down. Two agents can
coexist — one driving via HTTP ops, another attached via CDP for parallel work — on the one browser.

_Connect over HTTP:port, auto-start with ensure_server(), drive with goto/solve/text/shot. The engine
beats Cloudflare Turnstile (nodriver) and stays capture-only so any app/agent can share it._
