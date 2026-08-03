# playwrong as an MCP server

**The point: an agent should not have to read anything to use this.** Register the MCP server once
and the browser ops show up in the agent's tool list, already described, already auto-starting. No
API doc, no `ensure_server()` script, no `PYTHONPATH` juggling, no tab bookkeeping.

The one-call version of everything this project does:

```
fetch("https://example.com")  ->  "# Example Domain\nURL: https://example.com/\n\nThis domain is …"
```

That single call starts the HTTP engine if it isn't up, launches Chrome if it isn't up, opens its own
tab, navigates, detects and clears a Cloudflare Turnstile wall, converts the HTML to readable text,
**closes its tab**, and returns. Behind it is the same unchanged `engine/server.py` — `engine/mcp_server.py`
is a thin JSON-RPC-over-stdio proxy, stdlib only.

## Install on a fresh machine

Four commands, starting from nothing:

```sh
git clone https://github.com/awtoau/awto-playwrong && cd awto-playwrong
python3 -m venv .venv                                  # optional but recommended
.venv/bin/python scripts/install.py --deps --link --register --vscode   # deps, CLI, both registries
.venv/bin/python scripts/mcp_selftest.py                               # prove it (32 assertions)
```

Then restart your MCP client. Tools appear as `mcp__playwrong__fetch`, `…__screenshot`, and so on.

**What you actually need on the box:** Python 3.11+, Google Chrome or Chromium, and a display
(the browser runs **headed** on purpose — headless is the Turnstile tell). Everything else is either
stdlib or vendored: the only two PyPI packages are `websockets` and `Deprecated`, both pulled in by
the in-tree `vendor/nodriver`. The crawl/ library's heavier deps (SQLAlchemy, psycopg, zstandard) are
**not** needed for browser driving.

If anything is missing, `scripts/doctor.py` tells you which one and prints the literal command that
fixes it:

```sh
python scripts/doctor.py
```

It checks: Python version, the two deps, the vendored nodriver import, a Chrome binary (per-platform
paths), `DISPLAY`/`WAYLAND_DISPLAY`, a writable `tmp/`, the MCP server compiling, and whether the
engine port is free or already serving. Exit code 0 = ready.

### VSCode has a SEPARATE registry — this is the #1 "it didn't install" confusion

**Claude Code and VSCode keep entirely different MCP registries.** `claude mcp add` writes
`~/.claude.json`, and the server genuinely works in Claude Code — but **VSCode's MCP panel will show
nothing**, which reads as a failed install when the tools are actually running fine.

They also use different schemas:

| | Claude Code | VSCode |
|---|---|---|
| file | `~/.claude.json` | `~/.config/Code*/User/mcp.json` (Linux), `~/Library/Application Support/Code*/User/mcp.json` (macOS), `%APPDATA%\Code*\User\mcp.json` (Windows) — or `.vscode/mcp.json` per workspace |
| key | `"mcpServers"` | `"servers"` |
| entry | `{command, args}` | `{"type": "stdio", command, args}` |

**And VSCode's MCP config is PER-PROFILE.** This is the part that wastes an afternoon: if a folder is
bound to a profile (Settings Profiles — "python", "flutter", …), that window reads
`User/profiles/<id>/mcp.json` and **never looks at `User/mcp.json`**. Register at the user level only
and the panel stays empty in exactly the window you're working in — identical symptoms to a broken
install. The profile ↔ workspace mapping lives in `User/globalStorage/storage.json` under
`profileAssociations.workspaces`.

Insiders and stable are separate installs with separate files, and people run both. `install.py
--vscode` writes the default config **and every profile's**, naming each as it goes, with a
timestamped backup of each, preserving existing `inputs`/`servers` and indent style:

```sh
python scripts/install.py --register --vscode      # both registries at once
```

VSCode reads `mcp.json` at startup, so **reload the window** (Command Palette → *Developer: Reload
Window*) before expecting it in the panel. Claude Code likewise picks the server up on restart.

### Registering by hand

`install.py --register` shells out to `claude mcp add` (scope `user`, so it works in every project),
because that writes whatever config file your version of Claude Code actually reads. For any other
MCP client, get the block and paste it in:

