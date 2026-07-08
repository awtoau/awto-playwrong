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

Postgres instead of SQLite — same command, a URL for `--db`:

```bash
python -m crawl.run --seed https://competitor.com/ --db postgresql://user@host/mydb --schema crawl
```

Rule of thumb: **SQLite for a one-off rip** (a file, nothing to set up), **Postgres for the shared
pipeline**. Same code, same schema, same reports either way.

## Flags

| flag | meaning | default |
|------|---------|---------|
| `--seed URL` | seed URL (repeat for several) | required |
| `--db PATH_or_URL` | SQLite path or `postgres://…` | required |
| `--schema NAME` | Postgres schema (ignored by SQLite) | `crawl` |
| `--store DIR` | on-disk page store (compressed HTML) | `<db>.pages` |
| `--max N` | max pages this run | 200 |
| `--tabs N` | parallel browser tabs | 8 |
| `--depth N` | max link depth from a seed | 3 |
| `--host H` | extra host to allow beyond the seeds' | — |
| `--no-js` | block Script too (leanest; static sites) | keep JS |

Same-host only by default (it won't wander off into linked third parties). Add `--host` to widen.

## What you get

On disk: every distinct page's HTML, content-addressed + zstd-compressed, sharded under `--store`.

In the DB (see `../schema.sql`):
- **page** — one row per distinct page: url, title, text length, on-disk path, last-scan status.
- **page_link** — the page→page reference graph: *what links to what*.
- **page_asset** — every `<img>` a page references: url, alt, kind (piste_map / lift_map / map /
  panorama / logo / photo), and — once the bytes are fetched — the asset sha. **This answers
  "where is an image used on the site."**
- **asset** — one row per distinct image/binary actually fetched (via the `assets` package).
- **frontier** — the work list + per-URL last-scan status/error code (what to retry/skip/trust).

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

## Fetching the image bytes (optional second pass)

`crawl.run` records image *references*; it does not download the bytes (a text crawl stays cheap).
To mirror the images, iterate `page_asset` and use the `assets` package:

```python
from assets import store, classify, imgmeta      # sibling package
# for each page_asset.img_url: fetch bytes (via the browser or urllib), then:
meta = imgmeta.probe(data)
sa = store.store_asset(data, asset_root, mime=content_type, src_url=img_url, meta=meta)
d.upsert_asset(sa)
d.link_page_asset(page_sha, img_url, asset_sha=sa.sha256)   # closes the page→asset link
```

## Notes / gotchas

- **One crawl at a time on the shared browser.** Two crawls attaching to the same playwrong Chrome
  contend for tabs and break. Finish or stop one before starting another.
- **Never `pkill` the browser** — shut it down via its command port so the cleared session is reused.
- **Postgres needs the pure-Python `psycopg`** (the C/binary impl is broken under no-GIL). `crawl.db`
  asserts this and fails loud otherwise.
- The netblock intercepts only the blocked resource types (not every request), so page load never
  stalls — if you extend the block list, keep Document/Script/XHR/Fetch unblocked.
