# playwrong as an MCP server

Register once and the browser tools appear in the agent's tool list, already described and starting
the engine themselves. Nothing to read first, no server to start, no tabs to track.

```
fetch("https://awto.au")  ->  "# AWTO\nURL: https://awto.au/\n\n…"
```

That one call starts the engine and Chrome if they're down, opens its own tab, navigates, clears a
Cloudflare Turnstile wall, converts the page to text, closes the tab, and returns.
`engine/mcp_server.py` is a stdlib-only JSON-RPC-over-stdio layer over `engine/connect.py`.

## Every url goes through here. No exceptions.

Not curl, wget, urllib/requests or the client's own web-fetch, **including a url you already have, a
raw file, or one that looks simple.** Every failure below returned success:

| What was fetched | What arrived | How it looked |
|---|---|---|
| A vendor datasheet url | HTTP **200** whose body is a JS redirect to a login page | 200 OK, file saved |
| The same datasheet | **10 pages** of a **45**-page document | A complete-looking PDF |
| A DuckDuckGo query | **HTTP 202** and an image CAPTCHA | Not a 200, but not an error either |
| A client-rendered page | The empty shell before JS ran | Valid HTML, no content |

- A status code can't detect any of these. A real browser is served the real page in all four cases.
- The truncated PDF is the expensive one: the missing section read as *the part lacks the feature*.
- Use `fetch` for a page, `pdf` for a PDF, `download` for any other file, `search` to find a url,
  `prefetch`/`collect` for a list, and `goto`/`read` to stay on a page.
- If a tool here misbehaves, report it against `awtoau/awto-playwrong`. Don't fall back to curl.
- curl is fine for one thing: the engine's own port (`curl -s localhost:8731/status`).

## Install

```sh
git clone https://github.com/awtoau/awto-playwrong && cd awto-playwrong
python3.14t -m venv .venv                                              # 3.14+; free-threaded recommended
.venv/bin/python scripts/install.py --deps --link --register --vscode  # deps, CLI, both registries
.venv/bin/python scripts/mcp_selftest.py                               # prove it end to end
```

