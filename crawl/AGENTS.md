# Crawling with awto-playwrong — agent guide

Point yourself at this engine, give it seed URLs and a database target, and it crawls a site
end-to-end and prints a site-shape report. **Zero project references** — use it for any site
(competitor analysis, an audit, a one-off rip). The engine drives the shared headed Chrome
("playwrong") that already solves Cloudflare/Turnstile, so bot-walled sites work too.

## One command

```bash
# rip a site into a single SQLite file + print the report
python -m crawl.run --seed https://competitor.com/ --db competitor.sqlite --max 200 --tabs 8

# re-print the report later (no re-crawl)
python -m crawl.report --db competitor.sqlite --top 25
```

Postgres or MySQL instead of SQLite — same command, a SQLAlchemy URL for `--db`:

```bash
python -m crawl.run --seed https://competitor.com/ --db "postgresql+psycopg://user@host/mydb"
python -m crawl.run --seed https://competitor.com/ --db "mysql+pymysql://user@host/mydb"
```

Rule of thumb: **SQLite for a one-off rip** (a file, nothing to set up), **Postgres/MySQL for the
shared pipeline**. The relational store is SQLAlchemy 2.0 Core, so it's one model and the same reports
on any backend. (Point each crawl at its OWN database/schema — the tables use plain names like `page`,
so a shared DB with another project's `page` table will collide.)

## Flags

| flag | meaning | default |
|------|---------|---------|
| `--seed URL` | seed URL (repeat for several) | required |
| `--db PATH_or_URL` | SQLite path, or a SQLAlchemy URL (`postgresql+psycopg://…`, `mysql+pymysql://…`) | required |
| `--store DIR` | on-disk page store (compressed HTML) | `<db>.pages` |
| `--max N` | max pages **attempted** this run | 200 |
| `--tabs N` | parallel browser tabs | 8 |
| `--depth N` | max link depth from a seed | 3 |
| `--host H` | extra host to allow beyond the seeds' | — |
| `--no-js` | block Script too (leanest; static sites) | keep JS |
| `--rate-delay S` | min seconds between fetches to the SAME host (per-host politeness + 429/503 backoff, shared across all tabs). `0` disables. | 1.5 |
| `--no-shuffle` | crawl a depth band in url order (clusters one host) instead of randomised (spreads hosts) | shuffle on |

Same-host only by default (boundary match — it won't wander onto `evilexample.com` for `example.com`).
Add `--host` to widen.

## Politeness — rate limiting, backoff, and shuffle (don't trip a site's 429)

With many tabs and several seeds the engine crawls one host per tab (host-affinity). Two things keep it
from hammering any single origin into an HTTP 429 ("Too many requests"):

- **Per-host gate (`--rate-delay`, default 1.5s):** a shared minimum gap between two fetches to the
  *same* host — enforced across all tabs, so tabs on other hosts are unaffected and throughput stays
  high. `--rate-delay 0` turns it off (only for a site you own / are load-testing).
- **429/503 backoff (automatic):** the worker reads the navigation's HTTP status
  (`Network.responseReceived`). On a 429/503 it widens that host's gate exponentially (honouring
  `Retry-After` if sent), **requeues** the URL, and does NOT store the error body as content. A clean
  response relaxes the gate a step. The run summary prints any host that got backed off.
- **Shuffle (default on):** within a depth band, URLs are claimed in random order instead of sorted by
  url. Url-sort clusters a host's pages adjacently, so a batch of N slots would pull N pages of the
  *same* host in a row and hammer it (this is what tripped wbtools). Shuffle spreads a batch across
  hosts. Breadth-first by depth still leads (shallow/important pages first); only the intra-depth order
  is randomised. `--no-shuffle` restores the deterministic url order.

Rule of thumb: leave the defaults. Lower `--rate-delay` only for a site you own; raise it (e.g. `3`)
for a fragile origin. If a run summary shows a host with high `strikes`, that origin is touchy —
re-run it alone with a higher `--rate-delay`.

## What you get

On disk: every distinct page's HTML, content-addressed + zstd-compressed, sharded under `--store`.

In the DB (the model lives in `db.py`; `../schema.sql` documents it):
- **page** — one row per distinct page: url, title, text length, on-disk path, last-scan status.
- **page_link** — the page→page reference graph: *what links to what*.
- **page_asset** — every `<img>` a page references: url, alt, kind (generic: map / panorama / logo /
  photo / junk; a consumer can add vertical kinds), and — once the bytes are fetched — the asset sha.
  **This answers "where is an image used on the site."**
- **asset** — one row per distinct image/binary actually fetched (via the `assets` package).
- **frontier** — the work list + per-URL last-scan status/error code (what to retry/skip/trust).
- **unhandled** — where the engine fell through to a default (unknown consent, unclassified image,
  untranslatable page). `graph.improvement_report(d)` ranks these so the crawl tells you what generic
  handling to improve next — instead of silently baking one site/country/vertical's rules in.

## Reports (fast, no re-crawl)

`crawl.graph` runs against either backend:

```python
from crawl import db, graph
d = db.open_db("competitor.sqlite")
print(graph.summary_text(d))            # counts + hubs + authorities + orphans + dead links
graph.hubs(d, 20)                       # most linked-TO pages — the site's spine
graph.authorities(d, 20)                # pages that link OUT the most — menus/indexes
graph.orphans(d)                        # captured pages nothing links to
graph.dead_links(d, 50)                 # hrefs linked but never captured — gaps / off-site / 404
graph.inbound(d, url)                   # who links to this page
graph.outbound(d, url)                  # what this page links to
graph.image_usage(d, img_url)           # every page that references a given image
graph.image_pages(d, kind="piste_map")  # pages that reference an image of a given kind
```

## Fetching the image bytes

`crawl.run` records image *references* (fast text crawl). Getting the actual bytes, three ways
(cheapest first):

**1. Harvest Chrome's cache — ZERO re-fetch (`assets.cache`).** While the browser renders pages (images
aren't fully network-blocked — the block is best-effort and leaks), Chrome keeps every rendered image
in its Simple Cache. `assets.cache` reads those bytes straight off disk — no network at all.

```bash
# recover every cached image for your hosts into an asset store (dry-run first to count)
python -m assets.cache --root /path/assets --host mysite.com --dry-run
python -m assets.cache --root /path/assets --host mysite.com
```
```python
from assets import cache, store, imgmeta
def sink(url, data, mime):
    m = imgmeta.probe(data)
    sa = store.store_asset(data, ASSET_ROOT, mime=mime, src_url=url, meta=m)
    d.upsert_asset(sa); d.link_page_asset(page_sha_for(url), url, asset_sha=sa.sha256)
cache.harvest(sink, url_filter=lambda u: "mysite.com" in u)   # walks /tmp/uc_*/…/Cache_Data
```
Best right after a crawl (the cache is warm). The shared browser's profile is `/tmp/uc_*` — one 1GB+
cache can hold thousands of images across all recent browsing.

**2. Second-pass re-download (`assets.store`).** Iterate `page_asset` and fetch each `img_url` (browser
or urllib), then store — for images that weren't cached, or a clean deduped mirror:

```python
from assets import store, classify, imgmeta      # sibling package
meta = imgmeta.probe(data)
sa = store.store_asset(data, asset_root, mime=content_type, src_url=img_url, meta=meta)
d.upsert_asset(sa)
d.link_page_asset(page_sha, img_url, asset_sha=sa.sha256)   # closes the page→asset link
```

**3. Capture during the crawl (`Network.getResponseBody`)** — hook image responses as they load. Most
code; use only if you need bytes inline in one pass. (`docs/lessons.md` notes in-memory capture is less
reliable than 1 or 2 — prefer the cache harvest.)

## Notes / gotchas

- **One crawl at a time on the shared browser.** Two crawls attaching to the same playwrong Chrome
  contend for tabs and break. Finish or stop one before starting another.
- **Never `pkill` the browser** — shut it down via its command port so the cleared session is reused.
- **Postgres needs the pure-Python `psycopg`** (the C/binary impl is broken under no-GIL). `crawl.db`
  asserts this and fails loud otherwise.
- The netblock intercepts only the blocked resource types (not every request), so page load never
  stalls — if you extend the block list, keep Document/Script/XHR/Fetch unblocked.
