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
.venv/bin/python scripts/install.py --deps --register   # deps + register with Claude Code
.venv/bin/python scripts/mcp_selftest.py               # prove it (28 assertions, ~40s)
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
`--port N` (pins `PH_PORT` for this registration, e.g. a second isolated browser).

## The tools

| Tool | What it's for |
|---|---|
| **`fetch`** | **The 90% tool.** One page -> readable text. Own tab, auto-solve, auto-cleanup. |
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
