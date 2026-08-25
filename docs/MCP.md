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

## Every url goes through here. No exceptions.

Not `curl`, not `wget`, not `urllib`/`requests`, not the client's own web-fetch tool — **including a
url you already have, a raw file, or one that looks simple.** The whole value of this project is
that the fetch comes from a real browser; a fetch made any other way silently gives up that value,
and "silently" is the operative word. Every failure below returned success:

| What was fetched | What arrived | How it looked |
|---|---|---|
| A vendor datasheet url | HTTP **200** whose body is a JS redirect to a login page | 200 OK, file saved |
| The same datasheet | **10 pages** of a **45**-page document | A complete-looking PDF |
| A DuckDuckGo query | **HTTP 202** and an image CAPTCHA | Not a 200, but not an error either |
| A page that renders client-side | The empty shell before JS ran | Valid HTML, no content |

The second one is the expensive kind: the missing section read as *the part not having the feature*
rather than *the document not covering it*, and the wrong conclusion nearly got published. A status
code cannot tell you any of this. A real browser is served the real page in all four cases.

So: `fetch` for a page, `pdf` for a PDF, `download` for **any other file you want to keep**,
`search` to find a url, `prefetch`/`collect` for a list, `goto`/`read` when you need to stay on the
page. If something here
doesn't work, that is a bug worth reporting — not a reason to reach for curl, which just moves the
failure somewhere nobody will notice it.

The one thing curl is still for: talking to the engine's own local HTTP port (`curl -s
localhost:8731/goto -d …`), which is this project, not a bypass of it.

## Install on a fresh machine

Four commands, starting from nothing:

```sh
git clone https://github.com/awtoau/awto-playwrong && cd awto-playwrong
python3 -m venv .venv                                  # optional but recommended
.venv/bin/python scripts/install.py --deps --link --register --vscode   # deps, CLI, both registries
.venv/bin/python scripts/mcp_selftest.py                               # prove it (44 assertions)
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
| **`pdf`** | **Any PDF** — as text, and as a file you keep. Clears the wall, downloads through the cleared session (cookies + matching User-Agent), saves the file to `path`, extracts the text, reports bytes + page count + post-redirect url. |
| **`download`** | **Any non-page file** — firmware, archives, installers. Streams to disk through the cleared session; reports path, bytes, sha256, content-type, post-redirect url. `expect_sha256` verifies against a publisher's value. |
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

### Why `pdf` exists — and why not curl

