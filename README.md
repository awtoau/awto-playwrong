# awto-playwrong

**Fetch the pages that won't let you fetch them.**

```sh
playwrong https://awto.au          # the page, as readable text. Nothing to start first.
```

One shared, long-lived, headed Chrome — driven over a local port — that clears Cloudflare Turnstile
and stays warm between calls. A CLI, an AI agent (via **MCP**), and your own scripts all drive the
same browser and reuse the same cleared session.

**Why not just `curl`?** Because more and more pages don't answer it. Cloudflare serves a challenge;
DuckDuckGo now returns `HTTP 202` and an image CAPTCHA. A real browser isn't asked. Nothing here
solves or breaks a CAPTCHA — it drives a real browser that isn't given one, and hands you the result.

Built and hardened against real bot-walled sites, where it recovered thousands of pages that plain
HTTP fetching could not.

## Install (fresh machine)

```sh
git clone https://github.com/awtoau/awto-playwrong && cd awto-playwrong
python3 -m venv .venv                                            # optional but recommended
.venv/bin/python scripts/install.py --deps --link --register --vscode   # deps, CLI, MCP
.venv/bin/python scripts/mcp_selftest.py                               # prove it (33 assertions)
```

You need **Python 3.11+, Chrome or Chromium, and a display** — the browser runs headed on purpose
(headless is the Turnstile tell). The only two PyPI packages are `websockets` and `Deprecated`;
nodriver is vendored in-tree and everything else is stdlib. `python scripts/doctor.py` checks all of
it and prints the exact command for anything missing.

> The `crawl/` library needs more (SQLAlchemy, psycopg, zstandard — see `pyproject.toml`). You don't
> need any of that just to drive a browser.

## I'm an agent / I want this in Claude Code

Use the **MCP server** — [docs/MCP.md](docs/MCP.md). After `--register`, browser ops appear directly
in the tool list and one call does the whole job:

```
fetch("https://awto.au")   # auto-starts the engine + Chrome, clears Cloudflare,
                           # returns readable text, closes its own tab
```

Also: `prefetch`/`collect` (a list of urls, loaded in parallel, read as each finishes), `search`,
`pdf`, `screenshot`, `goto`, `read`, `js`, `click`, `key`, `solve`, `cookies`, `tabs`, `close_tab`.
No script to write, no API doc to read, no tab bookkeeping. `engine/mcp_server.py` is a stdlib-only proxy in
front of the same engine described below — driving it over HTTP still works exactly as before.

## I just want to view / fetch one web page

```sh
./playwrong https://awto.au
```

That's it. There is **nothing to start first** — no server to launch, no `PYTHONPATH`, no port to
pick. The first command starts the engine and a real headed Chrome; the page comes back as readable
text.

```sh
./playwrong --search "nodriver turnstile"  # DuckDuckGo results (curl gets a CAPTCHA now)
./playwrong --pdf https://awto.au/doc.pdf # a PDF from behind a bot wall, as text
./playwrong --profile work https://awto.au # persistent named profile: logins survive
./playwrong https://awto.au https://awto.au/capabilities   # several, one warm browser
./playwrong https://awto.au --links       # keep hrefs, so you can pick the next url
./playwrong https://awto.au --html        # raw markup instead of text
./playwrong https://awto.au --shot p.png  # also save a screenshot
./playwrong https://awto.au --json        # {text,title,url,challenge} for scripting
./playwrong --status                      # what's running
./playwrong --stop                        # stop the shared engine
```

### Every option

