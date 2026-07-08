"""crawl.run — self-contained, point-and-go site crawler built from the engine parts.

This is the REFERENCE CONSUMER: give it seed URLs and a database target and it does the whole job —
attach to the shared browser, block heavy resources, render each page, extract text + links + image
refs, content-address the HTML on disk, record everything (frontier + page + reference graph + image
refs) into SQLite or Postgres, and print a site-shape report at the end. Zero project references, so
an agent can point it at ANY site (e.g. competitor analysis) with one command and share the report.

    # rip a small site into a single SQLite file, print the report
    python -m crawl.run --seed https://competitor.com/ --db competitor.sqlite --max 200 --tabs 8

    # or into Postgres (schema 'crawl' by default)
    python -m crawl.run --seed https://competitor.com/ --db postgresql://u@host/mydb --max 500

    # multiple seeds, custom store dir, same-host only (default), depth limit
    python -m crawl.run --seed https://a.com/ --seed https://b.com/ --store ./pages --depth 3

What it stores (see schema.sql): every distinct page (sha, title, text length, on-disk path), the
page->page reference graph ("what links to what"), and every <img> reference with its classification.
Fetching the image BYTES is left to the consumer (assets.store) — this runner records the refs so an
image pass can pick them up. Run `python -m crawl.report --db …` any time to re-print the report.
"""
import argparse
import asyncio
import os
import sys
import time
from urllib.parse import urlsplit

from . import browser, netblock, render, parse, store, db, graph, ratelimit


def _same_site(url, hosts):
    """True only if url's host EQUALS one of `hosts` or is a dot-boundary subdomain of it. A raw
    suffix match (endswith) would let `evilsnowtravelbooker.com` pass for `snowtravelbooker.com` —
    a scope-escape / SSRF-adjacent bug — so we match on a host boundary and strip the port."""
    try:
        netloc = urlsplit(url).netloc.lower().split("@")[-1].split(":")[0]
        return any(netloc == h or netloc.endswith("." + h) for h in hosts)
    except Exception:
        return False


def _host_of(url):
    try:
        return urlsplit(url).netloc.lower().split("@")[-1].split(":")[0]
    except Exception:
        return ""


