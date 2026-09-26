"""crawl_theage.py — crawl the whole The Age website, saving articles and image assets.

Runs a deep multi-tab crawl against https://www.theage.com.au with JavaScript disabled (--no-js) to
bypass client-side paywall script DOM truncation, content-addresses all article HTML in a compressed
store, and harvests all image assets (via Chrome Simple Cache and direct image asset downloads).

Usage:
    python scripts/crawl_theage.py [--max 5000] [--tabs 8] [--depth 6] [--rate-delay 1.0]

Logs and outputs:
    Database:    tmp/theage_full.sqlite
    Page Store:  tmp/theage_full.sqlite.pages
    Asset Store: tmp/theage_full.assets
    Log file:    tmp/logs/crawl_theage.log
"""
import argparse
import asyncio
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# Ensure repo root is on sys.path
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from assets import cache, classify, imgmeta, store as asset_store  # noqa: E402, I001
from crawl import db, run as crawl_run  # noqa: E402, I001


class Logger:
    """Tee output to stdout and a log file."""
    def __init__(self, log_path):
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        self.log_file = open(log_path, "a", encoding="utf-8")
        self.stdout = sys.stdout

    def write(self, message):
        self.stdout.write(message)
        self.log_file.write(message)
        self.log_file.flush()

    def flush(self):
        self.stdout.flush()
        self.log_file.flush()


def harvest_and_download_images(d, asset_dir, logger):
    """Harvest Chrome cache first, then download missing image assets."""
    logger.write("\n=== Step 2: Harvesting Chrome Cache for Images ===\n")
    cached_count = 0

    def sink(url, data, mime):
        nonlocal cached_count
        if classify.is_junk_url(url):
            return
        m = imgmeta.probe(data)
        if classify.is_tracking_pixel(m.width, m.height):
            return
        sa = asset_store.store_asset(data, asset_dir, mime=mime, src_url=url, meta=m)
        d.upsert_asset(sa)
        cached_count += 1

    try:
        cache.harvest(sink, url_filter=lambda u: any(h in u for h in ("theage.com.au", "ffx.io", "static.ffx")))
        logger.write(f"Harvested {cached_count} image assets directly from Chrome cache.\n")
    except Exception as e:
        logger.write(f"Cache harvest note: {e}\n")

    logger.write("\n=== Step 3: Downloading Remaining Image Assets ===\n")
    with d.engine.connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT DISTINCT img_url FROM page_asset WHERE asset_sha IS NULL OR asset_sha = ''"
        ).fetchall()

    urls = [r[0] for r in rows if r[0] and not classify.is_junk_url(r[0])]
    logger.write(f"Found {len(urls)} distinct image URLs to download.\n")

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Referer": "https://www.theage.com.au/"
    }

    def download_one(img_url):
        try:
            req = urllib.request.Request(img_url, headers=headers)
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = resp.read()
                content_type = resp.headers.get("Content-Type")
                m = imgmeta.probe(data)
                if classify.is_tracking_pixel(m.width, m.height):
                    return None
                sa = asset_store.store_asset(data, asset_dir, mime=content_type, src_url=img_url, meta=m)
                d.upsert_asset(sa)
                with d.engine.begin() as tx:
                    tx.exec_driver_sql(
                        "UPDATE page_asset SET asset_sha = :sha WHERE img_url = :url",
                        {"sha": sa.sha256, "url": img_url}
                    )
                return sa
        except Exception:
            return None

    if urls:
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(download_one, urls))
        downloaded = [r for r in results if r is not None]
        logger.write(f"Downloaded {len(downloaded)} new image assets.\n")

    logger.write("\n=== Step 4: Updating Image Metadata & Photos Index ===\n")
    with d.engine.connect() as conn:
        asset_rows = conn.exec_driver_sql("SELECT sha256, ext, media_kind, src_url, content_type, bytes FROM asset").fetchall()

    with d.engine.begin() as tx:
        for row in asset_rows:
            sha, ext, media_kind, src_url, content_type, size_b = row
            data = asset_store.load_asset(asset_dir, sha, ext)
            if data:
                m = imgmeta.probe(data)
                tx.exec_driver_sql(
                    "UPDATE asset SET width = :w, height = :h, img_format = :fmt, img_mode = :mode, "
                    "is_probably_photo = :photo WHERE sha256 = :sha",
                    {"w": m.width, "h": m.height, "fmt": m.img_format, "mode": m.img_mode,
                     "photo": 1 if m.is_probably_photo else 0, "sha": sha}
                )