Restart the MCP client. The tools appear as `mcp__playwrong__fetch` and so on. Requirements are
listed in the [README](../README.md#install); `python scripts/doctor.py` checks them all and prints
the fix command for anything missing.

From an installed package, the entry point is `awto-playwrong` (or `playwrong-mcp`), e.g.
`uvx awto-playwrong`.

### VS Code has a separate registry

Claude Code and VS Code keep different MCP registries. Registering in one leaves the other's panel
empty, which looks like a failed install.

| | Claude Code | VS Code |
|---|---|---|
| file | `~/.claude.json` | `~/.config/Code*/User/mcp.json` (Linux), `~/Library/Application Support/Code*/User/mcp.json` (macOS), `%APPDATA%\Code*\User\mcp.json` (Windows), or `.vscode/mcp.json` per workspace |
| key | `"mcpServers"` | `"servers"` |
| entry | `{command, args}` | `{"type": "stdio", command, args}` |

- **VS Code's MCP config is per profile.** A window bound to a Settings Profile reads
  `User/profiles/<id>/mcp.json` and ignores `User/mcp.json`. The mapping lives in
  `User/globalStorage/storage.json` → `profileAssociations.workspaces`.
- Insiders and stable keep separate files.
- `install.py --vscode` writes the default config **and every profile's**, with a timestamped backup
  of each, keeping existing `inputs`/`servers`.
- After registering, reload the window (*Developer: Reload Window*).

### Registering by hand

`install.py --register` runs `claude mcp add` (scope `user`). For any other client, print the config
block with `python scripts/install.py --print-config`:

```json
{
  "mcpServers": {
    "playwrong": {
      "command": "/abs/path/to/.venv/bin/python",
      "args": ["/abs/path/to/awto-playwrong/engine/mcp_server.py"]
    }
  }
}
```

- The interpreter path must be **absolute** and must be the interpreter that has the deps installed.
  The client launches the server with no shell and none of your PATH.
- `--write-config <path>` merges the block into a client config, with a backup.
- Other options: `--scope local|user|project`, `--name <other>`, `--port N` (pins `PH_PORT`),
  `--link` (`playwrong` on PATH), `--vscode`.

### Persistent profiles

- By default each Chrome launch gets a throwaway profile.
- To keep a profile, set `PH_PROFILE=work` in the MCP entry's `env` (or `playwrong --profile work`).
  It lives at `$XDG_DATA_HOME/playwrong/profiles/work`.
- Each profile name gets its own engine on its own port derived from the name. A Chrome profile is
  fixed at launch, so one engine serves one profile. `PH_PORT`, if set, takes precedence (#54).
- A stale `SingletonLock` left by a crash is cleared automatically when its owner process is gone.

## The tools

| Tool | What it's for |
|---|---|
| **`fetch`** | **The 90% tool.** One page → readable text. Own tab, auto-solve, auto-cleanup. |
| **`pdf`** | **Any PDF**: text plus the file. Clears the wall, downloads with the cleared cookies and User-Agent, saves to `path`, reports bytes, page count and the post-redirect url. |
| **`download`** | **Any other file**: firmware, archives, installers. Streams to disk and reports path, bytes, sha256, content type and final url. `expect_sha256` verifies the file. |
| **`prefetch`** + **`collect`** | **A list of urls**: loads them in parallel (8 tabs by default) and returns a job id. `collect` returns pages as they finish. |
| **`search`** | DuckDuckGo results (title + url) through the real browser; Brave Search when DuckDuckGo blocks. |
| `screenshot` | A PNG the agent can look at. With a url it uses a throwaway tab; without one, it shoots your tab. |
| `goto` | Navigate **your tab** and keep it open, starting an interactive session. Clears a challenge. |
| `read` | Re-read your tab after something changed it. |
| `js` | Evaluate an expression in your tab: pull one field instead of the whole page. |
| `click` / `key` | CDP input on your tab at viewport coordinates / a single key. |
| `solve` | Manual Turnstile clear (`fetch` and `goto` already solve automatically). |
| `cookies` | The shared browser's cookies, including `cf_clearance`; `domain` filters them. |
| `tabs` / `close_tab` | List every tab with its owner. `close_tab` closes your tab; `close_extra` closes your tabs and orphans of exited agents. |
| `status` | Engine and Chrome state. Diagnostic only; no other tool needs it first. |

`mode` on the text tools: `text` (default), `text+links` (keeps hrefs as `anchor <url>`), `html`.
Text is converted server-side because a raw page is commonly 300-500 KB, against ~5 KB of text.

### `prefetch`/`collect`: don't loop `fetch`

A page load is mostly waiting. Looping `fetch` over a list serialises the waits, and one slow page
blocks everything behind it.

```
prefetch(urls=[...20 urls...])   -> "started job1: 20 urls loading, 8 at a time"
collect(job="job1")              -> "[6 ready, 14 still loading]" + the six pages
collect(job="job1", wait=10)     -> blocks up to 10 s for the next batch
```

- Results arrive in completion order, are handed over once, then are dropped engine-side.
- Each url has its own `timeout` (default 30 s), so a stalled url costs one slot. When the timeout
  fires, whatever rendered is captured and marked partial. Raise the timeout for urls behind a
  Cloudflare challenge.
- **A page still loading says so (#32).** The engine waits up to `LOAD_WAIT` (5 s) after a 2 s settle
  for `readyState == "complete"`. If the page still isn't complete, the text starts with
  `[INCOMPLETE: …]` and `capture()` returns `partial: true`. For a large file, use `download`, which
  checks `Content-Length`.
- Shell: `playwrong -j 8 url1 … url20`. Python: `for page in connect.fetch_many(urls): …`.

### `pdf` and `download`: why not curl

Chrome's PDF viewer can't be driven, and curl gets the block page. `pdf` does what works: clear the
challenge in the browser, then fetch the file over HTTP with the browser's cookies and exact
User-Agent, keep the file, and extract the text.

Use it **for every PDF**. You can't tell in advance whether a url is protected, and the failures are
silent:
- **A 200 that is a login page.** `pdf` checks the magic bytes: `not a PDF (starts with b'<!DOC')`.
- **A short document that looks complete.** `pdf` reports the page count.
- **A TLS chain that only works with `curl -k`.** The browser never has to turn verification off.

```
pdf(url="https://awto.au/datasheet.pdf", path="/abs/path/to/sources/datasheet.pdf")
  -> Saved: /abs/path/to/sources/datasheet.pdf (2411873 bytes, 45 pages)
     Final URL after redirects: https://cdn.awto.au/docs/datasheet-rev-c.pdf
     <text, truncated at max_chars; the saved file is always whole>
```

- Pass an **absolute** `path`: a relative one resolves against the MCP server's working directory.
  Parent directories are created; an existing file is overwritten.
- Final url, bytes and page count are what a manifest wants. The page count alone catches two of the
  three failures above.
- `download` is the same contract without text extraction. It streams in 1 MiB chunks, returns
  `sha256`, and with `expect_sha256` raises on a mismatch, **keeping** the file as evidence.
- Python: `connect.pdf(url, path=…)`, `connect.download(url, path=…, expect_sha256=…, expect_size=…)`,
  and `connect.session_headers(url)` for just the headers.

**Short transfers (#31):**
- Received bytes are checked against `Content-Length` (`expected_bytes` in the result).
- Short with `Accept-Ranges: bytes` → resume with `Range`, up to `RESUME_TRIES` (8) (`resumed: N`).
- Still short, or a chunked body cut off → browser fallback.
- Both fail → raises `short transfer: <got> of <expected> bytes …`, and the partial file moves to
  `<path>.partial`.
- No `Content-Length` and no chunked framing → completeness can't be checked, and the output says so.

**Browser fallback (#30).** Some CDNs stall or refuse the plain-HTTP replay even with the cleared
cookies and UA.
- Triggers: a socket timeout (`HTTP_READ_TIMEOUT`, 1 s per connect/read), a connection error,
  HTTP 403/429/503, or a short transfer that resume couldn't finish.
- It re-fetches with the page's own `fetch()` from a same-origin tab, and copies the body out in
  4 MiB chunks. The result gets `via: "browser"` and `http_error`; otherwise `via: "http"`.
- Cost: the whole body sits in the tab's memory until it has been copied out.

### Pages behind a login: open the tab and ask the user

The browser is headed and shared, and the user is sitting in front of it.

1. `goto(url)`. The tab stays open, labelled with your agent name and repo.
2. **Stop and ask.** Tell the user which site needs a sign-in and what the tab is called, then end
   your turn.
3. When the user confirms, `read()` the same tab (`screenshot()` first, if you want to check).
4. The cookies are now in the shared browser, so later `fetch` calls to that site are logged in too.

- **Don't wait in a loop.** No sleep, and no polling `read`.
- **Never handle credentials.** Not pasted into chat, not typed via `js`/`key`, not read from a file
  or env var. The same goes for 2FA, SSO and a CAPTCHA on the login form.
- **A login wall is not a puzzle.** Don't hunt for a cached copy, print view, AMP url or leaky API.
- To keep a login across restarts, use a [persistent profile](#persistent-profiles).

### `search`

- DuckDuckGo answers curl-like clients with **HTTP 202 + an image CAPTCHA** on both `lite` and `html`
  (since 2026-07-31). `search` runs the query in the real browser and unwraps `/l/?uddg=` redirects.
- Three distinct outcomes:
  - anti-bot page → raises (rate limiting)
  - no-results page → "ran, matched nothing" (#10, #12)
  - unrecognised page → raises with the byte count and title (markup drift)
- On a genuine zero with quotes/operators in the query, it **reruns a relaxed query** and labels
  those hits as answering the relaxed query.
- **DuckDuckGo block → Brave (#34):** one retry after `SEARCH_RETRY_WAIT` (2 s), then Brave through
  the same browser. Brave results are labelled; Brave loosens queries, so check that a hit contains
  your exact terms. Both blocked → raises naming both, or names headless (`PH_HEADLESS=1`) as the
  cause.
- Every attempt is logged as one JSON line in `logs/search.log` under the data dir.

## Many agents, one browser

One shared browser means one cleared Turnstile session, one cookie jar, one set of logins, and one
Chrome's worth of memory. Each agent gets its own **tagged tab** automatically:

| Tool | Which tab |
|---|---|
| `fetch`, `pdf`, `search`, `screenshot(url)` | a fresh tagged tab, closed before returning |
| `goto`, `read`, `js`, `click`, `key`, `solve`, `screenshot()` | **this agent's own tab**, opened on first use, reopened if it was swept |
| `close_tab` with no arguments | your tab only |
| `tabs` | every tab with its `tag` and `owner` |

- Each MCP server process has its own tag; N agents on one engine never read each other's pages.
  Before tags, one agent's `goto` moved the shared active tab and another agent's read returned the
  wrong page with no error. `scripts/concurrency_test.py` guards this.
- The owner label is `agent@repo:pid`, guessed from the program and git repo. Override it with
  `PLAYWRONG_AGENT` in the entry's env, or `playwrong --agent NAME`.
- The label is prefixed to the **browser tab title** (`⟦claude@awto-vyvanse:41792⟧ AWTO | …`) for the
  human watching, and stripped from every title the API returns.

```
$ playwrong --tabs
 #  OWNER                            TITLE                    URL
 0  -                                about:blank              about:blank
 1  claude@awto-vyvanse:41792        AWTO | Intelligent...    https://awto.au/
 2  mcp@awto-playwrong:41830         Capabilities             https://awto.au/capabilities
```

**`close_extra` is owner-scoped (#15):**

| Tab | `close_extra` |
|---|---|
| Yours | closed |
| Owned by an agent whose **process has exited** | closed |
| Owned by another agent that is **still running** | **spared**, reported in `skipped` |
| Tab 0, the base tab | never closed |

`force: true` closes everything except tab 0. It's meant for a human clearing the browser. The
protection is advisory, since the failure it prevents is an accident.

**When to use a separate engine:** different logins or cookie jars (`--profile`), a throwaway
session (`--port 8799`), or throughput beyond one browser. For parallel tabs in one process, attach
your own CDP client instead: see [AGENT-API.md](AGENT-API.md#parallel-tabs-attach-your-own-cdp-client).

### Rules to give an agent

1. **Every url comes through here**: no curl, wget, urllib or built-in web-fetch.
2. Use `fetch` for a page. It cleans up after itself.
3. Use `pdf` for any PDF, with an absolute `path` when the file should be kept.
4. For interactive work, call `goto`/`js`/`click`; you already have your own tab.
5. `close_tab` when finished with an interactive session.
6. Login wall: `goto`, ask the user, end the turn, then `read` once they confirm. Never touch
   credentials or poll.
7. `close_extra` spares other live agents. Leave `skipped` tabs alone; `force` is for humans.
8. **Never** `shutdown` the engine, `pkill` Chrome, or launch your own browser.
9. Need different cookies? Ask for a `--profile`.
10. A tool misbehaving is a bug to report, not something to work around.

## Design notes

- **One implementation.** The CLI, the MCP server and scripts all go through `connect.py`.
- **Stdlib only.** `mcp_server.py` imports nothing third-party, so it starts instantly and can report
  a broken engine install as a tool error.
- **stdout is the wire.** Diagnostics go to stderr. The spawned engine's output goes to
  `logs/playwrong-engine.log`.
- **`fetch` can't leak tabs.** Open and close happen in one call, and the close is checked against a
  refreshed tab list.
- **`shutdown` is not a tool.** Stopping the shared engine would kill every other agent's session.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Works in Claude Code, **VS Code panel empty** | Separate registries: `install.py --vscode`, then reload the window. |
| Ran `--vscode`, panel *still* empty | The window's Settings Profile has its own `mcp.json`. `--vscode` writes every profile: re-run it and reload. |
| Chrome windows piling up | Orphans of a dead engine: `python scripts/cleanup_orphans.py` (`--kill` closes them). Only engine/playwrong profiles can match. |
| `TabCrashed` / `renderer was killed` | That tab ran out of memory under the cap (#28). `close_tab` it; other tabs are fine. `MemoryHighWater` on a new tab: close tabs you no longer need. |
| Engine RSS climbing, host swapping | `status` shows `rss_mb`, `fds`, `cdp_sockets`. Sockets far above the tab count means a leak (#23). Run `scripts/engine_inspect.py`. Engine RSS above `PH_RSS_LIMIT` (2G) self-restarts. |
| Tools missing after registering | Restart the MCP client; `claude mcp list`. |
| "exited during startup" / "did not bind" | The error includes the end of `logs/playwrong-engine.log`. Run `python scripts/doctor.py`. |
| "browser connection lost" | The browser died. The next op relaunches it; if this persists, the error names why. |
| Browser never appears | No display. The engine adopts the user's X/Wayland session; with none at all, it fails with `no display: …`. |
| Port 8731 taken | Register with `--port 8732`, or export `PH_PORT`. |
| Turnstile not clearing | Raise `tries`; confirm the browser is headed and that no second Chrome is competing. |
| `fetch` on a PDF url returns junk | Use `pdf`. |
| `pdf` says "not a PDF" | An HTML block or login page. `fetch` the containing page first, then retry. Never fall back to curl. |
| Fewer pages than expected | A partial or substituted document. Clear the wall on the containing page, re-fetch, and record the expected count. |

## Testing this layer

```sh
python scripts/mcp_selftest.py --offline                    # protocol only, no browser
python scripts/mcp_selftest.py --port 8739 --shutdown       # isolated engine, stopped after
python scripts/mcp_selftest.py --port 8739 --cloudflare     # + a real Turnstile wall
python scripts/recovery_test.py                             # kill the browser, prove it comes back
```

Only pass `--shutdown` with an isolated `--port`; the default engine is shared.
