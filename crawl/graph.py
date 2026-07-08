"""crawl.graph — generic reports over the page->page reference graph (works on SQLite or Postgres).

"What references what" is captured in the page_link table (source page's sha -> href it links to).
That single edge list answers most site-structure questions fast — no re-crawl, no page bytes needed.
These helpers run identical SQL on either backend (via a CrawlDB from crawl.db) and return plain rows,
so a consumer (or an agent) can turn them straight into a competitor-analysis / site-shape report.

    from crawl import db, graph
    d = db.open_db("competitor.sqlite")
    print(graph.summary(d))          # counts + top hubs + orphans + dead links
    for url, n in graph.hubs(d, 20): ...        # most-referenced pages (the site's spine)
    for url in graph.orphans(d): ...            # captured pages nothing links to
    for href, n in graph.dead_links(d, 20): ... # linked-to URLs never captured (gaps / off-site)

Definitions:
  • inbound(url)  — pages that link TO url
  • outbound(url) — hrefs url links to
  • hubs          — pages with the most INBOUND links (the site's important pages)
  • authorities   — pages that link OUT the most (index/hub/menu pages)
  • orphans       — captured pages with ZERO inbound links (unreachable except by seed/menu)
  • dead_links    — hrefs that were linked but never became a captured page (gaps, off-host, 404s)
"""


def _p(dbobj):
    return dbobj._p            # table prefix ('' for sqlite, 'crawl.' for pg)


def inbound(dbobj, url):
    """Pages (sha, url) that link to `url`."""
    p = _p(dbobj)
    return dbobj._q(f"SELECT l.page_sha, pg.url FROM {p}page_link l "
                    f"JOIN {p}page pg ON pg.sha256=l.page_sha WHERE l.href=?", (url,))


def outbound(dbobj, page_url):
    """hrefs that the page at `page_url` links to."""
    p = _p(dbobj)
    return [r[0] for r in dbobj._q(
        f"SELECT l.href FROM {p}page_link l JOIN {p}page pg ON pg.sha256=l.page_sha "
        f"WHERE pg.url=? ORDER BY l.href", (page_url,))]


def hubs(dbobj, n=20):
    """(url, inbound_count) for the most-referenced captured pages — the site's spine."""
    p = _p(dbobj)
    return dbobj._q(
        f"SELECT pg.url, count(*) AS n FROM {p}page_link l "
        f"JOIN {p}page pg ON pg.url=l.href "          # href resolves to a captured page
        f"GROUP BY pg.url ORDER BY n DESC, pg.url LIMIT ?", (n,))


def authorities(dbobj, n=20):
    """(url, outbound_count) for pages linking OUT the most — menus / index / hub pages."""
    p = _p(dbobj)
    return dbobj._q(
        f"SELECT pg.url, count(*) AS n FROM {p}page_link l "
        f"JOIN {p}page pg ON pg.sha256=l.page_sha "
        f"GROUP BY pg.url ORDER BY n DESC, pg.url LIMIT ?", (n,))


def orphans(dbobj):
    """Captured page URLs with NO inbound links (reachable only via seed/menu — often stale/hidden)."""
    p = _p(dbobj)
    return [r[0] for r in dbobj._q(
        f"SELECT pg.url FROM {p}page pg WHERE NOT EXISTS "
        f"(SELECT 1 FROM {p}page_link l WHERE l.href=pg.url) ORDER BY pg.url")]


def dead_links(dbobj, n=50):
    """(href, times_linked) for hrefs that were referenced but never captured as a page — the gaps:
    off-host links, un-crawled pages, or 404s. High counts = prominent links worth checking."""
    p = _p(dbobj)
    return dbobj._q(
        f"SELECT l.href, count(*) AS n FROM {p}page_link l "
        f"LEFT JOIN {p}page pg ON pg.url=l.href WHERE pg.url IS NULL "
        f"GROUP BY l.href ORDER BY n DESC, l.href LIMIT ?", (n,))


def image_usage(dbobj, img_url):
    """Every page that references a given image URL — (page_url, alt, kind). "Where is this image
    used on the site?" A crawl records image REFS per page in page_asset, so this is a direct lookup."""
    p = _p(dbobj)
    return dbobj._q(
        f"SELECT pg.url, pa.alt, pa.kind FROM {p}page_asset pa "
        f"JOIN {p}page pg ON pg.sha256=pa.page_sha WHERE pa.img_url=? ORDER BY pg.url", (img_url,))


def image_pages(dbobj, kind=None, n=200):
    """(img_url, kind, n_pages) for images referenced across the site, most-used first. Filter by
    kind (piste_map | lift_map | map | panorama | logo | photo) to find e.g. every piste map."""
    p = _p(dbobj)
    where = "WHERE kind=?" if kind else ""
    args = (kind, n) if kind else (n,)
    return dbobj._q(
        f"SELECT img_url, kind, count(*) AS n FROM {p}page_asset {where} "
        f"GROUP BY img_url, kind ORDER BY n DESC, img_url LIMIT ?", args)


def summary(dbobj, top=10):
    """A compact, printable site-shape report as a dict — counts + top hubs/authorities + orphan and
    dead-link samples. An agent can render this straight into a competitor-analysis report."""
    c = dbobj.counts()
    orph = orphans(dbobj)
    return {
        "counts": c,
        "top_hubs": hubs(dbobj, top),
        "top_authorities": authorities(dbobj, top),
        "orphan_count": len(orph),
        "orphans_sample": orph[:top],
        "dead_links": dead_links(dbobj, top),
    }


def summary_text(dbobj, top=10):
    """summary() rendered as a plain-text block for a log or a quick report."""
    s = summary(dbobj, top)
    c = s["counts"]
    L = [f"pages={c['pages']}  links={c['links']}  assets={c['assets']}  "
         f"frontier: {c['frontier_done']} done / {c['frontier_queued']} queued",
         "",
         f"top {top} hubs (most linked-TO):"]
    L += [f"  {n:>5}  {u}" for u, n in s["top_hubs"]]
    L += ["", f"top {top} authorities (link OUT the most):"]
    L += [f"  {n:>5}  {u}" for u, n in s["top_authorities"]]
    L += ["", f"orphan pages (no inbound): {s['orphan_count']}"]
    L += [f"  {u}" for u in s["orphans_sample"]]
    L += ["", f"top dead links (linked, never captured):"]
    L += [f"  {n:>5}  {h}" for h, n in s["dead_links"]]
    return "\n".join(L)
