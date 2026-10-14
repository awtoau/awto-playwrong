# Engine HTTP API

The raw contract of `engine/server.py`. **You probably don't need it:**

- agent → MCP tools, [MCP.md](MCP.md): `fetch{url}`
- shell → `playwrong <url>`
- Python → `from engine import connect; connect.capture(url)`

All three start the engine themselves. Read on to build a client in another language, or to use an
op those layers don't expose.

## Basics

- `http://127.0.0.1:8731` (`PH_PORT`; a `PH_PROFILE` name picks its own port, see `connect.default_port()`).
- `POST /<op>` with a JSON object body → JSON. Failures return `{"error": "…"}` (HTTP 200); a
  malformed body returns HTTP 400.
- `GET /status`, `GET /tabs`.
- Start it: `connect.ensure()`, or run `python engine/server.py` (no `PYTHONPATH` needed).
- Ops run concurrently on the engine's event loop. **Name your tab on every driving op** (below).

## `/status`

```json
{"server": true, "alive": true, "launched": true, "chrome_pid": 1234,
 "code": {"sha": "eaee615", "dirty": false, "started": "2026-10-15T00:00:00+00:00"},
 "pid": 82682, "rss_mb": 90, "swap_mb": 0, "fds": 31, "cdp_sockets": 2, "threads": 5, "tabs": 3,
 "uptime_s": 120, "scope_current_mb": 900, "scope_peak_mb": 1400, "scope_unreclaimable_mb": 700,
 "scope_max_mb": 8192, "scope_oom_kills": 0, "memory_cap": "8G", "rss_limit_mb": 2048,
 "headless": false, "display": ":0", "display_mode": "desktop"}
```

