"""crawl.parse — pure HTML parsing: visible text, page links, image references.

Zero dependencies beyond the stdlib, zero project references. Deterministic, side-effect-free
functions. Consumers call these on the raw HTML the engine captured.
"""
import re
from urllib.parse import urljoin, urldefrag, urlsplit

# ── visible text ──────────────────────────────────────────────────────────────────────────────
_RE_SCRIPT = re.compile(r"<(script|style|noscript|template|svg)\b[^>]*>.*?</\1>", re.I | re.S)
_RE_COMMENT = re.compile(r"<!--.*?-->", re.S)
_RE_TAG = re.compile(r"<[^>]+>")
_RE_WS = re.compile(r"[ \t\r\f\v]+")
_RE_NL = re.compile(r"\n{3,}")
_RE_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def extract_text(html):
    """Best-effort visible-text extraction. Keeps line structure (block tags -> \\n) so downstream
    pattern-extraction has layout signal. Returns (title, text)."""
    m = _RE_TITLE.search(html)
    title = _RE_TAG.sub("", m.group(1)).strip()[:500] if m else ""
    s = _RE_COMMENT.sub(" ", html)
    s = _RE_SCRIPT.sub(" ", s)
    s = re.sub(r"</(p|div|li|tr|h[1-6]|section|article|header|footer|nav|br|ul|ol|table)\s*>",
               "\n", s, flags=re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = _RE_TAG.sub(" ", s)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&#39;", "'"), ("&apos;", "'")):
        s = s.replace(a, b)
    s = _RE_WS.sub(" ", s)
    s = "\n".join(ln.strip() for ln in s.split("\n"))
    s = _RE_NL.sub("\n\n", s).strip()
    return title, s


# ── page links ────────────────────────────────────────────────────────────────────────────────
_RE_HREF = re.compile(r'href\s*=\s*["\']([^"\'#][^"\']*)["\']', re.I)
_ASSET_EXT = (".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".woff",
              ".woff2", ".ttf", ".eot", ".pdf", ".zip", ".mp4", ".webm", ".mp3", ".xml", ".rss",
              ".json", ".txt", ".doc", ".docx", ".xls", ".xlsx", ".dmg", ".exe")
_FEED_MARKERS = ("format=feed", "type=rss", "type=atom", "/feed/", "/feed?", "wp-json", "/rss",
                 "/atom", "?rest_route=")
# Infrastructure / non-content endpoints — hrefs that appear on pages (esp. WordPress) but are RPC/
# admin/auth endpoints, not crawlable content. Skipping these avoids wasted fetches + junk.
_INFRA_MARKERS = ("/xmlrpc.php", "/wp-admin", "/wp-login", "/wp-cron.php", "/wp-json", "/wp-content/",
                  "/wp-includes/", "/cgi-bin/", "/.well-known/", "?replytocom=", "/trackback")


def is_page_url(u):
    """True if u looks like a crawlable HTML page (not an asset file or a feed)."""
    low = u.lower()
    path = urlsplit(low).path
    if path.endswith(_ASSET_EXT):
        return False
    # feeds: markers anywhere, OR a path ending in /feed or /comments/feed (WP/RSS convention)
    if any(m in low for m in _FEED_MARKERS):
        return False
    if path.rstrip("/").endswith(("/feed", "/comments/feed", "/rss", "/atom")):
        return False
    # infrastructure/RPC/admin endpoints (WordPress xmlrpc, wp-admin, wp-login, …) — not content
    if any(m in low for m in _INFRA_MARKERS):
        return False
    return True


def extract_links(html, base_url):
    """Absolute, de-fragmented PAGE links in the HTML. Skips asset files + feeds (they're hrefs but
    not crawlable pages — navigating to a favicon/css just times out)."""
    out, seen = [], set()
    for href in _RE_HREF.findall(html):
        if href.lower().startswith(("mailto:", "tel:", "javascript:", "data:")):
            continue
        absu = urldefrag(urljoin(base_url, href))[0]
        if not absu.startswith(("http://", "https://")):
            continue
        if not is_page_url(absu):
            continue
        if absu not in seen:
            seen.add(absu); out.append(absu)
    return out


# ── image references (no bytes; just what the page links to) ─────────────────────────────────
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.I)


def _img_attr(tag, name):
    m = re.search(rf'{name}\s*=\s*["\']([^"\']*)["\']', tag, re.I)
    return m.group(1) if m else None


def image_kind(url, alt):
    """Classify an image by url+alt so consumers can prioritise (maps/panoramas are usually the
    prize; logos/icons usually noise). Returns: piste_map|lift_map|map|panorama|logo|photo."""
    s = f"{url} {alt}".lower()
    if re.search(r"piste|trail.?map|slope.?map|ski.?map|plan.?des.?pistes|pistenplan", s):
        return "piste_map"
    if re.search(r"lift.?map|aerialway|liftplan", s):
        return "lift_map"
    if re.search(r"\bmap\b|karte|plan\b", s):
        return "map"
    if re.search(r"panorama|3d|bird.?eye", s):
        return "panorama"
    if re.search(r"logo|icon|sprite|favicon|badge", s):
        return "logo"
    return "photo"


def extract_images(html, base_url):
    """Yield (img_url, alt, kind) for every <img> in the HTML (absolute urls, data: skipped)."""
    seen = set()
    for tag in _IMG_TAG_RE.findall(html):
        src = _img_attr(tag, "src") or _img_attr(tag, "data-src") or _img_attr(tag, "data-lazy-src")
        if not src or src.startswith("data:"):
            continue
        img_url = urljoin(base_url or "", src)
        if img_url in seen:
            continue
        seen.add(img_url)
        alt = _img_attr(tag, "alt") or ""
        yield img_url, alt, image_kind(img_url, alt)
