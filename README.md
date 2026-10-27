# awto-playwrong

<!-- mcp-name: io.github.awtoau/awto-playwrong -->

**Fetch the pages that won't let you fetch them.**

```sh
playwrong https://awto.au          # the page, as readable text. Nothing to start first.
```

One shared, long-lived, headed Chrome, driven over a local port. It clears Cloudflare Turnstile and
stays warm between calls. The CLI, AI agents (via **MCP**) and your own scripts all drive the same
browser and reuse the same cleared session.

Repo: <https://github.com/awtoau/awto-playwrong> · MIT

## Features

**Getting pages**
- **Any page as readable text** (or HTML, or text with links): one call, nothing to start first.
- **Cloudflare Turnstile cleared**, including the checkbox inside its out-of-process iframe. Later
  calls reuse the clearance cookies.
- **Web search** where curl gets a CAPTCHA: DuckDuckGo, falling back to Brave Search.
- **PDFs** as text plus a page count (a truncated document shows itself), with the file kept.
- **Downloads** through the cleared session: Range resume, in-browser fallback, sha256/size
  verification. A short transfer is never reported as complete.
- **Batch prefetch**: many urls in parallel tabs, read as each finishes. A slow page costs one slot,
  not the whole batch.
- **Logins**: the user signs in to the headed browser. Named profiles (`--profile`) keep the session
  across restarts.

**Driving a page**
- `goto`, `read`, `js` (resolves promises, returns objects as JSON), `click`, `key`, `screenshot`,
  `cookies`, window placement, tabs.
- **Many agents, one browser**: each caller gets its own tagged tab, so no agent ever reads another
  agent's page. `playwrong --tabs` shows who owns each tab.
