"""crawl_browser.py — crawl powderhounds in a VISIBLE (headed) real browser.

Why: plain requests.get() gets Cloudflare-403'd (Turnstile). A real headed browser runs the
JS challenge and passes it, so we capture the pages requests.py couldn't.

- HEADED: headless=False — a real window you can watch (and hand-solve a challenge if needed).
- async Playwright (sync API segfaults under python3.14t free-threaded; async is fine).
- Reuses crawl.py's queue/bucket/robots logic. Writes the same raw_page rows.
- fetch-once: skips URLs already cached 2xx. Re-runs resume.
- 'passed' = Turnstile cleared (no cf challenge text + real content present).

Run:  .venv/bin/python scripts/crawl_browser.py        (writes tmp/crawl-browser.log)
Env:  PH_MAX_PAGES (default 200), PH_DELAY (default 5s, politeness).
"""
import os, sys, re, asyncio
from datetime import datetime, timezone
from collections import deque
from urllib.parse import urlparse, urljoin
sys.path.insert(0, os.path.dirname(__file__))
from ph_common import connect, BASE, UA
from playwright.async_api import async_playwright

DELAY = float(os.environ.get("PH_DELAY", "5"))
MAX_PAGES = int(os.environ.get("PH_MAX_PAGES", "200"))
LOGFILE = os.path.join(os.path.dirname(__file__), "..", "tmp", "crawl-browser.log")

# --- reuse crawl.py's policy ---
BUCKETS = ["Japan", "Canada", "USA", "Europe", "SouthAmerica", "NewZealand", "Other"]
ROW_DEPTH = {"Japan": 99}
DEFAULT_DEPTH = 5
DISALLOW = ("/Booking-Form-Collections/", "/Disclaimer", "/Copyright", "/Advertising",
            "/cc/", "/members/", "/Members/")
DISALLOW_SUB = ("page=1", "page=2", "page=3", "searchkey=", "newsearch=true", "template=", "add-rating")

def log(action, **kw):
    ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    line = f"{ts} {action} " + " ".join(f"{k}={v}" for k, v in kw.items())
    with open(LOGFILE, "a") as f: f.write(line + "\n")
    print(line, flush=True)

def allowed(path, query):
    if any(path.startswith(p) for p in DISALLOW): return False
    if any(s in (query or "") for s in DISALLOW_SUB): return False
    return True

def bucket_of(path):
    seg = path.strip("/").split("/")
    return seg[0] if seg and seg[0] in BUCKETS else None

def already_cached(conn, url):
    row = conn.execute("SELECT http_status FROM raw_page WHERE url=%s", (url,)).fetchone()
    return row is not None and row[0] is not None and 200 <= row[0] < 300

def extract_links(html, base_url):
    out = []
    for href in re.findall(r'href="([^"#]*)"', html):
        u = urljoin(base_url, href.split("#")[0]); pr = urlparse(u)
        if pr.netloc and "powderhounds.com" not in pr.netloc: continue
        out.append(pr.path + (("?" + pr.query) if pr.query else ""))
    return out

# Turnstile / Cloudflare challenge markers — if present, we did NOT pass.
CHALLENGE = re.compile(r"challenge-platform|cf-chl|Just a moment|Verifying you are human|turnstile", re.I)

async def passed_challenge(page):
    """Wait for the real page; return (html, passed). Headed browser solves Turnstile automatically;
    if it stalls (manual challenge), the visible window lets a human click it."""
    try:
        await page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    html = await page.content()
    title = (await page.title()) or ""
    is_challenge = bool(CHALLENGE.search(html)) or "just a moment" in title.lower()
    return html, (not is_challenge and len(html) > 2000)

async def main():
    seeds = [(f"/{b}.aspx", b, 0) for b in BUCKETS]
    q = deque(seeds); seen = set(u for u, _, _ in seeds); fetched = 0
    log("crawl_start", mode="HEADED_BROWSER", delay=DELAY, cap=MAX_PAGES, seeds=len(seeds))
    async with async_playwright() as p:
        # HEADED visible window. Real UA. Real viewport. This is what passes Turnstile.
        browser = await p.chromium.launch(headless=False, args=["--start-maximized"])
        ctx = await browser.new_context(user_agent=UA, viewport={"width": 1400, "height": 900})
        page = await ctx.new_page()
        with connect() as conn:
            while q and fetched < MAX_PAGES:
                path, bucket, depth = q.popleft()
                url = BASE + path; pr = urlparse(url)
                if not allowed(pr.path, pr.query):
                    log("skip_disallow", url=pr.path); continue
                if already_cached(conn, url):
                    row = conn.execute("SELECT raw_html FROM raw_page WHERE url=%s", (url,)).fetchone()
                    html = row[0]; log("skip_cached", url=pr.path)
                else:
                    await asyncio.sleep(DELAY)  # politeness
                    try:
                        resp = await page.goto(url, timeout=45000, wait_until="domcontentloaded")
                        html, ok = await passed_challenge(page)
                        status = (resp.status if resp else 0)
                        # if the browser rendered real content, treat as 200 even if cf returned 403 first
                        eff_status = 200 if ok else status
                        conn.execute(
                            """INSERT INTO raw_page (url, http_status, content_type, raw_html)
                               VALUES (%s,%s,%s,%s) ON CONFLICT (url) DO UPDATE
                               SET http_status=EXCLUDED.http_status, raw_html=EXCLUDED.raw_html,
                                   fetched_at=now(), fetch_note=NULL""",
                            (url, eff_status, "text/html", html))
                        conn.commit()
                        log("fetch", url=pr.path, status=status, passed=ok, bytes=len(html))
                    except Exception as e:
                        log("fetch_error", url=pr.path, err=str(e)[:80]); html = None
                    fetched += 1
                if html and depth < ROW_DEPTH.get(bucket, DEFAULT_DEPTH):
                    for link in extract_links(html, url):
                        lb = bucket_of(link)
                        if lb is None: continue
                        full = BASE + link
                        if full in seen: continue
                        seen.add(full); q.append((link, lb, depth + 1))
            total = conn.execute("SELECT count(*) FROM raw_page WHERE http_status=200").fetchone()[0]
            log("crawl_done", reason=("cap_reached" if fetched >= MAX_PAGES else "queue_empty"),
                fetched_this_run=fetched, total_good=total, queue_remaining=len(q))
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
