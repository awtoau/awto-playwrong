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


async def _fetch_one(tab, url, depth, cfg, d, stats, link_code=None):
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
            # 'blocked' is the valid SCAN_STATUSES member for a rate-limit (429/503); 'rate_limited'
            # is NOT in the CHECK vocabulary and would violate the constraint.
            d.scan(url, status="blocked", http_status=doc_status["code"],
                   note=f"429/503 backoff -> {rl._h(host).delay:.1f}s"); terminal = True
            d.enqueue(url, depth, link_code=link_code)   # requeue (keep the tag) for a slower attempt
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
        # link_code (the consumer tag) rides from this URL onto its page, its image refs, AND every
        # same-site link discovered under it — so a whole subtree stays tagged with the seed's code.
        d.upsert_page(sp, url, title=sp.title, text_chars=sp.text_chars, status="ok", link_code=link_code)
        d.add_links(sp.sha256, links)
        for iu, alt, kind in images:
            d.link_page_asset(sp.sha256, iu[:2000], alt=(alt or "")[:500], kind=kind, link_code=link_code)
        for lu in links:
            if _same_site(lu, cfg.hosts) and depth + 1 <= cfg.depth:
                d.enqueue(lu, depth + 1, discovered_from=url, link_code=link_code)
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


async def _auto_dismiss_dialogs(tab):
    """Auto-accept every JS dialog so a page can NEVER hang the tab. beforeunload ("Leave page? /
    Changes may not be saved") blocks navigation until answered; permission prompts (notifications,
    geolocation), alert()/confirm()/prompt() do the same. Chrome pauses the tab waiting for a human —
    the crawler has none, so it would stall. We handle every Page.javascriptDialogOpening by accepting
    it (beforeunload accept = 'leave', which is what we want since we're navigating away). Best-effort."""
    try:
        from nodriver import cdp as _cdp
        await tab.send(_cdp.page.enable())

        def _on_dialog(ev):
            async def _accept():
                try:
                    await tab.send(_cdp.page.handle_java_script_dialog(accept=True))
                except Exception:
                    pass
            try:
                import asyncio as _a
                _a.ensure_future(_accept())
            except Exception:
                pass
        tab.add_handler(_cdp.page.JavascriptDialogOpening, _on_dialog)
    except Exception:
        pass


async def _new_tab(b, cfg):
    """Open one crawl tab (resource blocker + JS-dialog auto-dismiss enabled). Returns (tab, blocker)."""
    tab = await b.get("about:blank", new_tab=True)
    blk = netblock.ResourceBlocker(tab, block_types=cfg.block_types)
    await blk.enable()
    await _auto_dismiss_dialogs(tab)   # never hang on a "Leave page?" / permission dialog
    return tab, blk


async def _recycle(b, cfg, tab, blk):
    """Replace a tab that has gone bad — close the old one, open a fresh one. Isolation without a leak:
    a wedged/leaked renderer is discarded so it can't poison the next page, but we never grow the tab
    count. Best-effort close (a crashed tab may already be gone)."""
    try:
        await blk.disable()
    except Exception:
        pass
    try:
        await tab.close()
    except Exception:
        pass
    return await _new_tab(b, cfg)


async def _worker(slot, b, queue, cfg, d, stats):
    """Pull URLs from the shared queue until a None sentinel. FRESH TAB PER URL: every URL gets its own
    brand-new tab (with its own resource blocker), and the tab is CLOSED as soon as the page is done —
    no tab is ever reused across URLs. This is the strongest process isolation (a page can't leak state,
    cookies, or a wedged renderer into the next one) at the cost of a tab open/close per page. `slot` is
    kept for signature/pool compatibility but no persistent tab lives in it."""
    while True:
        item = await queue.get()
        try:
            if item is None:
                return
            url, depth, link_code = item
            tab = blk = None
            try:
                tab, blk = await _new_tab(b, cfg)          # a fresh tab for THIS url only
                # HARD STALL GUARD: one ceiling over the WHOLE fetch (nav + render + capture). A page
                # that wedges the tab (e.g. a 'Leave site?' beforeunload the auto-dismiss missed, an
                # infinite redirect, a hung renderer) can't stall the crawl — we abandon it and the tab
                # is closed below. Ceiling = generous multiple of the nav budget so a slow-but-fine page
                # still finishes.
                # Ceiling over the WHOLE fetch. Explicit --stall-ceiling wins; else a generous multiple
                # of the nav budget. Lower it to abandon slow pages sooner (fresh-tab-per-URL means a
                # faster abandon = the next tab starts sooner — the aggressive "cut losses" mode).
                stall_ceiling = cfg.stall_ceiling if cfg.stall_ceiling else max(45.0, cfg.nav_timeout * 4)
                try:
                    await asyncio.wait_for(
                        _fetch_one(tab, url, depth, cfg, d, stats, link_code), timeout=stall_ceiling)
                except asyncio.TimeoutError:
                    try:
                        d.scan(url, status="render_error", note=f"stall>{stall_ceiling:.0f}s (tab abandoned)")
                    except Exception:
                        pass
                    stats["fail"] += 1
                    print(f"  STALL >{stall_ceiling:.0f}s abandoned {url[:60]}", flush=True)
            except Exception:
                pass   # _fetch_one records its own terminal state; never let a crash kill the worker
            finally:
                if blk is not None:
                    try:
                        await blk.disable()
                    except Exception:
                        pass
                if tab is not None:
                    try:
                        await tab.close()                  # close after every URL — never reuse
                    except Exception:
                        pass
        finally:
            queue.task_done()