async def _fetch_one(tab, url, depth, cfg, d, stats):
    """Nav → render → capture → parse → record. Guarantees a terminal frontier state for `url` even on
    crash (finally-scan) so no row is left stuck in 'fetching'. Honours the shared per-host rate limit
    (cfg.rl): waits for a polite slot before nav, and on a 429/503 backs the host off + requeues."""
    t0 = time.monotonic()
    terminal = False   # did we record a terminal scan for this url?
    host = _host_of(url)
    rl = getattr(cfg, "rl", None)
    # capture the main document's HTTP status via a one-shot CDP response hook (so we can SEE a 429).
    doc_status = {"code": None, "retry_after": None}
    def _on_resp(ev):
        try:
            r = ev.response
            # first same-URL (or same-host document) response wins — that's the navigation response.
            if doc_status["code"] is None and getattr(r, "status", None):
                doc_status["code"] = int(r.status)
                hdrs = {k.lower(): v for k, v in (getattr(r, "headers", {}) or {}).items()}
                ra = hdrs.get("retry-after")
                if ra:
                    try: doc_status["retry_after"] = float(ra)
                    except ValueError: doc_status["retry_after"] = None
        except Exception:
            pass
    try:
        if rl is not None:
            await rl.acquire(host)          # block until it's polite to hit this host
        try:
            from nodriver import cdp as _cdp
            tab.add_handler(_cdp.network.ResponseReceived, _on_resp)
        except Exception:
            pass
        nav_timed_out = False
        try:
            await asyncio.wait_for(tab.get(url), timeout=cfg.nav_timeout)
        except asyncio.TimeoutError:
            nav_timed_out = True
        # rate-limited? back the host off and requeue this URL — don't store the 429 body as content.
        if rl is not None and rl.on_response(host, doc_status["code"], doc_status["retry_after"]):
            d.scan(url, status="rate_limited", http_status=doc_status["code"],
                   note=f"429/503 backoff -> {rl._h(host).delay:.1f}s"); terminal = True
            d.enqueue(url, depth)           # requeue for a later, slower attempt
            stats["fail"] += 1
            print(f"  429 backoff {rl._h(host).delay:.0f}s (requeued) {url[:60]}", flush=True)
            return
        # nav can return while the doc is still 'loading' -> a stub with an empty body. Wait for the
        # DOM to be ready, dismiss any consent wall, then let client-rendered content mount.
        await render.wait_ready(tab, max_wait=cfg.nav_timeout)
        await render.dismiss_overlays(tab)
        await render.wait_for_render(tab, min_chars=200, max_wait=max(8.0, cfg.nav_timeout))
        if nav_timed_out and await render.body_text_len(tab) < 20:
            d.scan(url, status="nav_timeout", note=f">{cfg.nav_timeout}s"); terminal = True
            stats["fail"] += 1
            print(f"  TIMEOUT(empty) {url}", flush=True)
            return
        html = await tab.get_content()
        sp = store.store_page(html, cfg.store_root)          # store_page already extracted title+text
        links = parse.extract_links(html, url)
        images = list(parse.extract_images(html, url))
        # record: page row + reference graph + image refs, then enqueue same-site links.
        d.upsert_page(sp, url, title=sp.title, text_chars=sp.text_chars, status="ok")
        d.add_links(sp.sha256, links)
        for iu, alt, kind in images:
            d.link_page_asset(sp.sha256, iu[:2000], (alt or "")[:500], kind)
        for lu in links:
            if _same_site(lu, cfg.hosts) and depth + 1 <= cfg.depth:
                d.enqueue(lu, depth + 1, discovered_from=url)
        d.scan(url, status="ok", http_status=doc_status["code"], sha256=sp.sha256); terminal = True
        ms = int((time.monotonic() - t0) * 1000)
        stats["ok"] += 1
        stats["chars"] += sp.text_chars
        print(f"  OK {sp.text_chars:>6}ch +{len(links)}links +{len(images)}img {ms}ms {url[:56]}", flush=True)
    except Exception as e:
        try:
            d.scan(url, status="render_error", note=str(e)[:200]); terminal = True
        except Exception:
            pass
        stats["fail"] += 1
        print(f"  ERR {url} :: {str(e)[:120]}", flush=True)
    finally:
        if not terminal:   # crash/cancel before any scan -> don't leave the row stuck in 'fetching'
            try:
                d.scan(url, status="error", note="no terminal state (crash/cancel)")
            except Exception:
                pass


async def _worker(browser_obj, queue, cfg, d, stats):
    tab = await browser_obj.get("about:blank", new_tab=True)
    blk = netblock.ResourceBlocker(tab, block_types=cfg.block_types)
    await blk.enable()
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            url, depth = item
            await _fetch_one(tab, url, depth, cfg, d, stats)
            queue.task_done()
    finally:
        await blk.disable()
        try:
            await tab.close()
        except Exception:
            pass


async def crawl(cfg):
    """Run the crawl described by cfg (a Config). Returns the stats dict.

    `cfg.max_pages` is a cap on pages ATTEMPTED this run (each claimed URL counts whether it succeeds,
    times out, or errors) — so a broken site can't loop forever. `stats['ok']` is the real page count."""
    os.makedirs(cfg.store_root, exist_ok=True)
    d = db.open_db(cfg.db_dsn)
    try:
        d.init_schema()
        reclaimed = d.reclaim_stuck()          # recover any rows a previous crashed run left 'fetching'
        if reclaimed:
            print(f"reclaimed {reclaimed} stuck 'fetching' rows -> 'queued'", flush=True)
        for s in cfg.seeds:
            d.enqueue(s, 0)
        print(f"crawl: seeds={len(cfg.seeds)} db={cfg.db_dsn} store={cfg.store_root} "
              f"max={cfg.max_pages} tabs={cfg.tabs} depth={cfg.depth}", flush=True)
        b = await browser.attach(cfg.port)
        stats = {"ok": 0, "fail": 0, "chars": 0}
        attempted = 0
        while attempted < cfg.max_pages:
            batch = d.claim(min(cfg.tabs * 3, cfg.max_pages - attempted), shuffle=cfg.shuffle)
            if not batch:
                break
            queue = asyncio.Queue()
            for u, dep in batch:
                queue.put_nowait((u, dep))
            for _ in range(cfg.tabs):
                queue.put_nowait(None)
            workers = [asyncio.create_task(_worker(b, queue, cfg, d, stats)) for _ in range(cfg.tabs)]
            await asyncio.gather(*workers)
            attempted += len(batch)
        print(f"\nDONE: {stats['ok']} ok, {stats['fail']} fail ({attempted} attempted), "
              f"{stats['chars']} text chars\n", flush=True)
        if cfg.rl is not None:
            backed = cfg.rl.snapshot()
            if backed:
                print(f"rate-limit backoff (host -> delay/strikes): {backed}", flush=True)
        print(graph.summary_text(d))
        return stats
    finally:
        d.close()