def print_summary(d, db_path, page_store, asset_dir, logger):
    """Print comprehensive summary of full crawl."""
    logger.write("\n============================================================\n")
    logger.write("               THE AGE FULL CRAWL REPORT                    \n")
    logger.write("============================================================\n")

    with d.engine.connect() as conn:
        page_count = conn.exec_driver_sql("SELECT COUNT(*) FROM page").fetchone()[0]
        ok_pages = conn.exec_driver_sql("SELECT COUNT(*) FROM page WHERE scan_status = 'ok'").fetchone()[0]
        text_chars = conn.exec_driver_sql("SELECT SUM(text_chars) FROM page WHERE scan_status = 'ok'").fetchone()[0] or 0
        total_links = conn.exec_driver_sql("SELECT COUNT(*) FROM page_link").fetchone()[0]
        queued_urls = conn.exec_driver_sql("SELECT COUNT(*) FROM frontier WHERE state = 'queued'").fetchone()[0]

        total_assets = conn.exec_driver_sql("SELECT COUNT(*) FROM asset").fetchone()[0]
        photo_count = conn.exec_driver_sql("SELECT COUNT(*) FROM asset WHERE is_probably_photo = 1").fetchone()[0]
        total_asset_bytes = conn.exec_driver_sql("SELECT SUM(bytes) FROM asset").fetchone()[0] or 0

    logger.write(f"  Pages Crawled (OK)          : {ok_pages:,} / {page_count:,} total\n")
    logger.write(f"  Total Text Content Length   : {text_chars:,} characters\n")
    logger.write(f"  Page-to-Page Links Saved    : {total_links:,}\n")
    logger.write(f"  Frontier Queue Remaining    : {queued_urls:,} URLs\n")
    logger.write(f"  Distinct Image Assets Stored: {total_assets:,}\n")
    logger.write(f"  Photos (>=200x200 RGB/RGBA) : {photo_count:,}\n")
    logger.write(f"  Image Storage Size on Disk  : {total_asset_bytes / 1024 / 1024:.2f} MB\n")
    logger.write("------------------------------------------------------------\n")
    logger.write(f"  SQLite Database File        : {db_path}\n")
    logger.write(f"  Compressed HTML Page Store  : {page_store}\n")
    logger.write(f"  Image Asset Store           : {asset_dir}\n")
    logger.write("============================================================\n")


def main():
    p = argparse.ArgumentParser(description="Crawl The Age website end-to-end (articles & photos).")
    p.add_argument("--max", type=int, default=5000, help="Max pages to crawl (default 5000)")
    p.add_argument("--tabs", type=int, default=8, help="Browser tabs (default 8)")
    p.add_argument("--depth", type=int, default=6, help="Link depth limit (default 6)")
    p.add_argument("--rate-delay", type=float, default=1.0, help="Per-host rate delay in seconds (default 1.0)")
    p.add_argument("--db", default="tmp/theage_full.sqlite", help="SQLite DB path")
    args = p.parse_args()

    log_path = os.path.join(REPO, "tmp", "logs", "crawl_theage.log")
    logger = Logger(log_path)
    sys.stdout = logger

    db_path = os.path.abspath(args.db)
    page_store = db_path + ".pages"
    asset_dir = os.path.join(os.path.dirname(db_path), "theage_full.assets")

    logger.write("Starting full crawl of The Age (https://www.theage.com.au)...\n")
    logger.write(f"Config: max={args.max}, tabs={args.tabs}, depth={args.depth}, rate_delay={args.rate_delay}s\n")
    logger.write(f"Output DB: {db_path}\nLog: {log_path}\n\n")

    seed = "https://www.theage.com.au"
    hosts = ["www.theage.com.au", "theage.com.au"]

    cfg = crawl_run.Config(
        seeds=[seed], db_dsn=db_path, store_root=page_store,
        max_pages=args.max, tabs=args.tabs, depth=args.depth,
        nav_timeout=12.0, port=8731, hosts=hosts, keep_js=False,  # keep_js=False -> --no-js mode
        rate_delay=args.rate_delay, shuffle=True, host_diverse=True, stall_ceiling=30
    )

    t0 = time.monotonic()
    asyncio.run(crawl_run.crawl(cfg))
    elapsed = time.monotonic() - t0

    logger.write(f"\nCrawl loop completed in {elapsed:.1f} seconds.\n")

    d = db.open_db(db_path)
    harvest_and_download_images(d, asset_dir, logger)
    print_summary(d, db_path, page_store, asset_dir, logger)


if __name__ == "__main__":
    main()