```sh
python scripts/install.py --print-config
```

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

`--write-config <path>` merges that into an arbitrary client config, keeping a timestamped backup.

**The interpreter path must be absolute and must be the one with the deps.** MCP clients start the
server with no shell and no PATH of yours — a bare `python` is a coin flip. `install.py` uses the
interpreter that ran it, which is why you invoke it with the venv's python.

Options worth knowing: `--scope local|user|project`, `--name <other>` (if you want two registrations),
`--port N` (pins `PH_PORT` for this registration, e.g. a second isolated browser), `--link` (puts the
`playwrong` command on your PATH), `--vscode` (the separate VSCode registry, above).

### Persistent profiles
By default each engine launch gets a throwaway Chrome profile. To keep logins and cleared sessions
across restarts, name a profile: `PH_PROFILE=work` in the MCP entry's `env`, or `playwrong --profile
work <url>` on the CLI. The name resolves to `$XDG_DATA_HOME/playwrong/profiles/<name>` and each name
gets its own engine on its own stable, name-derived port — a Chrome profile is fixed at launch, so
one engine serves exactly one profile. A stale `SingletonLock` left by a crash used to hang the next
launch forever; the engine now clears it automatically if its owning process is gone.

## The tools

| Tool | What it's for |
|---|---|
| **`fetch`** | **The 90% tool.** One page -> readable text. Own tab, auto-solve, auto-cleanup. |
| **`pdf`** | A PDF that plain HTTP can't reach, as text: clears the wall, downloads through the cleared session (cookies + matching User-Agent), extracts the text. |
| **`prefetch`** + **`collect`** | **A list of urls.** Fires them all off (8 tabs by default), returns a job id immediately; `collect` hands you pages as they finish. Don't loop `fetch` — see below. |
| **`search`** | DuckDuckGo results (title + url) through the real browser. Needed because DDG now answers curl-like clients with an image CAPTCHA instead of results — see below. |
| `screenshot` | PNG the agent can actually look at. With a url it uses its own tab; without, it shoots the current page. |
| `goto` | Navigate the *current* tab and keep it open — starts an interactive session. |
| `read` | Re-read the current page after something changed it. |
| `js` | Evaluate an expression in the page. The precise tool: pull one field instead of the whole document. |
| `click` / `key` | Synthetic CDP input at viewport coordinates / a single key. |
| `solve` | Manual Turnstile clear (`fetch` and `goto` already do it automatically). |
| `cookies` | The shared browser's cookies, including a cleared `cf_clearance`. |
| `tabs` / `close_tab` | Enumerate tabs; close one, or `close_extra` to clean up after an aborted run. |
| `status` | Engine up? Chrome up? How many tabs? Diagnostic only — nothing needs it first. |

`mode` on the text-returning tools: `text` (default), `text+links` (keeps hrefs as `anchor <url>` so
you can navigate without a second round-trip), or `html` (raw source, when you need markup).

### Why `prefetch`/`collect` exist — don't loop `fetch`

A page load is almost entirely **waiting**. Looping `fetch` over ten urls serialises ten waits, and
one slow page blocks every page behind it. Measured on eight real urls: **119s sequential vs 3.5s for
seven of the eight in parallel.**

```
prefetch(urls=[...20 urls...])   -> "started job1: 20 urls loading, 8 at a time"
   ...go do other work, or just call collect...
collect(job="job1")              -> "[6 ready, 14 still loading]" + the six pages
collect(job="job1", wait=10)     -> blocks up to 10s for the next batch
```

Results come back in **completion order**, are handed over exactly once, then forgotten engine-side
(a batch of pages is tens of MB of HTML; holding it after you have read it is a slow leak).

**A stalled url costs one slot, never the batch.** Each url has its own deadline (`timeout`, 30s
default). Some real pages never fire a load event at all — `pypi.org` sits on an open connection long
after the content has rendered — so on timeout whatever *did* render is captured and marked partial
rather than thrown away. Raise `timeout` for urls behind a Cloudflare challenge, since a solve
legitimately spends 10-30s.

The same thing from the shell and from Python:

```sh
playwrong -j 8 url1 url2 ... url20      # 8 at a time, printed as each finishes
```
```python
for page in connect.fetch_many(urls, concurrency=8):   # a generator: use the first while the
    print(page["text"])                                 # rest are still loading
