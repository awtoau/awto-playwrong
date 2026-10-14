# Crawling with awto-playwrong: agent guide

Give it seed urls and a database. It crawls the site through the shared headed Chrome (which clears
Cloudflare) and prints a site-shape report. It contains no project-specific code.

Install the crawl deps first: `pip install -e ".[crawl]"`. On a free-threaded Python, SQLAlchemy needs
a pure-Python build: see the [README](../README.md#crawl-the-crawl-library).

## One command

```bash
python -m crawl.run --seed https://awto.au/ --db site.sqlite --max 200 --tabs 8
python -m crawl.report --db site.sqlite --top 25          # re-print the report, no re-crawl
python -m crawl.run --seed https://awto.au/ --db "postgresql+psycopg://user@host/mydb"
python -m crawl.run --seed https://awto.au/ --db "mysql+pymysql://user@host/mydb"
```

- Use SQLite for a one-off crawl, Postgres/MySQL for a shared pipeline. All three share one
  SQLAlchemy 2.0 Core model (`crawl/db.py`, documented in `../schema.sql`).
- **Give each crawl its OWN database or schema.** The table names are plain (`page`, `asset`, …).
- **One crawl at a time on the shared browser.** Two crawls fight over tabs.
- The crawl runs in its own systemd memory scope (`PH_CRAWL_MEMORY_MAX`, 4G, #26).

## Flags

| Flag | Meaning | Default |
|---|---|---|
| `--seed URL` | seed url (repeatable) | required |
| `--db PATH_or_URL` | SQLite path or a SQLAlchemy URL | required |
| `--store DIR` | on-disk page store (zstd HTML) | `<db>.pages` |
| `--max N` | max pages **attempted** this run | 200 |
| `--max-per-host N` | stop taking a host's pages once N are attempted, counting earlier runs. Pages over the cap stay queued. | 0 (no limit) |
| `--tabs N` | parallel tabs, clamped to 32 (`CRAWL_MAX_TABS`) because the browser is shared | 8 |
| `--depth N` | max link depth from a seed | 3 |
| `--nav-timeout S` | per-page navigation budget | 12 |
| `--stall-ceiling S` | hard cap on one page's fetch (nav + render + capture) before abandoning page and tab. `0` = `max(45, nav*4)`. | 0 |
| `--port N` | engine port | `PH_PORT` / 8731 |
| `--host H` | extra host to allow (repeatable). Default: the seeds' hosts only, matched on label boundaries. | — |
| `--exclude REGEX` | never queue a matching link (repeatable): post anchors, redirects, member pages, calendars | — |
| `--rank` | important pages first: sitemap + url shape + OpenPageRank (#2) | off |
| `--prefer KW` / `--avoid KW` | with `--rank`: raise/lower urls containing KW (repeatable) | — |
| `--min-priority P` | with `--rank`: leave rows scored below P queued (scores 0-3) | — |
| `--no-js` | block Script too (static sites) | JS on |
| `--rate-delay S` | min gap between fetches to the same host; `0` disables | 1.5 |
| `--no-shuffle` | url order within a depth band instead of random | shuffle on |
| `--no-host-diverse` | disable round-robin-by-host claiming | on |

**OpenPageRank** needs a key in `~/.config/awto/secrets.yaml` (section `openpagerank`, field
`password`). Without one, ranking uses sitemap + url shape, and the log says so.

## Politeness

- **Per-host gap** (`--rate-delay`): enforced across all tabs, so other hosts aren't slowed.
- **429/503 backoff**: reads the document's HTTP status. On 429/503 it widens that host's gap
  exponentially (honouring `Retry-After`) and requeues the url without storing the error body. A
  clean response relaxes the gap a step. The run summary lists hosts that were backed off.
- **Shuffle**: random order within a depth band, so a batch doesn't hit one host N times in a row.
- **Host-diverse claiming**: a batch is built round-robin across hosts, one site per tab. A host with
  only deep pages left still contributes.
- Leave the defaults. Raise `--rate-delay` (e.g. 3) for a fragile origin; lower it only for your own
  site.

## What you get

On disk: every distinct page's HTML, content-addressed and zstd-compressed under `--store`.

In the DB:
- **page**: url, title, text length, store path, scan status, HTTP status (#42), `text_sha`.
- **page_link**: the page→page graph.
- **page_asset**: every `<img>` a page references: url, alt, caption, kind (`map` / `panorama` /
  `logo` / `photo` / `junk`, extendable via `extra_rules`), and the asset sha once fetched.
- **asset**: one row per distinct image/binary fetched (`assets` package).
- **frontier**: the work list, with priority, claim lease and last error.
- **unhandled**: where handling fell through to a default (unknown consent, unclassified image).
  `graph.improvement_report(d)` ranks them.

**`link_code`**: an opaque tag on a seed, carried onto `frontier`, `page` and `page_asset`. Use it to
join crawled data back to your own entities:

```python
d.enqueue("https://awto.au/", link_code="site-442")
rows = d.engine.connect().execute(text("SELECT url, title FROM page WHERE link_code = :lc"),
                                  {"lc": "site-442"})
```

**Schema upgrades are additive.** `init_schema()` creates missing tables and `ALTER`s in missing
columns. It never drops or changes existing columns.

## Reports

```python
from crawl import db, graph
d = db.open_db("site.sqlite")
print(graph.summary_text(d))            # counts, hubs, authorities, orphans, dead links
graph.hubs(d, 20)                       # most linked-to pages
graph.authorities(d, 20)                # pages linking out most (menus, indexes)
graph.orphans(d)                        # captured pages nothing links to
graph.dead_links(d, 50)                 # linked, never captured; a url sharing content is not dead (#48)
graph.inbound(d, url); graph.outbound(d, url)
graph.image_usage(d, img_url)           # every page referencing an image
graph.image_pages(d, kind="map")        # pages referencing an image of a kind
```

## Image bytes

`crawl.run` records image **references**. Three ways to get the bytes, cheapest first:

1. **Chrome's disk cache (`assets.cache`)**, no re-fetch. Best straight after a crawl.
   ```bash
   python -m assets.cache --root /path/assets --host awto.au --dry-run
   python -m assets.cache --root /path/assets --host awto.au
   ```
   The default glob is the engine's throwaway profiles (`<tempdir>/playwrong-profile-*`). For a named
   profile, pass `--profiles-glob`.
2. **Re-download** each `page_asset.img_url`, then `store.store_asset(...)` + `d.upsert_asset(sa)` +
   `d.link_page_asset(page_sha, img_url, asset_sha=sa.sha256)`.
3. **`Network.getResponseBody` during the crawl.** The least reliable: bodies get evicted
   ([lessons](../docs/lessons.md)).

## Gotchas

- Never `pkill` the browser. Stop the engine through its port.
- `crawl.netblock` intercepts only the blocked resource types. If you extend the list, keep
  Document/Script/XHR/Fetch unblocked, or the page load stalls.
- Drivers must be pure-Python on a free-threaded build: stdlib `sqlite3`, `psycopg` (not
  `[binary]`/`[c]`), `pymysql`.
