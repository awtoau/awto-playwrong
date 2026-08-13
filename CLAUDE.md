# awto-playwrong — project rules

Loaded after the global agent rules (`~/.claude/CLAUDE.md`). These override the global ones where
they disagree.

## Issues and fixes: file and fix, don't ask

This repo is exempt from the global "public repo → two approvals" gate.

- **File the issue when you find it.** No approval, no draft-then-ask round trip. `gh issue create`
  against `awtoau/awto-playwrong` directly.
- **Fix it in the same session** where the fix is in scope. Don't stop at a report.
- **Still scrub the content** — it is a public repo: no `/home/...` or `/mnt/...` paths, no secrets,
  no mention of the RE work. Repo-relative paths (`engine/server.py:202`, `tmp/nd-server.log`) are
  fine and preferred.
- Show what was filed or changed afterwards. The exemption is about not asking first, not about
  working silently.

## This project is the web-access path for every other agent

When it breaks, the failure shows up somewhere else as "the web tool doesn't work", and the agent
that hits it usually reaches for curl instead of reporting it. So:

- **A bug here is worth a regression test**, not just a fix — `scripts/recovery_test.py` exists
  because a wedged engine looked healthy for three separate days before anyone traced it.
- **A failure must name itself.** A bare `ConnectionClosedError` sent every caller looking in the
  wrong place. Errors say what died and what to do.
- **Never make a diagnostic optimistic.** `/status` reporting `alive` from a stale handle turned
  `doctor.py` into a tool that certified a dead engine as healthy (issue #8).

## Testing

- Live tests go on an **isolated port** (`--port 8739`), never the shared engine on 8731 — it holds
  everyone's cleared Turnstile session.
- Never `pkill` Chrome. `scripts/recovery_test.py` kills a browser on purpose, and gets the pid from
  that engine's own `/status` so it can only ever hit its own.
- Before committing: `python scripts/mcp_selftest.py --port 8739 --shutdown`,
  `python scripts/check_docs.py`, `uvx ruff check .`.
- `check_docs.py` runs both ways — a new script in `scripts/` or a new CLI flag **fails the check
  until it is documented**. That is deliberate.