async def crawl(cfg):
    """Run the crawl described by cfg (a Config). Returns the stats dict.

    `cfg.max_pages` is a cap on pages ATTEMPTED this run (each claimed URL counts whether it succeeds,
    times out, or errors) — so a broken site can't loop forever. `stats['ok']` is the real page count.

    Tab discipline: FRESH TAB PER URL. cfg.tabs is the CONCURRENCY (that many workers run at once); each
    worker opens a brand-new tab for every URL and closes it right after — no tab is ever reused. So the
    only tabs alive at any moment are the ≤cfg.tabs in-flight ones; each closes itself when its page is
    done, so there is nothing to sweep at the end."""
    os.makedirs(cfg.store_root, exist_ok=True)
    d = db.open_db(cfg.db_dsn)
    b = None
    try:
        d.init_schema()
        reclaimed = d.reclaim_stuck()          # recover any rows a previous crashed run left 'fetching'
        if reclaimed:
            print(f"reclaimed {reclaimed} stuck 'fetching' rows -> 'queued'", flush=True)
        for s in cfg.seeds:
            d.enqueue(s, 0)
        print(f"crawl: seeds={len(cfg.seeds)} db={cfg.db_dsn} store={cfg.store_root} "
              f"max={cfg.max_pages} tabs={cfg.tabs} depth={cfg.depth} (fresh tab per URL)", flush=True)
        b = await browser.attach(cfg.port)
        stats = {"ok": 0, "fail": 0, "chars": 0}
        attempted = 0
        while attempted < cfg.max_pages:
            batch = d.claim(min(cfg.tabs * 3, cfg.max_pages - attempted),
                            shuffle=cfg.shuffle, host_diverse=cfg.host_diverse)
            if not batch:
                break
            queue = asyncio.Queue()
            for item in batch:                 # (url, depth, link_code)
                queue.put_nowait(item)
            for _ in range(cfg.tabs):
                queue.put_nowait(None)     # one sentinel per worker
            # cfg.tabs concurrent workers; each makes + closes its OWN tab per URL (slot arg unused now).
            workers = [asyncio.create_task(_worker([None], b, queue, cfg, d, stats))
                       for _ in range(cfg.tabs)]
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
        # Fresh-tab-per-URL means every tab already closed itself after its page — nothing to sweep here
        # (and NO global sweep: other crawls may share this browser). Just release the DB.
        d.close()


class Config:
    def __init__(self, seeds, db_dsn, store_root, max_pages=200, tabs=8,
                 depth=3, nav_timeout=12.0, port=8731, hosts=None, keep_js=True,
                 rate_delay=1.5, shuffle=True, host_diverse=True, stall_ceiling=0.0):
        self.seeds = list(seeds)
        self.db_dsn = db_dsn
        self.store_root = store_root
        self.max_pages = max_pages
        self.tabs = tabs
        self.depth = depth
        self.nav_timeout = nav_timeout
        self.stall_ceiling = stall_ceiling   # hard whole-fetch cap; 0 => auto (max(45, nav*4))
        self.port = port
        # default: stay on the seeds' own hosts (boundary match, see _same_site)
        self.hosts = hosts or sorted({urlsplit(s).netloc.lower().split(":")[0] for s in seeds})
        self.block_types = netblock.TEXT_ONLY_KEEP_JS if keep_js else netblock.TEXT_ONLY
        # per-host politeness + 429 backoff (shared across all tabs). rate_delay=0 disables.
        self.shuffle = shuffle
        self.host_diverse = host_diverse
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
    p.add_argument("--stall-ceiling", type=float, default=0.0,
                   help="Hard cap (s) on the WHOLE fetch (nav+render+capture) before abandoning the page + tab. "
                        "0 = auto (max(45, nav*4)). LOWER it to cut losses on slow pages sooner — a faster "
                        "abandon means the next fresh tab starts sooner (aggressive mode; e.g. 25).")
    p.add_argument("--port", type=int, default=int(os.environ.get("PH_PORT", "8731")))
    p.add_argument("--host", action="append", help="Extra host to allow (repeatable)")
    p.add_argument("--no-js", action="store_true", help="Block Script too (leanest; static sites)")
    p.add_argument("--rate-delay", type=float, default=1.5,
                   help="Min seconds between fetches to the SAME host (per-host politeness + 429 backoff). 0 disables.")
    p.add_argument("--no-shuffle", action="store_true",
                   help="Crawl a depth band in url order (clusters a host) instead of randomised (spreads hosts).")
    p.add_argument("--no-host-diverse", action="store_true",
                   help="Disable round-robin-by-host claiming (one site per tab). On by default for multi-site crawls.")
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
                 rate_delay=a.rate_delay, shuffle=not a.no_shuffle,
                 host_diverse=not a.no_host_diverse, stall_ceiling=a.stall_ceiling)
    asyncio.run(crawl(cfg))


if __name__ == "__main__":
    main()
