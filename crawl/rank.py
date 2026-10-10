"""crawl.rank — importance scores for the frontier, so a crawl fetches what matters first (#2).

priority = sitemap + shape + opr, each in [0, 1]:
- sitemap: 1 if the url is in its host's sitemap (robots.txt Sitemap: lines, else /sitemap.xml;
  sitemap indexes followed, .gz handled). The site's own list of its real pages.
- shape: url-shape score, no network. Shallow paths high; consumer `prefer` keywords up, the
  built-in AVOID patterns plus consumer `avoid` keywords down. No domain vocabulary ships here.
- opr: OpenPageRank domain rank / 10, when a key is in ~/.config/awto/secrets.yaml
  (section `openpagerank`, field `password` or `api_key`). Without one it is 0 and says so.

Every fetch goes through playwrong (engine.connect.download); `fetch` is injectable for tests.
"""
import gzip
import os
import re
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET

# Listing/archive/utility shapes that are rarely the substance of a site.
AVOID = (r"/tags?/", r"/category/", r"/categories/", r"/page/\d", r"/feed", r"/print", r"/author/",
         r"/search", r"replytocom=", r"/wp-json/", r"/amp/?$", r"/20\d\d/(\d\d/)?", r"/comments?/",
         r"\?(s|q|sort|filter|page)=", r"/login", r"/cart", r"/checkout")
# Bounds per host: an index can fan out to hundreds of sitemaps and millions of urls.
MAX_SITEMAPS = 50
MAX_URLS = 50_000
OPR_API = "https://openpagerank.com/api/v1.0/getPageRank"
OPR_BATCH = 100                     # the API's per-request domain limit
SECRETS = os.path.expanduser("~/.config/awto/secrets.yaml")
_AVOID_RE = re.compile("|".join(AVOID), re.I)


def shape_score(url, prefer=(), avoid=()):
    """[0, 1]: 1 for a site root, falling with path depth; +0.25 per prefer keyword (max +0.5);
    halved by any AVOID pattern or avoid keyword."""
    p = urllib.parse.urlsplit(url)
    segs = [s for s in p.path.split("/") if s]
    score = 1.0 / (1 + len(segs))
    low = url.lower()
    score += min(0.5, 0.25 * sum(1 for k in prefer if k.lower() in low))
    if _AVOID_RE.search(p.path + ("?" + p.query if p.query else "")) or any(
            k.lower() in low for k in avoid):
        score *= 0.5
    return max(0.0, min(1.0, score))


def _default_fetch(url, port=None):
    """Bytes at url through playwrong (the engine on `port`), or None."""
    from engine import connect
    fd, path = tempfile.mkstemp(prefix="rank-", dir=_tmpdir())
    os.close(fd)
    try:
        connect.download(url, path=path, port=port)
        return open(path, "rb").read()
    except Exception:
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _tmpdir():
    d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tmp")
    os.makedirs(d, exist_ok=True)
    return d


def _xml(data):
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        return None


def sitemap_urls(origin, fetch=None):
    """Page urls a site lists in its sitemaps. origin like "https://awto.au". Returns (set, notes)."""
    fetch = fetch or _default_fetch
    notes, todo, seen, out = [], [], set(), set()
    robots = fetch(origin.rstrip("/") + "/robots.txt") or b""
    for line in robots.decode(errors="replace").splitlines():
        if line.lower().startswith("sitemap:"):
            todo.append(line.split(":", 1)[1].strip())
    if not todo:
        todo.append(origin.rstrip("/") + "/sitemap.xml")
    while todo and len(seen) < MAX_SITEMAPS and len(out) < MAX_URLS:
        sm = todo.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        root = _xml(fetch(sm) or b"")
        if root is None:
            notes.append(f"unreadable sitemap {sm}")
            continue
        tag = root.tag.rsplit("}", 1)[-1]
        for loc in root.iter():
            if loc.tag.rsplit("}", 1)[-1] != "loc" or not (loc.text or "").strip():
                continue
            u = loc.text.strip()
            if tag == "sitemapindex":
                todo.append(u)
            elif len(out) < MAX_URLS:
                out.add(u)
    if todo:
        notes.append(f"stopped at {len(seen)} sitemaps / {len(out)} urls (MAX_SITEMAPS/MAX_URLS)")
    return out, notes


def opr_key(path=SECRETS):
    """The OpenPageRank API key from secrets.yaml, or None. Never printed."""
    try:
        import yaml
        sec = (yaml.safe_load(open(path)) or {}).get("openpagerank") or {}
    except (OSError, ImportError, AttributeError):
        return None
    return sec.get("api_key") or sec.get("password")


def opr_ranks(domains, key, fetch_json=None, port=None):
    """{domain: rank 0-10} from OpenPageRank; domains it does not know are left out."""
    import json
    if not key or not domains:
        return {}
    if fetch_json is None:
        def fetch_json(url):
            from engine import connect
            fd, path = tempfile.mkstemp(prefix="opr-", dir=_tmpdir())
            os.close(fd)
            try:
                connect.download(url, path=path, port=port, solve=False,
                                 extra_headers={"API-OPR": key})
                return json.load(open(path))
            finally:
                os.unlink(path)
    out, ds = {}, sorted(set(domains))
    for i in range(0, len(ds), OPR_BATCH):
        q = "&".join(f"domains%5B{j}%5D={urllib.parse.quote(d)}"
                     for j, d in enumerate(ds[i:i + OPR_BATCH]))
        for r in (fetch_json(f"{OPR_API}?{q}") or {}).get("response", []):
            if r.get("status_code") == 200 and r.get("page_rank_decimal") not in (None, ""):
                out[r["domain"]] = float(r["page_rank_decimal"])
    return out


def host_of(url):
    h = urllib.parse.urlsplit(url).netloc.lower().split("@")[-1].split(":")[0]
    return h[4:] if h.startswith("www.") else h


class Ranker:
    """Scores urls for one crawl: sitemaps and OPR fetched once per host, shape per url."""

    def __init__(self, prefer=(), avoid=(), fetch=None, key=None, fetch_json=None, say=print,
                 port=None):
        self.prefer, self.avoid, self.say, self.port = prefer, avoid, say, port
        self.fetch = fetch or (lambda u: _default_fetch(u, port))
        self.key = key if key is not None else opr_key()
        self.fetch_json = fetch_json
        self.in_sitemap, self.opr, self.done_hosts = set(), {}, set()
        if not self.key:
            say("rank: no OpenPageRank key (secrets.yaml section `openpagerank`, field "
                "`password`): opr scores 0, sitemap + url-shape only")

    def prepare(self, origins):
        """Fetch sitemaps for each origin and OPR for their hosts. Returns the sitemap urls."""
        found = set()
        for o in origins:
            h = host_of(o)
            if h in self.done_hosts:
                continue
            self.done_hosts.add(h)
            urls, notes = sitemap_urls(o, self.fetch)
            self.say(f"rank: {h}: {len(urls)} sitemap url(s)" + (f" ({'; '.join(notes)})"
                                                                  if notes else ""))
            found |= urls
        self.in_sitemap |= found
        if self.key:
            self.opr.update(opr_ranks({host_of(o) for o in origins}, self.key, self.fetch_json,
                                      self.port))
            self.say(f"rank: OpenPageRank for {len(self.opr)} host(s)")
        return found

    def score(self, url):
        return ((1.0 if url in self.in_sitemap else 0.0) + shape_score(url, self.prefer, self.avoid)
                + self.opr.get(host_of(url), 0.0) / 10.0)