| Flag | What |
|---|---|
| `-j N`, `--jobs N` | load N urls **in parallel**, printing each as it finishes. A page load is mostly waiting; this overlaps it. |
| `-s`, `--search Q` | DuckDuckGo results as `title + url` (curl gets a CAPTCHA; a real browser doesn't) |
| `-n N` | max search results (default 10) |
| `--pdf URL` | download a PDF **through the cleared session** and print its text |
| `-o`, `--out PATH` | where `--pdf` / `--shot` writes (default: the cache dir) |
| `--links` / `--html` | keep hrefs as `anchor <url>` / return raw markup instead of text |
| `--max-chars N` | truncate each page (`0` = no limit; default 40000) |
| `--shot PATH` | also save a PNG. Several urls get `name-0.png`, `name-1.png`. |
| `--json` | machine-readable output instead of text |
| `--no-solve` | don't auto-clear Cloudflare challenges |
| `--tries N` | max challenge-solve iterations (default 20) |
| `--profile NAME` | persistent named Chrome profile — logins survive a restart |
| `--agent NAME` | how this caller is labelled in tab titles and `--tabs` |
| `--tabs` | list every open tab with the agent and repo that opened it |
| `--port N` | use/start an engine on another port (a second, isolated browser) |
| `-q`, `--quiet` | no progress notes on stderr |
| `--status` / `--stop` | what's running / stop the shared engine |

Progress notes go to stderr and the page to stdout, so `playwrong <url> > page.txt` stays clean.

**You never manage a host:port.** The engine is a long-lived local server on a fixed default port
(`PH_PORT`, 8731). The first command starts it; every later command finds it already up and reuses
it — same browser, same cookies, same cleared Turnstile session, no cold start (~3s cold, ~2s warm).
Fire one command per url whenever you like and they compound. `--port` is only for deliberately
running a *second*, isolated browser.

`scripts/install.py --link` puts `playwrong` on your PATH so you can drop the `./`.

### Driving a page, not just reading it
`engine/client.py` has the interactive verbs — `goto`, `click`, `key`, `js`, `shot`, `tabs`,
`solvecf`. It auto-starts the engine too:

```sh
python engine/client.py goto https://awto.au
python engine/client.py js "document.title"       # promises resolve; objects come back as JSON
python engine/client.py read --links              # current page as text, hrefs kept
python engine/client.py shot page.png
```

Or POST directly to the port from anything: `curl -s localhost:8731/goto -d '{"url":"..."}'`. The
browser stays alive between calls. The rest of this README is for crawling many pages.

## Testing and maintenance

Every one of these runs standalone and prints a pass/fail tally.

| Script | What it proves |
|---|---|
| `scripts/doctor.py` | preflight: Python, deps, vendored nodriver, Chrome, display, port. Prints the exact fix command for anything missing. |
| `scripts/mcp_selftest.py` | the MCP layer over real stdio JSON-RPC: protocol, a live fetch, tab cleanup. `--cloudflare` adds a real Turnstile wall. |
| `scripts/stress_test.py` | the edge cases: duplicate and bad urls, empty batches, double collect, missing tabs, a batch running while someone else drives, malformed requests. |
| `scripts/concurrency_test.py` | N processes against ONE engine each get the page they asked for, with no leaked tabs. |
| `scripts/frontier_test.py` | `--max-per-host` holds in all three claim orderings, and survives a resume. No browser or network needed. |
| `scripts/cleanup_orphans.py` | finds and closes browsers left behind by a dead engine. Only ever touches nodriver temp profiles, so your own browser can't match. |
| `scripts/check_docs.py` | the docs describe the code that exists: no dead paths, no removed files, every flag and tool real and documented. |
| `scripts/release.py` | builds, then **installs each artifact into a clean venv and fetches a page with it** before it will upload. `twine check` only validates metadata — it passed the whole time the wheel was broken. |
| `scripts/install.py` | deps, the `playwrong` command on PATH, and both MCP registries. |

## Why this exists
Browser automation kept getting rebuilt per-project. This is the shared home: a **running server**
you drive over **IP:port** — `goto a page → get back {html, cookies, status, screenshot, metadata}` —
that any number of apps/agents can share. No DB, no project specifics: pure capture. Wire your own
data layer on top (the project keeps its DB code; this stays generic).

## Why nodriver, and not Playwright
**Playwright is the detection tell.** Cloudflare serves it a dead challenge that never resolves, no
matter how the automation is configured. nodriver drives a real Chrome over raw CDP and passes. The
browser also runs **headed** deliberately — headless is itself a signal. That is the whole
architectural choice, and it's why this repo exists rather than a Playwright wrapper.

### The pieces
- `engine/server.py` — the engine: one persistent **headed real Chrome** (nodriver), driven over HTTP
  on a port. Holds the browser, the cleared session, and every verb.
- `engine/connect.py` — **the one place that reaches the engine and starts it if it's down**, plus
  html→text, one-shot `capture()`, `search()` and `download()`/`pdf()`. Every entry point goes
  through it, so "how do I start this thing" has exactly one answer.
- `engine/cli.py` + `./playwrong` — the one-shot command: url in, readable page out.
- `engine/client.py` — the interactive port client (`goto/click/key/js/read/tabs/…`).
- `engine/mcp_server.py` — the MCP stdio server for agents ([docs/MCP.md](docs/MCP.md)).
- `crawl/` — an optional library for crawling many pages on top of the engine (its own heavier deps).
- `vendor/nodriver` — patched nodriver 0.50.3 (fixes a non-UTF-8 byte in `cdp/network.py` line ~1345
  that raises `SyntaxError` on import under CPython 3.14). Upstream: issue
  [ultrafunkamsterdam/nodriver#35](https://github.com/ultrafunkamsterdam/nodriver/issues/35) + fix PR
  [#36](https://github.com/ultrafunkamsterdam/nodriver/pull/36) (both open/unmerged). **Drop the vendor
  pin once #36 merges and a fixed release ships.**

## Usage (engine)
```
# there is no step 1 — every entry point starts the engine and Chrome if they're down
./playwrong https://awto.au                      # one page as text
python engine/client.py goto https://awto.au
python engine/client.py solvecf                  # clear a Turnstile challenge
python engine/client.py text                     # get the page HTML
python engine/client.py shot frame.png           # screenshot
```
From your own Python, use the same code path the CLI and the MCP server use — `engine/connect.py`:

```python
from engine import connect

page = connect.capture("https://awto.au")        # starts engine+Chrome, solves, closes its tab
print(page["text"])                              # {text, title, url, challenge}
```

| Function | What |
|---|---|
| `capture(url, mode=, max_chars=, solve=)` | one page, own tab, auto-solve, auto-cleanup |
| `fetch_many(urls, concurrency=8)` | a **generator** — every page in parallel, yielded as each finishes |
| `prefetch(urls, concurrency=, timeout=)` | start a batch, return a job id immediately |
| `poll(job)` | counts only: ready / loading / pending / errors / done |
| `collect(job, wait=, max_chars=)` | take whatever is ready; each page delivered exactly once |
| `search(query, max_results=)` | DuckDuckGo results as `[{title, url}]` |
| `pdf(url)` / `download(url, path=)` | a file from behind a bot wall, via the cleared session |
| `session_headers(url)` | just the `{Cookie, User-Agent, Referer}`, to drive a transfer yourself |
| `ensure(port=, profile=)` | engine + Chrome up; idempotent, and safe when many processes call it at once |
| `call(op, **body)` / `op(op, **body)` | any raw engine verb, without / with an auto-start |

The crawl library adds `CrawlDB.host_counts()` — pages already attempted per host, which is what makes
`--max-per-host` survive a resume.
Launching `engine/server.py` by hand still works if you want it in the foreground — and it needs no
`PYTHONPATH`, since it puts `vendor/` on `sys.path` itself.
Or POST directly: `POST http://127.0.0.1:8731/goto {"url": "..."}` → returns the capture.

## The capture contract (what you get back)
`goto` / `capture` returns: **html, title, status, cookies, screenshot (base64), and metadata**
(timing, passed-challenge flag, request counts). Wire your own storage/DB on top — this engine never
touches a database.

## crawl/ — the reusable crawl LIBRARY (on top of the server)

Where `engine/` captures ONE page over a port, `crawl/` is a **library of crawl mechanics** for
walking a whole site — still ref-free (zero project names), so any consumer reuses it. It attaches to
the same shared browser.

| module | what |
|---|---|
| `crawl.browser` | attach nodriver to the shared engine Chrome via `/cdp` (starts the server if down) |
| `crawl.challenge` | Cloudflare "verify you are human" detect + `solve()`; generic soft-404 matcher |
| `crawl.netblock` | CDP Fetch resource-type block — **one pattern per blocked type** so page load never stalls (the naive `url_pattern="*"` version froze `readyState=loading` → empty captures; see the docstring) |
| `crawl.render` | consent-dismiss (`dismiss_overlays`) + `wait_ready` (readyState) + `wait_for_render` (client-mount) |
| `crawl.parse` | clean words-only text, page links, image refs; entity-decoding; segment-boundary feed/infra URL filter; generic `image_kind` (consumer supplies vertical kinds via `extra_rules`) |
| `crawl.store` | content-addressed zstd page store (sha256, sharded) — no DB |
| `crawl.db` | **SQLAlchemy 2.0 Core** relational store: SQLite (a file) / Postgres / MySQL from one model. Atomic `claim()`, `reclaim_stuck()`, portable `scan_status` CHECK, and an `unhandled` feedback table |
| `crawl.graph` | reports over the page→page reference graph: hubs / authorities / orphans / dead-links / `image_usage` / `improvement_report` |
| `crawl.run` / `crawl.report` | point-and-go CLI: crawl a site → SQLite + auto site-shape report |
| `crawl.drive` | hand-drive helpers (click / scroll / screenshot; settles without a fixed sleep) |
| sibling `assets/` | content-addressed image/binary store (store / classify / imgmeta) |

Point-and-go (one command rips a site + prints its shape):
```
python -m crawl.run --seed https://example.com/ --db site.sqlite --max 200 --max-per-host 50 --tabs 8
python -m crawl.report --db site.sqlite            # re-print the report later
```
`--db` also takes `postgresql+psycopg://…` or `mysql+pymysql://…`. See `crawl/AGENTS.md` for the full
agent guide and `schema.sql` for the relational model.

## Install (system-wide tool — no venv to activate)

Install the crawl library as an isolated tool so `crawl` / `crawl-report` are on your PATH from
anywhere, with the deps kept out of system Python:

```
DISABLE_SQLALCHEMY_CEXT=1 uv tool install \
  --python /usr/bin/python3.14t \
  --no-binary-package sqlalchemy \
  git+https://github.com/awtoau/awto-playwrong     # or a local checkout path

crawl --seed https://example.com/ --db site.sqlite --max 200 --max-per-host 50 --tabs 8
```

Every flag is load-bearing on a free-threaded (no-GIL) machine — omit one and the GIL comes back on:
- `--python /usr/bin/python3.14t` — build the tool env on the **free-threaded** interpreter (plain
  `python3.14` has the GIL; `uv` would otherwise pick it and no-GIL is silently lost).
- `--no-binary-package sqlalchemy` + `DISABLE_SQLALCHEMY_CEXT=1` — build SQLAlchemy from source with
  its C extension off; the prebuilt wheel's `cyextension` re-enables the GIL on import.
- The patched **nodriver is bundled** in the wheel (`crawl/_vendor/nodriver`) and `import crawl`
  prepends it to `sys.path`, so no `PYTHONPATH` juggling — `import crawl` then `import nodriver` gets
  the fixed copy. (A bare `import nodriver` without importing `crawl` first still finds the broken
  PyPI copy; always go through `crawl`.)

Verify after install: `python -c "import sys, crawl; import sqlalchemy.util as u; assert not
sys._is_gil_enabled() and not u.has_compiled_ext()"` on the tool's interpreter.

For development, an editable install in a free-threaded venv works too:
`uv pip install -e .` after `DISABLE_SQLALCHEMY_CEXT=1 uv pip install --no-binary sqlalchemy "sqlalchemy>=2.0"`.

## Gotchas — read before running
- **nodriver import landmine.** Upstream `nodriver/cdp/network.py` has a non-UTF-8 byte (line ~1345)
  that raises `SyntaxError` under **Python 3.14** (both the GIL and free-threaded builds — 3.14 tightened
  the source tokenizer to reject non-UTF-8 bytes with no encoding declaration; 3.13 and earlier were
  lenient). `vendor/nodriver` here is patched, and `engine/server.py` puts it FIRST on `sys.path`, so
  the patched copy wins over any `site-packages` install automatically. If you import nodriver
  yourself in another process, make sure `vendor/` leads there too.
- **SQLAlchemy on no-GIL.** Its `cyextension` C module silently re-enables the GIL. Install the
  pure-Python build: `DISABLE_SQLALCHEMY_CEXT=1 pip install --no-binary SQLAlchemy "SQLAlchemy>=2.0"`.
  Verify `not sys._is_gil_enabled()` after import. Drivers must be pure-Python too: `psycopg` (not
  `[binary]`/`[c]`), `pymysql`, stdlib `sqlite3`.
- **One crawl at a time on the shared browser.** Two crawls attaching to the same Chrome contend for
  tabs and break. Finish/stop one first. Never `pkill` the browser — shut it down over the command
  port so the cleared session is reused.
- **Point each crawl at its OWN db/schema.** The `crawl.db` tables use plain names (`page`, `asset`,
  `frontier`, …); a shared DB with another project's `page` table collides.

## Key lessons baked in (see docs/)
- **Playwright is the Turnstile tell.** Cloudflare detects Playwright's CDP instrumentation and serves
  a dead challenge; **nodriver** (raw CDP, no Playwright) gets the real interactive widget and passes.
- **Site-isolation flags** + real Chrome channel matter for reaching cross-origin challenge iframes.
- **Torn-frame guard** for image/cam grabs (validate JPEG ends FFD9 / PNG ends IEND; retry on
  mid-write).
- **Clean shutdown over the command port** — never pkill the browser.
- **Python 3.14t free-threaded** — sync Playwright segfaults; use async / nodriver. Vendored nodriver
  is patched for it.

## Licence
MIT — see [LICENSE](LICENSE).

Use it lawfully: respect the terms of the sites you fetch, and don't point it at anything you have no
right to access. It exists so that legitimate access — research, archiving, your own accounts and
your own sites — isn't blocked by a bot check aimed at someone else.
