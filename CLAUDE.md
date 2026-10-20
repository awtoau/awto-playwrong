# awto-playwrong — project rules

Loaded after the global agent rules; these override the global ones where they disagree.

## Issues and fixes: file and fix, don't ask

Exempt from the global public-repo two-approval gate.

- **File the issue when you find it** — no approval, no draft-then-ask round trip: `gh issue create` against `awtoau/awto-playwrong` directly.
- **Fix it in the same session** where the fix is in scope. Don't stop at a report.
- **Still scrub the content** (public repo): no `/home/...` or `/mnt/...` paths, no secrets, no RE mention. Repo-relative paths (`engine/server.py:202`, `tmp/nd-server.log`) are fine and preferred.
- Show what was filed/changed afterward — the exemption is about not asking first, not working silently.

## This is the web-access path for every other agent

When it breaks, the failure shows up elsewhere as "the web tool doesn't work", and the agent reaches for curl instead of reporting it. So:

- **A bug here is worth a regression test**, not just a fix — `scripts/recovery_test.py` exists because a wedged engine looked healthy for three days before anyone traced it.
- **A failure must name itself** — a bare `ConnectionClosedError` sent every caller to the wrong place. Errors say what died + what to do.
- **Never make a diagnostic optimistic** — `/status` reporting `alive` from a stale handle made `doctor.py` certify a dead engine as healthy (#8).

## Reviews: the CDP client against the installed Chrome

`engine/cdp.py` is ours (#41); a Chrome update can break it. Every review:

- `python scripts/cdp_smoke_test.py`, `turnstile_click_test.py`, `stealth_check.py`, `cdp_surface_check.py`. A failure after a Chrome update → file an issue naming the Chrome version (`google-chrome --version`) + the failing check.
- Any change to Chrome's launch arguments: `stealth_check.py` + `cdp_surface_check.py` before merging. A port of 0 silently set `navigator.webdriver = true` (#47); `--remote-allow-origins=*` let any page drive the browser (#52).

## Testing

- Live tests go on an **isolated port** (`--port 8739`), never the shared engine on 8731 (it holds everyone's cleared Turnstile session).
- Never `pkill` Chrome. `scripts/recovery_test.py` kills a browser on purpose and gets the pid from that engine's own `/status`, so it only ever hits its own.
- Before committing: `python scripts/mcp_selftest.py --port 8739 --shutdown`, `python scripts/check_docs.py`, `uvx ruff check .`.
- Docs: README is the index (features, install, env, files, tests); MCP.md, AGENT-API.md, crawl/AGENTS.md go deep. State a fact once + link.
- `check_docs.py` runs both ways — a new script in `scripts/` or a new CLI flag **fails the check until it is documented**. Deliberate.