- **Full Chrome DevTools Protocol**: a raw `cdp` passthrough op, a CDP endpoint your own client can
  attach to, and the DevTools frontend. See [Chrome debugging surface](#chrome-debugging-surface).

**Interfaces**
- **MCP server** (stdio, stdlib-only) for Claude Code, VS Code and other agents.
- **`playwrong` CLI**, an interactive client, and a plain HTTP API on `127.0.0.1:<port>`.
- **Python**: `from engine import connect; connect.capture(url)`.

**Stealth by omission**
- Uses its own minimal CDP client (`engine/cdp.py`, one file, MIT). No Playwright, no chromedriver,
  no `--enable-automation`, and it never calls `Runtime.enable`. `navigator.webdriver` stays false.
- The engine reads pages from an isolated world that page scripts cannot hook.
- No `Debugger.enable`, so a page's `debugger;` timing traps never pause. Without `Runtime.enable`,
  console-getter DevTools detection never fires. Nothing is written into the page (#55).
- Not hidden: the `js` op runs your expression in the page's own world, and CDP mouse input
  (see #55).

**Reliability**
- Starts the engine and Chrome automatically. Reattaches to a live Chrome, or relaunches a dead one.
- Everything is bounded: tab timeouts, a crashed renderer fails only its own tab (by name), a
  memory cap per engine (systemd scope), RSS self-restart, Chrome recycling, size-capped logs.
- `scripts/doctor.py` preflight checks. `/status` never reports a dead browser as alive.

**Crawling (optional `crawl/` library)**
- Point-and-go site crawler: resource blocking, consent dismissal, render waits, HTTP status
  recording, 429/503 backoff, per-host caps, a resumable frontier in SQLite / Postgres / MySQL.
- Importance ranking (sitemaps, url shape, OpenPageRank) and a site-shape report (hubs, orphans,
  dead links).

**Why not just `curl`?** More and more pages won't answer it. Cloudflare serves a challenge, and
DuckDuckGo returns `HTTP 202` with an image CAPTCHA. A real browser isn't challenged at all.
Nothing here solves or breaks a CAPTCHA: it drives a real browser and hands you what it was served.

## Install

```sh
git clone https://github.com/awtoau/awto-playwrong && cd awto-playwrong
python3.14t -m venv .venv                                              # 3.14+; free-threaded recommended
.venv/bin/python scripts/install.py --deps --link --register --vscode  # deps, CLI on PATH, MCP
.venv/bin/python scripts/mcp_selftest.py                               # prove it end to end
```

| Need | Why |
|---|---|
| **Python 3.14+**; a free-threaded build (`3.14t`) is recommended | The engine runs an HTTP thread per request alongside its asyncio loop. Crawls parse in parallel. A GIL build works but runs serialised. |
| **Chrome or Chromium** (`PH_CHROME` overrides the lookup) | the browser |
| **A display** | Chrome runs headed on purpose, because headless is the Turnstile tell. An engine with no `DISPLAY` adopts the user's X/Wayland session. |
| `websockets` | the only PyPI package; everything else is in-tree stdlib |
| optional: `poppler-utils` | `pdftotext`/`pdfinfo`, for PDF text and page count |
| optional: `systemd-run` (user session) | the engine's memory cap |
| optional: `Xvfb` | `PH_DISPLAY=xvfb`: tests run headed, off the desktop |

`python scripts/doctor.py` checks every one of these and prints the exact fix command for anything
missing.

## Agents: the MCP server

Full guide: [docs/MCP.md](docs/MCP.md). After `--register`, the tools appear in the agent's tool list:

```
fetch("https://awto.au")   # starts engine + Chrome, clears Cloudflare, returns text, closes its tab
```

Tools: `fetch`, `prefetch`/`collect`, `search`, `pdf`, `download`, `screenshot`, `goto`, `read`, `js`,
`click`, `key`, `solve`, `cookies`, `tabs`, `close_tab`, `status`.

- **Send every url through it**, including urls you already have, raw files, and urls that look
  simple. Each other path fails silently: a 200 whose body is a login page, a 45-page datasheet that
  arrives as 10, a search answered with HTTP 202 + CAPTCHA, a JS page that arrives empty.
  [Details](docs/MCP.md#every-url-goes-through-here-no-exceptions).
- **Login walls:** `goto` the page, ask the user to sign in to that tab, then `read`. Never handle
  their credentials. See [Pages behind a login](docs/MCP.md#pages-behind-a-login-open-the-tab-and-ask-the-user).

## Shell: `playwrong`

```sh
playwrong https://awto.au                           # one page as text
playwrong https://awto.au https://awto.au/about     # several, one warm browser
playwrong -j 8 url1 url2 …                          # in parallel, printed as each finishes
playwrong --search "nodriver turnstile"             # DuckDuckGo results (Brave if blocked)
playwrong --pdf https://awto.au/doc.pdf -o sources/doc.pdf   # text + page count, file kept
playwrong --profile work https://awto.au            # persistent named profile
playwrong --tabs                                    # every open tab and who opened it
playwrong --status | --stop
```

| Flag | What |
|---|---|
| `-j N`, `--jobs N` | load N urls in parallel and print each as it finishes. Falls back to sequential with `--shot` / `--no-js`. |
| `-s`, `--search Q` / `-n N` | search results as `title + url` / max results (default 10) |
| `--pdf URL` | download through the cleared session, keep the file, print text and page count |
| `-o`, `--out PATH` | where `--pdf` writes (default: the data dir, see [Files](#files-and-logs)) |
| `--links` / `--html` | keep hrefs as `anchor <url>` / raw markup instead of text |
| `--max-chars N` | truncate each page (`0` = no limit; default 40000) |
| `--no-js` | disable page JavaScript: the server-sent HTML only |
| `--shot PATH` | also save a PNG. Several urls give `name-0.png`, `name-1.png`, … |
| `--json` | `{text, title, url, challenge, partial}` per page |
| `--no-solve` / `--tries N` | don't auto-clear Cloudflare / max solve iterations (default 20) |
| `--profile NAME` | persistent profile; each name gets its own engine on a stable port |
| `--agent NAME` | how this caller is labelled in `--tabs` (default: guessed; `PLAYWRONG_AGENT`) |
| `--tabs` | list every open tab with its owner |
| `--port N` | use/start an engine on another port (a second, isolated browser) |
| `-q`, `--quiet` | no progress notes on stderr |
| `--status` / `--stop` | engine state / stop the shared engine **for everyone** |

- Progress goes to stderr and the page to stdout, so `playwrong <url> > page.txt` stays clean.
- **You never manage a host:port.** The engine listens on `PH_PORT` (default 8731). The first call
  starts it and later calls reuse it, with the same cookies and the same cleared session.

### Driving a page: `engine/client.py`

Drives the CLI's own tab (`client`), which persists across invocations:

```sh
python engine/client.py goto https://awto.au
python engine/client.py js "document.title"       # promises resolve; objects come back as JSON
python engine/client.py read --links              # the page as text, hrefs kept
python engine/client.py click 640 360             # CDP input, viewport CSS pixels
python engine/client.py shot page.png
python engine/client.py solvecf                   # clear a Turnstile challenge
python engine/client.py cdp Page.getLayoutMetrics # raw CDP on the CLI's tab
python engine/client.py closetab                  # done: close the CLI's tab
```

Run `python engine/client.py` with no arguments to list every verb.

## Python: `engine/connect.py`

The CLI, the MCP server and your scripts all go through this one module, so "how do I start this
thing" has one answer.

```python
from engine import connect
page = connect.capture("https://awto.au")   # starts engine+Chrome, solves, closes its tab
print(page["text"])
```

| Function | Returns |
|---|---|
| `capture(url, mode=, max_chars=, solve=, shot=, no_js=, profile=)` | `{text, title, url, challenge, partial, b64?}`: one page in its own tab |
| `fetch_many(urls, concurrency=8)` | a **generator**: pages in completion order |
| `prefetch(urls, concurrency=, timeout=)` → `poll(job)` → `collect(job, wait=)` | job id → counts → ready pages, each delivered once |
| `search(query, max_results=)` | a list of `{title, url}`; its `.engine` is `"brave"` when DuckDuckGo blocked |
| `pdf(url, path=)` | `{path, bytes, pages, text, content_type, final_url, sha256, via, …}` |
| `download(url, path=, expect_sha256=, expect_size=, extra_headers=)` | `{path, bytes, content_type, final_url, sha256, via, expected_bytes, resumed}` |
| `session_headers(url)` | `{Cookie, User-Agent, Referer}`, to drive a transfer yourself |
| `ensure(port=, profile=)` | the port, with engine + Chrome up. Idempotent; safe for many processes at once. |
| `call(op, **body)` / `op(op, **body)` | any raw engine op, without / with auto-start |

Raw HTTP contract (every op, `/status` fields, tab tagging): [docs/AGENT-API.md](docs/AGENT-API.md).

## Chrome debugging surface

| Who | How |
|---|---|
| HTTP callers | `POST /cdp {"method": "Page.getLayoutMetrics", "params": {}, "tab": "<tag>"}` returns `{result}`. Add `"browser": true` for the browser session. Request/response only. |
| Python | `ep = connect.call("cdp")` → `await engine.cdp.attach(ep["host"], ep["port"])`: full CDP including events (`tab.on(...)`) on the shared browser. Close your tabs and `aclose()`; never `launch()`. |
| A human | `GET http://127.0.0.1:<cdp port>/json/list` → open a tab's `devtoolsFrontendUrl`. |

- The DevTools port binds to loopback only. Chrome refuses a websocket from any web origin except the
  DevTools frontend's own (`--remote-allow-origins=http://127.0.0.1:<port>`, #52). Clients without
  an Origin header (ours, Puppeteer, `websockets`) are accepted.
- `scripts/cdp_surface_check.py` proves all of this. Run it after any change to Chrome's flags.

## The engine

| Piece | Role |
|---|---|
| `engine/server.py` | one headed Chrome behind an HTTP port; holds the session, tags, memory guards |
| `engine/cdp.py` | the CDP client: one websocket, flattened per-tab sessions, `find_text` through OOPIF iframes (#45), isolated-world reads (#49) |
| `engine/connect.py` | reach/start the engine, html→text, `capture`/`search`/`download`/`pdf` |
| `engine/cli.py` + `./playwrong` | the one-shot command |
| `engine/client.py` | interactive verbs |
| `engine/mcp_server.py` | MCP stdio server |
| `engine/memcap.py`, `engine/logroll.py` | systemd memory scope; 20 MB + `.1` log rotation |

**Why raw CDP and not Playwright:** Cloudflare serves Playwright a dead challenge that never
resolves. A real Chrome over raw CDP passes. See [docs/lessons.md](docs/lessons.md).

### Memory

- The engine re-execs into a systemd user scope: `MemoryMax=8G` (`PH_MEMORY_MAX`, `0` = off), no
  swap, `OOMPolicy=continue`. Running out kills one renderer, and that tab fails by name (#23, #28).
- New tabs are refused above `PH_MEMORY_HIGH_WATER` (0.85) of the cap.
- The engine's own Python RSS above `PH_RSS_LIMIT` (2G) means a leak: it stops Chrome and exits, and
  the next call restarts it.
- Chrome is recycled after `PH_CHROME_MAX_TABS` (64) open tabs, or `PH_CHROME_MAX_FETCHES` (1000)
  tabs opened since launch.
- `python -m crawl.run` gets its own scope: `PH_CRAWL_MEMORY_MAX` (4G, #26).
- No systemd user manager → runs uncapped, and `/status` `memory_cap` says so.

### Environment

| Var | Default | What |
|---|---|---|
| `PH_PORT` | 8731 | engine port |
| `PH_PROFILE` / `PH_PROFILE_DIR` | throwaway | named persistent profile (own engine port, #54) / explicit profile path |
| `PH_CHROME` | lookup | Chrome binary |
| `PH_DISPLAY` | desktop | `xvfb`: headed Chrome on the engine's own Xvfb |
| `PH_HEADLESS` | off | `1`: headless. DuckDuckGo and Turnstile block it (#34). |
| `PLAYWRONG_AGENT` | guessed | the owner label on your tabs |
| `PH_TAB_LABEL` | off | `1`: also write the owner into the tab title. The page can read it (#55). |
| `PH_BLOCK_ANALYTICS` | on | `0`: stop blocking Google Analytics / Tag Manager requests on every tab (`engine/cdp.py` `BLOCK_URLS`). Agent page loads otherwise count as visitors in the site owner's GA4. Scripts and beacons only, not iframe documents. |
| `PH_MEMORY_MAX`, `PH_MEMORY_HIGH_WATER`, `PH_RSS_LIMIT`, `PH_CHROME_MAX_TABS`, `PH_CHROME_MAX_FETCHES`, `PH_CRAWL_MEMORY_MAX` | see [Memory](#memory) | |
| `PH_RIGHTMON_BOUNDS` | `0,0,1280,720` | `client.py rightmon` window placement, `x,y,w,h` |
| `PH_OUT_DIR` | `./tmp` | `crawl.drive` screenshots |
| `PH_TEST_HOOKS` | off | `1`: test-only ops (`recovery_test.py`, `memory_test.py`) |

### Files and logs

Everything goes in the **data dir**: the checkout's `tmp/`, or `$XDG_CACHE_HOME/playwrong` when the
checkout isn't writable (an installed package).

| File | What |
|---|---|
| `nd-server.log` | structured engine log: one line per op with RSS (`op_done`), `mem` every 60 s, errors |
| `chrome.log` | Chrome's own stdout/stderr, truncated per launch |
| `logs/playwrong-engine.log` | the spawned engine's stdout/stderr, including startup failures |
| `logs/search.log` | one JSON line per search attempt (engine, outcome, n) |

Logs roll at 20 MB, keeping one `.1` backup (#39).

## crawl/: the crawl library

Walks a whole site on top of the shared browser. Agent guide: [crawl/AGENTS.md](crawl/AGENTS.md).

```sh
python -m crawl.run --seed https://awto.au/ --db site.sqlite --max 200 --max-per-host 50 --tabs 8
python -m crawl.report --db site.sqlite          # re-print the site-shape report
```

| Module | What |
|---|---|
| `crawl.run` / `crawl.report` | point-and-go CLI → DB + site-shape report |
| `crawl.browser` | attach an `engine.cdp` client to the shared Chrome (starts the engine if down) |
| `crawl.challenge` | challenge detection (the engine's own check), `solve()`, soft-404 matcher |
| `crawl.netblock` | CDP Fetch resource blocking, one pattern per blocked type, so the page load never stalls |
| `crawl.render` | consent dismissal, `wait_ready`, `wait_for_render` (stable body length) |
| `crawl.parse` | words-only text, links, image refs, `image_kind` |
| `crawl.ratelimit` | per-host gap + 429/503 backoff, shared across tabs |
| `crawl.rank` | priority = sitemap + url shape + OpenPageRank (`--rank`, #2) |
| `crawl.store` | content-addressed zstd page store |
| `crawl.db` | SQLAlchemy 2.0 Core: SQLite / Postgres / MySQL, atomic `claim()`, lease reclaim |
| `crawl.graph` | hubs, authorities, orphans, dead links, image usage |
| `crawl.drive` | hand-driving: click / scroll / screenshot with condition-based settling ([helpers/README.md](helpers/README.md)) |
| `assets/` | content-addressed image store, Chrome cache harvest, image metadata |

Crawl dependencies: `pip install -e ".[crawl]"` (SQLAlchemy, psycopg, pymysql, zstandard).

**On a free-threaded Python**, two of them re-enable the GIL unless built pure-Python:
- SQLAlchemy: `DISABLE_SQLALCHEMY_CEXT=1 pip install --no-binary sqlalchemy "sqlalchemy>=2.0"`
- psycopg: plain `psycopg`, not `[binary]`/`[c]`
- As a tool: `DISABLE_SQLALCHEMY_CEXT=1 uv tool install --python python3.14t --no-binary-package
  sqlalchemy "awto-playwrong[crawl] @ git+https://github.com/awtoau/awto-playwrong"`
- Verify: `python -c "import sys, crawl.db; assert not sys._is_gil_enabled()"`

## Testing

Tests that start an engine use an **isolated port** (8739). They run Chrome headed on the engine's
own Xvfb (`PH_DISPLAY=xvfb`); pass `--headed` to watch. Every test prints a pass/fail tally.

| Script | Proves |
|---|---|
| `scripts/doctor.py` | preflight: Python, deps, Chrome, display, optional tools, port, the running engine's code and a real op |
| `scripts/mcp_selftest.py` | MCP over real stdio: protocol, live fetch, tab cleanup, short-transfer resume (#31), `PH_PROFILE` port (#54). `--offline`, `--cloudflare`, `--shutdown`. |
| `scripts/cdp_smoke_test.py` | `engine/cdp.py` against headless Chrome, offline: tabs, evaluate, screenshot, find+click through an iframe and shadow root, Fetch/dialog handling (#43) |
| `scripts/cdp_surface_check.py` | DevTools port: our client + a second attach accepted, the frontend origin accepted, a foreign origin refused, loopback only (#52) |
| `scripts/turnstile_click_test.py` | `solve` clicks a real interactive Turnstile (test sitekey) in its OOPIF (#45) |
| `scripts/stealth_check.py` | a CDP-leak detector: `navigator.webdriver`, `Runtime.enable` and sourceURL leaks |
| `scripts/stress_test.py` | duplicate/bad urls, empty batches, double collect, missing tabs, malformed requests, window + raw `cdp` ops |
| `scripts/concurrency_test.py` | N processes on one engine each get their own page; owner-scoped `closeextra`; stale tags (#14, #15) |
| `scripts/recovery_test.py` | SIGKILLed browser: `/status` tells the truth, the next op relaunches; reattach keeps the same Chrome (#33) |
| `scripts/memory_test.py` | sockets, fds and RSS return to baseline (#23). `--shed`, `--high-water`, `--cap`, `--retire`, `--recycle`. |
| `scripts/profile_test.py` | a persistent profile survives a clean restart and a SIGKILL (stale `SingletonLock` healed) |
| `scripts/display_test.py` | display adoption; an engine that dies at startup fails fast with its log (#53) |
| `scripts/search_fallback_test.py` | DuckDuckGo block → retry → Brave, logged (#34) |
| `scripts/logroll_test.py` | log rotation (#39), offline |
| `scripts/frontier_test.py` | `--max-per-host`, claim leases, priority, `--exclude`, dead links (#48), offline |
| `scripts/rank_test.py` | `crawl.rank` offline, then a ranked crawl on a local site (#2) |
| `scripts/crawl_stall_test.py` | a crawl survives a page that blows the memory cap; the watchdog cuts a stuck batch (#27) |
| `scripts/download_latency.py` | time-to-headers and read gaps on real downloads: the source of `download()`'s timeouts (#30) |
| `scripts/op_latency.py` | per-op duration percentiles from the engine log: the source of the tab timeouts (#29) |
| `scripts/engine_inspect.py` | read-only heap probe of a running engine (`sys.remote_exec`) |
| `scripts/cleanup_orphans.py` | finds browsers left by a dead engine; only engine/playwrong profiles can match |
| `scripts/check_docs.py` | docs match code: paths exist, every CLI flag, MCP tool and script documented |
| `scripts/release.py` | builds, installs each artifact in a clean venv and fetches a page with it before uploading |
| `scripts/install.py` | deps, `playwrong` on PATH, Claude Code + VS Code MCP registries |

Before committing: `python scripts/mcp_selftest.py --port 8739 --shutdown`,
`python scripts/check_docs.py`, `uvx ruff check .`.

## Licence

MIT, see [LICENSE](LICENSE).

Use it lawfully: respect the terms of the sites you fetch, and don't point it at anything you have no
right to access. It exists so that legitimate access (research, archiving, your own accounts and
sites) isn't blocked by a bot check aimed at someone else.
