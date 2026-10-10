"""rank_test.py — crawl.rank scores the right pages, and crawl.run --rank fetches them first (#2).

Offline (no browser, no network): url-shape scoring, sitemap discovery (robots.txt, index, .gz,
bounds), OpenPageRank parsing and the secrets lookup, all with injected fetches.
Live: crawl.run --rank against a local site whose important page is only in its sitemap, on an
isolated engine (own Xvfb). A 2-page budget must spend itself on the sitemap page, not on tag pages.

    python scripts/rank_test.py                 # both
    python scripts/rank_test.py --offline       # no engine

Results: tmp/logs/rank-test.log
"""
import argparse
import gzip
import http.server
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from crawl import rank  # noqa: E402
from engine import connect  # noqa: E402

LOG = os.path.join(REPO, "tmp", "logs", "rank-test.log")
WORK = os.path.join(REPO, "tmp", "rank-test")
# ~1.25x the measured 2-page ranked crawl (issue #2). Expiry = the crawl did not finish.
CRAWL_BUDGET = 10.0

PASS, FAIL = [], []
_fh = None


def say(*a):
    line = " ".join(str(x) for x in a)
    print(line, flush=True)
    if _fh:
        _fh.write(line + "\n"); _fh.flush()


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    say(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def offline():
    s = rank.shape_score
    ok("a site root outscores a deep page", s("https://a.test/") > s("https://a.test/a/b/c/d"))
    ok("a tag listing scores under the same-depth page", s("https://a.test/tag/x") < s("https://a.test/doc/x"))
    ok("a dated archive is avoided", s("https://a.test/2024/05/post") < s("https://a.test/x/y/post"))
    ok("prefer keywords lift a page", s("https://a.test/a/b/pricing", prefer=["pricing"]) >
       s("https://a.test/a/b/other", prefer=["pricing"]))
    ok("avoid keywords lower a page", s("https://a.test/x", avoid=["x"]) < s("https://a.test/y"))
    ok("scores stay in [0, 1]", all(0 <= s(u, prefer=["a", "b", "c"]) <= 1 for u in
                                    ("https://a.test/", "https://a.test/a/b/c")))

    sm = (b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
          b'<url><loc>https://a.test/docs/guide</loc></url><url><loc>https://a.test/about</loc>'
          b'</url></urlset>')
    idx = (b'<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           b'<sitemap><loc>https://a.test/s1.xml.gz</loc></sitemap></sitemapindex>')
    site = {"https://a.test/robots.txt": b"User-agent: *\nSitemap: https://a.test/idx.xml\n",
            "https://a.test/idx.xml": idx, "https://a.test/s1.xml.gz": gzip.compress(sm)}
    urls, notes = rank.sitemap_urls("https://a.test", site.get)
    ok("robots.txt -> index -> gzipped sitemap", urls == {"https://a.test/docs/guide",
                                                         "https://a.test/about"}, f"{urls} {notes}")
    urls, _ = rank.sitemap_urls("https://b.test", {"https://b.test/sitemap.xml": sm}.get)
    ok("no robots Sitemap: line -> /sitemap.xml", len(urls) == 2, str(urls))
    urls, notes = rank.sitemap_urls("https://c.test", {}.get)
    ok("no sitemap at all is an empty set, with a note", urls == set() and notes, str(notes))

    resp = {"status_code": 200, "response": [
        {"status_code": 200, "page_rank_decimal": 6.5, "domain": "a.test"},
        {"status_code": 404, "page_rank_decimal": "", "domain": "nope.test"}]}
    seen = []
    ranks = rank.opr_ranks({"a.test", "nope.test"}, "k", lambda u: seen.append(u) or resp)
    ok("OpenPageRank: known domains ranked, unknown left out", ranks == {"a.test": 6.5}, str(ranks))
    ok("OpenPageRank query carries every domain", "a.test" in seen[0] and "nope.test" in seen[0],
       seen[0][:120])
    ok("no key -> no OpenPageRank call", rank.opr_ranks({"a.test"}, None, lambda u: 1 / 0) == {})
    with tempfile.TemporaryDirectory() as t:
        p = os.path.join(t, "s.yaml")
        open(p, "w").write("other:\n  password: x\n")
        ok("secrets without an openpagerank section -> no key", rank.opr_key(p) is None)
        open(p, "w").write("openpagerank:\n  url: https://openpagerank.com\n  password: KEY\n")
        ok("secrets openpagerank.password -> the key", rank.opr_key(p) == "KEY")
    r = rank.Ranker(fetch=site.get, key="k", fetch_json=lambda u: resp, say=lambda m: None)
    r.prepare(["https://a.test"])
    ok("Ranker: a sitemap page outscores an unlisted one at the same depth",
       r.score("https://a.test/docs/guide") > r.score("https://a.test/docs/other"))
    ok("Ranker: OPR adds rank/10", abs(r.score("https://a.test/") - (0 + 1.0 + 0.65)) < 1e-9,
       str(r.score("https://a.test/")))


PAGES = {
    "/": ('<title>home</title>' + "".join(f'<a href="/tag/t{i}">t{i}</a> ' for i in range(6))),
    "/robots.txt": "User-agent: *\nSitemap: {base}/sitemap.xml\n",
    "/sitemap.xml": ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/'
                     'sitemap/0.9"><url><loc>{base}/docs/guide</loc></url></urlset>'),
    "/docs/guide": "<title>guide</title><p>the substance</p>",
}
for i in range(6):
    PAGES[f"/tag/t{i}"] = f"<title>tag {i}</title>"


def live(port):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    Site.base = base = f"http://127.0.0.1:{srv.server_address[1]}"
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    db = os.path.join(WORK, "r.sqlite")
    try:
        connect.ensure(port, want_browser=True)
        cmd = [sys.executable, "-m", "crawl.run", "--seed", f"{base}/", "--db", db,
               "--store", os.path.join(WORK, "pages"), "--max", "2", "--tabs", "1",
               "--depth", "2", "--rate-delay", "0", "--rank", "--port", str(port)]
        t0 = time.monotonic()
        try:
            r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=CRAWL_BUDGET,
                               env={**os.environ, "PH_CRAWL_MEMORY_MAX": "0"})
            out = r.stdout + r.stderr
            say(f"    ranked crawl took {time.monotonic() - t0:.1f}s of a {CRAWL_BUDGET:.0f}s budget")
        except subprocess.TimeoutExpired:
            ok("ranked crawl finishes", False, f"over {CRAWL_BUDGET}s")
            return
        for line in out.splitlines():
            if line.startswith(("rank:", "DONE", "Traceback")) or "Error" in line:
                say("   ", line[:150])
        rows = dict(sqlite3.connect(db).execute("select url, state from frontier").fetchall())
        ok("ranked crawl fetched the sitemap-only page within a 2-page budget",
           rows.get(f"{base}/docs/guide") == "done", str(rows.get(f"{base}/docs/guide")))
        ok("...and left the tag listings queued",
           all(rows.get(f"{base}/tag/t{i}") == "queued" for i in range(6)),
           str({k: v for k, v in rows.items() if "/tag/" in k}))
    finally:
        srv.shutdown()
        try:
            connect.call("shutdown", port=port, timeout=15.0)
        except Exception as e:
            say(f"  shutdown: {str(e)[:80]}")


class Site(http.server.BaseHTTPRequestHandler):
    base = ""

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = PAGES.get(self.path)
        if body is None:
            self.send_response(404); self.end_headers(); return
        b = body.replace("{base}", self.base).encode()
        self.send_response(200)
        ctype = "text/plain" if self.path.endswith(".txt") else (
            "application/xml" if self.path.endswith(".xml") else "text/html")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


def main():
    global _fh
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8739, help="isolated engine port (never 8731)")
    ap.add_argument("--offline", action="store_true", help="skip the live crawl")
    ap.add_argument("--headed", action="store_true",
                    help="show the test browser on your desktop (default: its own Xvfb, PH_DISPLAY=xvfb)")
    a = ap.parse_args()
    if a.port == 8731:
        sys.exit("refusing to run on 8731: use an isolated engine")
    if not a.headed:
        os.environ["PH_DISPLAY"] = "xvfb"   # headed Chrome off the desktop; --headed to watch
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    _fh = open(LOG, "w")
    offline()
    if not a.offline:
        live(a.port)
    say(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    say(f"log: {LOG}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