class Config:
    def __init__(self, seeds, db_dsn, store_root, max_pages=200, tabs=8,
                 depth=3, nav_timeout=12.0, port=8731, hosts=None, keep_js=True,
                 rate_delay=1.5, shuffle=True):
        self.seeds = list(seeds)
        self.db_dsn = db_dsn
        self.store_root = store_root
        self.max_pages = max_pages
        self.tabs = tabs
        self.depth = depth
        self.nav_timeout = nav_timeout
        self.port = port
        # default: stay on the seeds' own hosts (boundary match, see _same_site)
        self.hosts = hosts or sorted({urlsplit(s).netloc.lower().split(":")[0] for s in seeds})
        self.block_types = netblock.TEXT_ONLY_KEEP_JS if keep_js else netblock.TEXT_ONLY
        # per-host politeness + 429 backoff (shared across all tabs). rate_delay=0 disables.
        self.shuffle = shuffle
        self.rl = ratelimit.RateLimiter(base_delay=rate_delay) if rate_delay and rate_delay > 0 else None


def _parse_args(argv):
    p = argparse.ArgumentParser(prog="crawl.run", description="Point-and-go site crawler.")
    p.add_argument("--seed", action="append", required=True, help="Seed URL (repeatable)")
    p.add_argument("--db", required=True, help="SQLite path or a SQLAlchemy URL (postgresql+psycopg://…, mysql+pymysql://…)")
    p.add_argument("--store", default=None, help="On-disk page store dir (default: <db>.pages)")
    p.add_argument("--max", type=int, default=200, help="Max pages ATTEMPTED this run")
    p.add_argument("--tabs", type=int, default=8, help="Parallel browser tabs")
    p.add_argument("--depth", type=int, default=3, help="Max link depth from a seed")
    p.add_argument("--nav-timeout", type=float, default=12.0, help="Per-page nav budget (s)")
    p.add_argument("--port", type=int, default=int(os.environ.get("PH_PORT", "8731")))
    p.add_argument("--host", action="append", help="Extra host to allow (repeatable)")
    p.add_argument("--no-js", action="store_true", help="Block Script too (leanest; static sites)")
    p.add_argument("--rate-delay", type=float, default=1.5,
                   help="Min seconds between fetches to the SAME host (per-host politeness + 429 backoff). 0 disables.")
    p.add_argument("--no-shuffle", action="store_true",
                   help="Crawl a depth band in url order (clusters a host) instead of randomised (spreads hosts).")
    return p.parse_args(argv)


def main(argv=None):
    a = _parse_args(argv if argv is not None else sys.argv[1:])
    is_url = "://" in a.db
    store_root = a.store or ("crawl_pages" if is_url
                             else a.db.rsplit("/", 1)[-1].split("?")[0] + ".pages")
    hosts = sorted({urlsplit(s).netloc.lower().split(":")[0] for s in a.seed} | set(a.host or []))
    cfg = Config(seeds=a.seed, db_dsn=a.db, store_root=store_root,
                 max_pages=a.max, tabs=a.tabs, depth=a.depth, nav_timeout=a.nav_timeout,
                 port=a.port, hosts=hosts, keep_js=not a.no_js,
                 rate_delay=a.rate_delay, shuffle=not a.no_shuffle)
    asyncio.run(crawl(cfg))


if __name__ == "__main__":
    main()