```

### Why `pdf` exists

"Don't open PDFs in the browser" is right — Chrome's PDF viewer can't be driven via JS — but it is
only half the advice. For a PDF **behind a bot wall**, curl alone gets the block page (often written
to disk as a `.pdf` that won't open), and the browser can't be driven. Neither tool works alone.

`pdf` does the sequence that works: clear the challenge in the real browser → read the cookies →
fetch the file over plain HTTP with that jar *and the browser's exact User-Agent*. For an
unprotected PDF, plain `curl -sL <url> -o f.pdf && pdftotext -layout f.pdf -` is still fine and
cheaper. `connect.download(url, path)` does the same for any file type, and
`connect.session_headers(url)` hands back just the headers if you want to drive the transfer.

### Why `search` exists

The standard keyword-search recipe — `curl 'https://lite.duckduckgo.com/lite/?q=…'` — no longer
returns results. As of 2026-07-31 DDG answers curl-like clients with **HTTP 202 and an image CAPTCHA**
("Please complete the following challenge… Select all squares containing a duck") on both the `lite`
and `html` endpoints. It is not an error status, so a script that only checks for HTTP 200 silently
treats the challenge page as results.

A real headed Chrome is **not challenged at all** — nothing is solved or bypassed, the browser simply
looks like a browser. `search` runs the same query through it and unwraps DDG's `/l/?uddg=` redirector
so you get real destination urls. Then `fetch` whichever one you want.

### Why text and not HTML

`/text` on the raw engine hands back the full document — commonly 300–500KB. Converting server-side
puts ~5KB of readable content in the agent's context instead, which is the difference between "one
page" and "one page and no room to think about it". Ask for `html` when you actually need markup.

## The same thing, without an agent

The MCP tools are one of three front doors onto `engine/connect.py`. Same code path, same auto-start,
same cleanup:

```sh
./playwrong https://awto.au              # shell
```
```python
from engine import connect               # python
print(connect.capture("https://awto.au")["text"])
```

None of them need a server started first. If you find yourself writing `ensure_server()` or exporting
`PYTHONPATH=vendor`, you're reimplementing `connect.ensure()`.

## Many agents, one browser

**You do not need an engine per agent.** One shared browser is the point — it holds one cleared
Turnstile session, one cookie jar, one set of logins, and costs one Chrome's worth of memory. What
you *do* need is for each agent to name the tab it is driving, which is now automatic.

Every tab carries a **tag**, and every driving op takes a `tab` argument. An agent's ops therefore
act on its own page regardless of what anyone else is doing:

| Tool | Which tab |
|---|---|
| `fetch`, `pdf`, `search`, `screenshot(url)` | open a freshly tagged tab, use it, close it — nothing shared, nothing to clean up |
| `goto`, `read`, `js`, `click`, `key`, `solve`, `screenshot()` | **this agent's own tab**, opened on first use and reused |
| `close_tab` with no arguments | closes *your* tab, never anyone else's |
| `tabs` | lists every tab with its `tag`, so you can see who owns what |

Nothing about this needs configuring. Each MCP server process takes an identity at startup and tags
accordingly, so N agents pointed at the same engine simply work.

### Why this was needed

Before tagging, every op acted on the engine's single **active** tab. Two agents interleaved: A
issued `goto`, B's `goto` moved the active tab, and A's read returned **B's page** — as a
plausible-looking answer to A's url. No error, no warning. It was caught when a release check fetched
`example.com` and got the unrelated page the human user happened to be browsing at that moment.

`scripts/concurrency_test.py` is the regression guard: it runs N processes against one engine and
asserts each got the url it asked for and that no tabs leaked.

```sh
python scripts/concurrency_test.py -n 12
```

### When you DO want a separate engine

Tags solve contention, not isolation. Use a separate engine when agents need genuinely different
browser state:

- **different logins or cookie jars** → `--profile work` / `--profile scrape` (each name gets its own
  persistent profile and its own engine on a stable, name-derived port)
- **a throwaway session that must not touch your real one** → `--port 8799`
- **true parallel throughput** beyond what one browser can serialise → several engines, or attach
  your own nodriver via the engine's `/cdp` endpoint and drive many tabs yourself

### Rules to give an agent

1. Use `fetch` for a page. It handles its own tab; there is nothing to clean up.
2. For interactive work, just call `goto`/`js`/`click` — you already have your own tab.
3. Call `close_tab` when finished with an interactive session.
4. **Never** `shutdown` the engine, `pkill` Chrome, or launch your own browser. It is shared.
5. If you need different cookies or logins from another agent, ask for a `--profile`, not a fight
   over the same one.

## Design notes

- **Thin proxy, not a rewrite.** Tools are shims over `engine/connect.py`, which POSTs to the
  unchanged `engine/server.py`. The engine's verbs and `engine/client.py` still work exactly as
  before; see [AGENT-API.md](AGENT-API.md) for the HTTP contract.
- **One implementation of "start it and drive it".** The CLI, the MCP server, and your own scripts
  all go through `connect.py`. When each caller had its own copy, they drifted — and the copies in
  the docs drifted furthest.
- **Stdlib only.** `engine/mcp_server.py` imports nothing outside the standard library, so it starts
  instantly and can report a broken *engine* install as a tool error rather than failing to load.
- **stdout is the wire.** MCP stdio is newline-delimited JSON-RPC on stdin/stdout. Diagnostics go to
  stderr; the spawned engine's output is redirected to `tmp/logs/playwrong-engine.log`. Nothing else
  may print.
- **Tabs cannot leak from `fetch`.** Open and close happen inside one call, and the close is
  *verified* against a refreshed tab list rather than assumed.
- **`shutdown` is deliberately not exposed as a tool.** The engine is shared; stopping it kills every
  other agent's cleared Turnstile session. It stays available on the HTTP port for you.
- **Concurrency.** The engine has one globally-active tab, so a capture is serialised by a lock
  inside this process. Two *separate* MCP server processes (two agents) driving one engine can still
  interleave. If you need genuine parallelism: run a second engine on another `PH_PORT` (register a
  second MCP entry with `--port`), or attach your own nodriver to the shared browser via the engine's
  `/cdp` endpoint — see the sharding section of [AGENT-API.md](AGENT-API.md).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Tools work in Claude Code but the **VSCode panel is empty** | Two separate registries — see above. Run `install.py --vscode`, then reload the window. |
| Ran `--vscode`, panel *still* empty | VSCode MCP config is per-profile. The window's profile has its own `mcp.json`; writing only `User/mcp.json` does nothing for it. `--vscode` now writes every profile — re-run it and reload. |
| Chrome windows piling up / lots of RAM gone | Orphaned browsers from an engine that died without closing its own. `python scripts/cleanup_orphans.py` reports them, `--kill` closes them. It only ever touches Chrome on a nodriver temp profile (`/tmp/uc_*`) or a playwrong profile, so your own browser can never match. |
| Tools missing after registering | Restart the MCP client. Check with `claude mcp list`. |
| Every tool errors with "engine did not bind" | Run `python scripts/doctor.py` — nearly always a missing dep or no Chrome. Full engine output is in `tmp/logs/playwrong-engine.log`. |
| Server starts, browser never appears | No `DISPLAY` (headed Chrome needs one), or a stale `SingletonLock` in a persistent `PH_PROFILE_DIR` — see AGENT-API.md. |
| Port 8731 taken by something else | Register with `--port 8732`, or export `PH_PORT`. |
| Turnstile not clearing | Raise `tries`; confirm the browser is headed (it must be) and that you're not running a second competing Chrome. |
| A PDF url returns junk | Don't fetch PDFs here. `curl -sL <url> -o f.pdf && pdftotext -layout f.pdf -`. The browser's PDF viewer can't be driven reliably. |

## Testing changes to this layer

```sh
python scripts/mcp_selftest.py --offline                    # protocol only, no browser
python scripts/mcp_selftest.py                              # + live page, tab-leak assertion
python scripts/mcp_selftest.py --cloudflare                 # + a real Turnstile wall
python scripts/mcp_selftest.py --port 8739 --shutdown       # isolated engine, stopped after
```

Only pass `--shutdown` with an isolated `--port`: the default engine is shared.