"Don't open PDFs in the browser" is right — Chrome's PDF viewer can't be driven via JS — but it is
only half the advice. For a PDF **behind a bot wall**, curl alone gets the block page (often written
to disk as a `.pdf` that won't open), and the browser can't be driven. Neither tool works alone.

`pdf` does the sequence that works: clear the challenge in the real browser → read the cookies →
fetch the file over plain HTTP with that jar *and the browser's exact User-Agent* → keep the file →
extract the text. `connect.download(url, path)` does the same for any file type, and
`connect.session_headers(url)` hands back just the headers if you want to drive the transfer.

**Use it for every PDF, including ones that look unprotected.** This page used to say curl was "fine
and cheaper" for an unprotected file. That advice was wrong in a way worth spelling out, because
whether a url is protected is *not knowable before you fetch it*, and all three ways it goes wrong
are silent:

- **A 200 that is a login page.** A vendor datasheet url returned HTTP **200** whose body was a
  JavaScript redirect to a support login. Nothing errored; the login page was saved under the
  datasheet's name. `pdf` checks the magic bytes and says `not a PDF (starts with b'<!DOC')`.
- **A short document that looks complete.** The file retrieved was **10 pages**; the real datasheet
  was **45**. The missing pages held the answer, so the absence read as *the part lacking the
  feature* rather than *the document lacking the section*. `pdf` reports the page count, which is
  what makes this visible at all.
- **A chain that needs `-k`.** One host's TLS chain fails verification, so the recipe that "works"
  becomes `curl -skL` — verification off. A real browser is never asked to make that trade.

The cost difference against curl is real but small. The cost of an undetected wrong document is not.

### Keeping a document: `path`

`pdf` returns text *and* writes the file. Pass `path` to put it somewhere permanent instead of
scratch — parent directories are created, an existing file is overwritten:

```
pdf(url="https://awto.au/datasheet.pdf", path="/abs/path/to/sources/datasheet.pdf")
  -> # datasheet.pdf
     URL: https://awto.au/datasheet.pdf
     Saved: /abs/path/to/sources/datasheet.pdf (2411873 bytes, 45 pages)
     Final URL after redirects: https://cdn.awto.au/docs/datasheet-rev-c.pdf

     <extracted text, truncated at max_chars — the saved file is always whole>
```

Relative paths resolve against the *engine's* working directory, not the caller's, so pass an
absolute one. `max_chars` truncates only the text; the file on disk is complete.

The three lines above are what a manifest wants: the **final url after redirects** (which is where
the bytes actually came from, and differs from what you asked for exactly when something unexpected
happened), the **byte count**, and the **page count** to check a later copy against. Two of the three
silent failures above are caught by that page count alone.

From the shell, same thing: `playwrong --pdf <url> -o sources/doc.pdf`. From Python:
`connect.pdf(url, path=...)` → `{path, bytes, pages, text, content_type, final_url}`.

**Not a PDF? `download`.** Same contract without the text extraction — firmware images, archives,
installers, any binary. It streams in 1 MiB chunks rather than reading the body into memory, so a
750 MB image costs nothing in RSS, and it returns a `sha256` of what actually landed:

```
download(url="https://awto.au/fw/board-1.4.2.bin", path="/abs/path/to/sources/board-1.4.2.bin",
         expect_sha256="9f2c…")
  -> Saved: /abs/path/to/sources/board-1.4.2.bin
     Bytes: 786,432,000
     SHA256: 9f2c…
     Verified against the expected value you passed.
```

`expect_sha256` is the binary equivalent of `pdf`'s page count: the check that catches a block page
or a truncated transfer *at the moment it arrives*, instead of when someone tries to flash it. On a
mismatch it raises and **keeps** the file — the wrong bytes are the evidence for what went wrong.
From Python: `connect.download(url, path=…, expect_sha256=…, expect_size=…)`.

### Pages behind a login — open the tab and ask the user

The browser is **headed and shared**, and the person you are working for is sitting in front of it.
That is the feature, not a limitation: when a page needs an account, you don't need credentials, a
cookie file, or a way around the wall. You need the human to sign in once, in the window that is
already open.

The sequence:

1. `goto(url)` — the login or target page. Unlike `fetch`, `goto` **keeps its tab open**, and the tab
   is labelled with your agent name and repo, so the user can find it (`tabs`, or the tab title).
2. **Stop and ask.** Tell the user which site is asking, what the tab is called, and that you'll
   continue when they say so. Then end your turn.
3. When they confirm, `read()` — same tab, now logged in — and carry on. `screenshot()` first if you
   want to verify the signed-in state before trusting the content.
4. The cookies are now in the shared browser, so later `fetch` calls to that site are logged in too,
   for as long as the browser lives.

**Do not wait in a loop.** No sleep, no polling `read` until the page changes — a human takes as long
as they take, and the turn is free to end. Asking and stopping is the cheapest thing you can do.

**Never handle the credentials yourself.** Don't ask the user to paste a password into the chat,
don't type one in with `js` or `key`, don't read one from a file or an env var, and don't store one.
The human types it into the real page in the real browser. Same answer for 2FA, SSO redirects and a
CAPTCHA on the login form: it's theirs to do, and it works because the browser is real.

**A login wall is not a puzzle to route around.** If a page needs an account, ask — don't go looking
for a cached copy, a print view, an AMP url or an API that leaks the same content.

**Make it survive a restart** with a named profile. The default profile is thrown away when Chrome
exits, so a login done today is gone tomorrow. `PH_PROFILE=work` in the MCP entry's env (or
`playwrong --profile work`) keeps the cookie jar — see [Persistent profiles](#persistent-profiles).
Worth setting up *before* asking someone to log in for the third time.

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

### Seeing who owns what

With one browser and several agents, the obvious question looking at a window full of tabs is *who
opened that?* — so every tab carries its owner:

```
$ playwrong --tabs
 #  OWNER                            TITLE                    URL
 0  -                                about:blank              about:blank
 1  claude@awto-vyvanse:41792        AWTO | Intelligent...    https://awto.au/
 2  mcp@awto-playwrong:41830         Capabilities             https://awto.au/capabilities
```

The same label is prefixed to the **tab title in the browser**, so the window title and tab strip
name the owner without you having to ask the API at all:

```
⟦claude@awto-vyvanse:41792⟧ AWTO | Intelligent Control
```

It is stripped from every title the API returns, so a capture, a `fetch` result and `--tabs` all show
the page's own title — the label is decoration for the human watching the screen, never data.

**Nothing has to be passed in.** The label is `agent@repo:pid`, derived from the program that is
running and the git repo it was started in — an identity you must remember to supply is one that
will be missing exactly when you need it. Override it when you want something specific:
`playwrong --agent researcher <url>`, or `PLAYWRONG_AGENT=researcher` in the MCP entry's `env`.

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

1. **Every url comes through here** — no curl, wget, urllib or built-in web-fetch, not even for a url
   you already have or a raw file. A fetch made any other way fails silently; see
   [Every url goes through here](#every-url-goes-through-here-no-exceptions).
2. Use `fetch` for a page. It handles its own tab; there is nothing to clean up.
3. Use `pdf` for any PDF — and pass `path` when the document should be kept.
4. For interactive work, just call `goto`/`js`/`click` — you already have your own tab.
5. Call `close_tab` when finished with an interactive session.
6. Page needs an account? `goto` it, ask the **user** to sign in to that tab, end your turn, `read`
   when they confirm. Never take their credentials, never poll while you wait.
7. **Never** `shutdown` the engine, `pkill` Chrome, or launch your own browser. It is shared.
8. If you need different cookies or logins from another agent, ask for a `--profile`, not a fight
   over the same one.
9. A tool here misbehaving is a bug to report against `awtoau/awto-playwrong`, not a reason to work
   around it — a workaround just leaves the next agent to rediscover it.

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
| Every tool errors with `ConnectionClosedError` | The browser died under the engine. It now relaunches on the next op by itself, so this should self-clear — if it persists, the relaunch is failing and the error says so. Was issue #8, where the engine wedged instead and `status` reported it healthy. |
| Server starts, browser never appears | No `DISPLAY` (headed Chrome needs one), or a stale `SingletonLock` in a persistent `PH_PROFILE_DIR` — see AGENT-API.md. |
| Port 8731 taken by something else | Register with `--port 8732`, or export `PH_PORT`. |
| Turnstile not clearing | Raise `tries`; confirm the browser is headed (it must be) and that you're not running a second competing Chrome. |
| A PDF url returns junk from `fetch` | Right — the browser's PDF viewer can't be driven. Use the `pdf` tool (not curl): it downloads through the cleared session and extracts the text. |
| `pdf` says "not a PDF (starts with …)" | The server returned an HTML block or login page. `fetch` the containing page first so the session is fully cleared, then retry. Do **not** fall back to curl — curl saves that same page as a `.pdf` without telling you. |
| A saved PDF has fewer pages than expected | You got a partial or substituted document. `pdf` prints the page count for exactly this; re-fetch after clearing the wall on the containing page, and record the expected count in your manifest. |

## Testing changes to this layer

```sh
python scripts/mcp_selftest.py --offline                    # protocol only, no browser
python scripts/mcp_selftest.py                              # + live page, tab-leak assertion
python scripts/mcp_selftest.py --cloudflare                 # + a real Turnstile wall
python scripts/mcp_selftest.py --port 8739 --shutdown       # isolated engine, stopped after
python scripts/recovery_test.py                             # kill the browser, prove it comes back
```

Only pass `--shutdown` with an isolated `--port`: the default engine is shared. `recovery_test.py`
always uses an isolated port and refuses to run on 8731 — it works by SIGKILLing a browser.