| Field | Meaning |
|---|---|
| `server` | the HTTP process answered (always true if you got JSON) |
| `alive` | Chrome answers a CDP round trip **now** (#8) |
| `launched` | a browser was started at some point. Not health. |
| `code` | the source this process imported at launch. Compare `code.sha` with your checkout before assuming a fix is live. |
| `rss_mb`, `fds`, `cdp_sockets`, `scope_*` | memory and leak signals (#23, #28) |

**Chrome launches lazily.** Nothing starts a browser until an op needs one, so polling `/status` for
`alive` will wait forever. Call `POST /start` instead: it blocks until Chrome is up.

## Ops

Driving ops take an optional `tab` (a tag or an index). Without one, they act on the engine's active
tab.

| Op | Body | Returns |
|---|---|---|
| `start` | — | `{started}`: launch Chrome and block until it is ready |
| `newtab` | `{url?, tag?, owner?}` | `{ok, url, index, tag, owner, target_id}`. A tagged tab doesn't become active. `owner` (`agent@repo:pid`) prefixes the tab title. |
| `goto` | `{url, tab?, no_js?}` | `{title, url, requested, ready_state}`. `url` is where the page **landed**. Waits a 2 s settle, then up to `LOAD_WAIT` (5 s) for `complete` (#32). `no_js` disables page scripts. |
| `text` | `{tab?}` | `{title, url, html}` |
| `solve` | `{tries?, tab?}` | `{passed, iter}`: find and click the Turnstile checkbox (through its OOPIF), poll until clear |
| `shot` | `{tab?}` | `{b64}`: PNG |
| `js` | `{expr, tab?}` | `{result}`, main world. Resolves promises; objects come back as JSON. A JS throw returns `{error}`. |
| `click` / `move` | `{x, y, tab?}` | `{ok, x, y}`: CDP mouse input, viewport CSS pixels |
| `key` | `{key, tab?}` | `{ok, key}`: a named key (Enter/Tab/Escape/Backspace/arrows) or a printable char |
| `window` | `{bounds?, tab?}` | `{windowId, bounds}`; with `{left, top, width, height}`, moves/resizes the window first |
| `cdp` | — | `{host, port, http}`: the browser's CDP endpoint |
| `cdp` | `{method, params?, tab?, browser?}` | `{result}`: raw CDP on the tab's session, or the browser's with `browser: true`. Errors carry Chrome's message. Request/response only. |
| `tabs` | — (or `GET /tabs`) | `{tabs: [{index, url, title, active, tag, owner, target_id}], count}` |
| `closetab` | `{tag}` (or `{index}` / `{url}`, `keep_first?`) | `{closed, remaining}`. Prefer `tag`: an index shifts when another caller closes a tab. Never closes the last tab, nor tab 0 while `keep_first`. |
| `closeextra` | `{owner?, force?}` | `{closed, remaining, skipped}`: closes your tabs (`owner`) and orphans of exited processes; spares live agents' tabs. `force` closes all but tab 0 (#15). |
| `prefetch` | `{urls, concurrency?, solve?, tries?, timeout?, owner?}` | `{job, count, concurrency, timeout}`, immediately. Each url has its own `timeout` (30 s). |
| `jobs` | `{job?}` | `{job, total, delivered, ready, loading, errors, pending, done}`, or `{jobs: […]}` for every job |
| `collect` | `{job, drain?}` | `{job, results: [{status, title, html, url, partial?} \| {status: "error", requested, error}], remaining, done}`. Drained once handed over. Jobs idle for `JOB_IDLE` (600 s) are dropped. |
| `cookies` | — | `{cookies: [{name, value, domain}]}`: the whole browser, including `cf_clearance` |
| `clearcookies` | — | `{cleared}`. Wipes the **shared** session. |
| `shutdown` | — | `{ok}`. Stops the engine **for everyone**. Never `pkill` instead. |

From Python, `connect.call("cdp", body={"method": …})`: `method` collides with `call()`'s own HTTP
method argument.

## Tabs: the unit of sharing

One engine, one browser, many callers. Each caller works in **its own tagged tab**:

```python
connect.call("newtab", tag="agent-7", owner=connect.OWNER, url="about:blank")
connect.call("goto", url="https://awto.au", tab="agent-7")
page = connect.call("text", tab="agent-7")          # your page, whatever anyone else is doing
connect.call("closetab", tag="agent-7")
```

- Tags resolve to CDP target ids, which never shift.
- A tag whose tab was swept is reopened on the next use (#14). An unknown tag errors with
  `no such tab`.
- An untagged op whose active tab vanished moves to a live untagged tab (or a fresh `about:blank`)
  and retries once, logged as `tab_repointed` (#22).
- **Never** launch your own browser, `pkill` Chrome, or `shutdown` to end your work. Close your tabs.
- `connect.capture()` and the MCP server do all of this for you.

## Parallel tabs: attach your own CDP client

To drive many tabs at once with CDP events, attach to the same browser:

```python
from engine import cdp, connect
ep = connect.call("cdp")                             # {host, port, http}
browser = await cdp.attach(ep["host"], ep["port"])   # ATTACH, never launch
tab = await browser.new_tab("https://awto.au")       # your own tab on the shared browser
await tab.send("Page.getLayoutMetrics")              # any CDP command
tab.on("Network.responseReceived", handler)          # events, after the domain is enabled
title = await tab.evaluate("document.title", isolated=True)   # a world the page cannot hook
await tab.close()                                    # close every tab you opened
await browser.aclose()                               # your socket only; Chrome keeps running
```

- `crawl.browser` wraps this for the crawler.
- The DevTools port is loopback only, and refuses connections from web origins (#52).
- For a human: `http://<host>:<port>/json/list` → `devtoolsFrontendUrl`.

## Recovery

- **The browser socket died but Chrome is still running (#6)** → reattach. The session survives.
- **Chrome exited** → relaunch. Cookies are lost unless the profile is persistent.
- Either way, tags are dropped with the browser, so reopen your tabs. `recovery_test.py` covers both
  cases.
- **A crashed renderer (#28)**: ops on that tab raise `TabCrashed`; other tabs are unaffected.
- **Near the memory cap**: `newtab`/`prefetch` raise `MemoryHighWater`. Memory limits:
  [README](../README.md#memory).
- **A stale `SingletonLock`** on a persistent profile is removed when its owner pid is gone
  (`profile_lock_cleared`).

## Logs

In the data dir (the checkout's `tmp/`, or `$XDG_CACHE_HOME/playwrong`):

- `nd-server.log`: `server_start`, `nd_started`, `op_done op= ms= rss_before_mb=`, `op_err`, and `mem`
  every 60 s. A `server_start` with no `nd_started` usually means no op has asked for Chrome yet.
- `chrome.log`: Chrome's own output, for a launch that fails.
- `logs/playwrong-engine.log`: the spawned process's stdout/stderr, including startup tracebacks.
