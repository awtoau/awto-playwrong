# awto-playwrong

**One place to go when an app or agent needs browser automation.** A generic, shareable browser-
capture engine — port-based, so multiple agents/apps can hit the same running infrastructure. It
documents **several methods** (plain Playwright style + the "powders" nodriver style that beats
Cloudflare Turnstile), so you pick the right one for the job.

Extracted from the powderhounds project, where it was built + battle-tested (it beats Cloudflare
Turnstile on a real site and recovered thousands of bot-blocked pages).

## Install (fresh machine)

```sh
git clone https://github.com/awtoau/awto-playwrong && cd awto-playwrong
python3 -m venv .venv                                            # optional but recommended
.venv/bin/python scripts/install.py --deps --link --register      # deps, `playwrong` on PATH, MCP
.venv/bin/python scripts/mcp_selftest.py                         # prove it works (28 assertions)
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
fetch("https://example.com")   # auto-starts the engine + Chrome, clears Cloudflare,
                               # returns readable text, closes its own tab
```

Also: `screenshot`, `goto`, `read`, `js`, `click`, `key`, `solve`, `cookies`, `tabs`, `close_tab`.
No script to write, no API doc to read, no tab bookkeeping. `mcp/server.py` is a stdlib-only proxy in
front of the same engine described below — driving it over HTTP still works exactly as before.

## I just want to view / fetch one web page

```sh
./playwrong https://awto.au
```

That's it. There is **nothing to start first** — no server to launch, no `PYTHONPATH`, no port to
pick. The first command starts the engine and a real headed Chrome; the page comes back as readable
text.

```sh
./playwrong https://a.com https://b.com   # several urls, reusing the same warm browser
./playwrong https://awto.au --links       # keep hrefs, so you can pick the next url
./playwrong https://awto.au --html        # raw markup instead of text
./playwrong https://awto.au --shot p.png  # also save a screenshot
./playwrong https://awto.au --json        # {text,title,url,challenge} for scripting
./playwrong --status                      # what's running
./playwrong --stop                        # stop the shared engine
```

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
python engine/client.py goto https://example.com
python engine/client.py js "document.title"
python engine/client.py shot page.png
```

Or POST directly to the port from anything: `curl -s localhost:8731/goto -d '{"url":"..."}'`. The
browser stays alive between calls. The rest of this README is for crawling many pages.

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
- `engine/connect.py` — **the one place that reaches the engine and starts it if it's down**, plus
  html→text. Every entry point (CLI, MCP server, your scripts) goes through it, so "how do I start
  this thing" has exactly one answer and callers stop reimplementing it.
- `engine/cli.py` + `./playwrong` — the one-shot command: url in, readable page out.
- `engine/client.py` — the interactive port client (`goto/click/key/js/shot/tabs/...`).
- `engine/solve.py` — standalone Turnstile solve (find "verify you are human" inside the cross-origin
  iframe + click).
- `vendor/nodriver` — patched nodriver 0.50.3 (fixes a non-UTF-8 byte in `cdp/network.py` line ~1345
  that raises `SyntaxError` on import under CPython 3.14). Upstream: issue
  [ultrafunkamsterdam/nodriver#35](https://github.com/ultrafunkamsterdam/nodriver/issues/35) + fix PR
  [#36](https://github.com/ultrafunkamsterdam/nodriver/pull/36) (both open/unmerged). **Drop the vendor
  pin once #36 merges and a fixed release ships.**

### methods/ — alternative / historical
- `playwright-server.py` + `playwright-ctl.py` — the earlier Playwright-based server/client. **Note:
  Playwright is detectable by Cloudflare Turnstile** (it gets served a dead, never-rendering
  challenge) — kept for non-protected sites + reference. Use the nodriver engine for anything behind
  Cloudflare.
- `playwright-crawl.py` — a one-shot Playwright crawler (headed).

## Usage (engine)
```
# there is no step 1 — every entry point starts the engine and Chrome if they're down
./playwrong https://example.com                  # one page as text
python engine/client.py goto https://example.com
python engine/client.py solvecf                  # clear a Turnstile challenge
python engine/client.py text                     # get the page HTML
python engine/client.py shot frame.png           # screenshot
```
From your own Python, use the same code path the CLI and the MCP server use:
```python
from engine import connect
page = connect.capture("https://example.com")    # starts engine+Chrome, solves, closes its tab
print(page["text"])
```
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
python -m crawl.run --seed https://example.com/ --db site.sqlite --max 200 --tabs 8
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

crawl --seed https://example.com/ --db site.sqlite --max 200 --tabs 8
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

## Handoff state (for the next agent)

**DONE + verified:** the `crawl/` library above; the SQLAlchemy-Core DB layer (SQLite/Postgres/MySQL,
tested both); a 3-agent adversarial review with all findings fixed (subdomain-escape, a `<script>`
ReDoS, HTML-entity decoding, feed/infra over-matching, stuck-frontier reclaim); a live **472-page
parallel crawl, 0 failures, 85% rich pages**. `sniff.py` (powderhounds) consumes `crawl.challenge` +
`crawl.netblock`.

**OPEN:**
- **Finish the #165 fold-back** — re-point powderhounds' `nd_crawl.py` at this engine (it still has its
  own copy), and fold PH's DNS tracker-block (`tracker_resolver_rules`/`TRACKER_HOSTS`) into the engine
  (`sniff.py` still imports those from `nd_crawl`).
- Review follow-ups (medium): `--no-js` mode should skip the render-wait (blocking Script means no
  client render to wait for); surface `netblock.enable()` failures instead of swallowing them.

**GOTCHAS — read before running:**
- **nodriver import landmine.** Upstream `nodriver/cdp/network.py` has a non-UTF-8 byte (line ~1345)
  that raises `SyntaxError` under **Python 3.14** (both the GIL and free-threaded builds — 3.14 tightened
  the source tokenizer to reject non-UTF-8 bytes with no encoding declaration; 3.13 and earlier were
  lenient). `vendor/nodriver` here is patched, but a consumer's
  `site-packages` copy may NOT be — **put `vendor` FIRST in `PYTHONPATH`** (`PYTHONPATH=vendor:…`) so
  the patched copy wins. This is the #1 thing that breaks a fresh run.
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

## Status
Public on GitHub at https://github.com/awtoau/awto-playwrong. `main` carries the `engine/` capture
server + the `crawl/` library + `assets/`. Local working checkout remains on your machine.

_Browser automation + a reusable crawl library: the nodriver engine (Turnstile-beating), a port-driven
capture server, and `crawl/` (walk a site → content-addressed store + SQLAlchemy DB + reference-graph
reports). Ref-free — the shared home for any app/agent that needs a browser or a crawler._
