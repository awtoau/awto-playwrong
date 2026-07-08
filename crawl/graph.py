"""crawl.graph — reports over the page->page reference graph + image usage + improvement feedback.

Runs on any backend a CrawlDB opened (SQLite/Postgres/MySQL) via SQLAlchemy Core — no raw SQL, no
per-dialect strings. "What references what" is captured in page_link (source page's sha -> href); most
site-structure questions answer fast from that edge list with no re-crawl and no page bytes.

    from crawl import db, graph
    d = db.open_db("competitor.sqlite")
    print(graph.summary_text(d))          # counts + hubs + authorities + orphans + dead links
    graph.image_usage(d, img_url)          # every page that references a given image
    print(graph.improvement_report(d))     # where the engine fell through -> what to improve next

Definitions: inbound/outbound = who links to / what this links to; hubs = most INBOUND (the spine);
authorities = most OUTBOUND (menus/indexes); orphans = captured pages with zero inbound; dead_links =
hrefs linked but never captured (gaps/off-site/404).
"""
from sqlalchemy import func, select

from .db import asset, frontier, page, page_asset, page_link, unhandled  # noqa: F401


def _rows(dbobj, stmt):
    with dbobj.engine.connect() as c:
        return [tuple(r) for r in c.execute(stmt).all()]


def inbound(dbobj, url):
    """Pages (sha, url) that link to `url`."""
    stmt = (select(page_link.c.page_sha, page.c.url)
            .select_from(page_link.join(page, page.c.sha256 == page_link.c.page_sha))
            .where(page_link.c.href == url))
    return _rows(dbobj, stmt)


def outbound(dbobj, page_url):
    """hrefs that the page at `page_url` links to."""
    stmt = (select(page_link.c.href)
            .select_from(page_link.join(page, page.c.sha256 == page_link.c.page_sha))
            .where(page.c.url == page_url).order_by(page_link.c.href))
    return [r[0] for r in _rows(dbobj, stmt)]


def hubs(dbobj, n=20):
    """(url, inbound_count) for the most-referenced captured pages — the site's spine."""
    stmt = (select(page.c.url, func.count().label("n"))
            .select_from(page_link.join(page, page.c.url == page_link.c.href))
            .group_by(page.c.url).order_by(func.count().desc(), page.c.url).limit(n))
    return _rows(dbobj, stmt)


def authorities(dbobj, n=20):
    """(url, outbound_count) for pages linking OUT the most — menus / index / hub pages."""
    stmt = (select(page.c.url, func.count().label("n"))
            .select_from(page_link.join(page, page.c.sha256 == page_link.c.page_sha))
            .group_by(page.c.url).order_by(func.count().desc(), page.c.url).limit(n))
    return _rows(dbobj, stmt)


def orphans(dbobj):
    """Captured page URLs with NO inbound links (reachable only via seed/menu — often stale/hidden)."""
    sub = select(page_link.c.href).where(page_link.c.href == page.c.url).exists()
    stmt = select(page.c.url).where(~sub).order_by(page.c.url)
    return [r[0] for r in _rows(dbobj, stmt)]


def dead_links(dbobj, n=50):
    """(href, times_linked) for hrefs linked but never captured — gaps / off-site / 404. High = prominent."""
    j = page_link.outerjoin(page, page.c.url == page_link.c.href)
    stmt = (select(page_link.c.href, func.count().label("n")).select_from(j)
            .where(page.c.url.is_(None))
            .group_by(page_link.c.href).order_by(func.count().desc(), page_link.c.href).limit(n))
    return _rows(dbobj, stmt)


def image_usage(dbobj, img_url):
    """Every page that references a given image URL — (page_url, alt, kind). "Where is this image used?" """
    stmt = (select(page.c.url, page_asset.c.alt, page_asset.c.kind)
            .select_from(page_asset.join(page, page.c.sha256 == page_asset.c.page_sha))
            .where(page_asset.c.img_url == img_url).order_by(page.c.url))
    return _rows(dbobj, stmt)


def image_pages(dbobj, kind=None, n=200):
    """(img_url, kind, n_pages) for images referenced across the site, most-used first. Filter by kind."""
    stmt = select(page_asset.c.img_url, page_asset.c.kind, func.count().label("n"))
    if kind:
        stmt = stmt.where(page_asset.c.kind == kind)
    stmt = stmt.group_by(page_asset.c.img_url, page_asset.c.kind).order_by(
        func.count().desc(), page_asset.c.img_url).limit(n)
    return _rows(dbobj, stmt)


def improvement_report(dbobj, top=20):
    """The feedback loop: rank the categories where the engine fell through to a default (unknown
    consent platform, unclassified image, untranslatable language, blocked capture), with a sample.
    This is how the CODE tells you what generic handling to improve next — per-country/vertical cases
    surface here instead of being silently baked in. Returns [(category, n, sample_url, sample), …]."""
    stmt = (select(unhandled.c.category, func.count().label("n"),
                   func.min(unhandled.c.url), func.min(unhandled.c.sample))
            .group_by(unhandled.c.category).order_by(func.count().desc()).limit(top))
    return _rows(dbobj, stmt)


def summary(dbobj, top=10):
    """A compact, printable site-shape report as a dict."""
    orph = orphans(dbobj)
    return {
        "counts": dbobj.counts(),
        "top_hubs": hubs(dbobj, top),
        "top_authorities": authorities(dbobj, top),
        "orphan_count": len(orph),
        "orphans_sample": orph[:top],
        "dead_links": dead_links(dbobj, top),
        "improvements": improvement_report(dbobj, top),
    }


def summary_text(dbobj, top=10):
    """summary() as a plain-text block for a log or quick report."""
    s = summary(dbobj, top)
    c = s["counts"]
    L = [f"pages={c['pages']}  links={c['links']}  assets={c['assets']}  "
         f"frontier: {c['frontier_done']} done / {c['frontier_queued']} queued  "
         f"unhandled={c['unhandled']}",
         "", f"top {top} hubs (most linked-TO):"]
    L += [f"  {n:>5}  {u}" for u, n in s["top_hubs"]]
    L += ["", f"top {top} authorities (link OUT the most):"]
    L += [f"  {n:>5}  {u}" for u, n in s["top_authorities"]]
    L += ["", f"orphan pages (no inbound): {s['orphan_count']}"]
    L += [f"  {u}" for u in s["orphans_sample"]]
    L += ["", "top dead links (linked, never captured):"]
    L += [f"  {n:>5}  {h}" for h, n in s["dead_links"]]
    if s["improvements"]:
        L += ["", "improvement feedback (engine fell through — fix these to raise coverage):"]
        L += [f"  {n:>5}  {cat:22} e.g. {samp or url or ''}"[:100]
              for cat, n, url, samp in s["improvements"]]
    return "\n".join(L)
