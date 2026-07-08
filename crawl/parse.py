"""crawl.parse — pure HTML parsing: visible text, page links, image references.

Zero dependencies beyond the stdlib, zero project references. Deterministic, side-effect-free
functions. Consumers call these on the raw HTML the engine captured.
"""
import html as _html
import re
from urllib.parse import urljoin, urldefrag, urlsplit

# ── visible text ──────────────────────────────────────────────────────────────────────────────
# One open-tag + one close-tag pattern PER stripped element (no backreference, no `.*?` spanning the
# whole element) — the backreference form `<(script|…)\b[^>]*>.*?</\1>` backtracks catastrophically
# on many unclosed <script> tags (a single crafted/broken page could hang the whole crawl). We instead
# blank each element by scanning open->close spans in one linear pass (_strip_elements).
_STRIP_TAGS = ("script", "style", "noscript", "template", "svg")
_RE_COMMENT = re.compile(r"<!--.*?-->", re.S)
_RE_TAG = re.compile(r"<[^>]+>")
_RE_WS = re.compile(r"[ \t\r\f\v]+")
_RE_NL = re.compile(r"\n{3,}")
_RE_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_RE_OPEN = {t: re.compile(rf"<{t}\b", re.I) for t in _STRIP_TAGS}
_RE_CLOSE = {t: re.compile(rf"</{t}\s*>", re.I) for t in _STRIP_TAGS}


def _strip_elements(s):
    """Remove <script>/<style>/… …</…> spans without a backtracking regex. Linear scan: find the
    next open tag, find its matching close, blank the span; an unclosed open tag blanks to EOF."""
    for t in _STRIP_TAGS:
        out = []
        pos = 0
        opn, cls = _RE_OPEN[t], _RE_CLOSE[t]
        while True:
            mo = opn.search(s, pos)
            if not mo:
                out.append(s[pos:])
                break
            out.append(s[pos:mo.start()])
            mc = cls.search(s, mo.end())
            if not mc:
                break                       # unclosed -> drop the rest
            pos = mc.end()
        s = "".join(out)
    return s


def extract_text(html):
    """Best-effort visible-text extraction. Keeps line structure (block tags -> \\n) so downstream
    pattern-extraction has layout signal. Returns (title, text). HTML entities are fully decoded via
    html.unescape (named + decimal + hex), not a hardcoded handful."""
    m = _RE_TITLE.search(html)
    title = _html.unescape(_RE_TAG.sub("", m.group(1))).strip()[:500] if m else ""
    s = _RE_COMMENT.sub(" ", html)
    s = _strip_elements(s)
    s = re.sub(r"</(p|div|li|tr|h[1-6]|section|article|header|footer|nav|br|ul|ol|table)\s*>",
               "\n", s, flags=re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = _RE_TAG.sub(" ", s)
    s = _html.unescape(s)                   # decode AFTER tag strip so entity `<` can't fake a tag
    s = _RE_WS.sub(" ", s)
    s = "\n".join(ln.strip() for ln in s.split("\n"))
    s = _RE_NL.sub("\n\n", s).strip()
    return title, s


# ── page links ────────────────────────────────────────────────────────────────────────────────
_RE_HREF = re.compile(r'href\s*=\s*["\']([^"\'#][^"\']*)["\']', re.I)
_ASSET_EXT = (".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".woff",
              ".woff2", ".ttf", ".eot", ".pdf", ".zip", ".mp4", ".webm", ".mp3", ".xml", ".rss",
              ".json", ".txt", ".doc", ".docx", ".xls", ".xlsx", ".dmg", ".exe")
# Query-string markers that unambiguously mark a feed/REST endpoint (distinctive enough to substring).
_FEED_QUERY_MARKERS = ("format=feed", "type=rss", "type=atom", "rest_route=")
# Path SEGMENTS that mark a feed OR a WordPress infra/RPC/admin endpoint (not content). Matched on
# whole path components (bounded by '/'), NOT bare substrings — else `/atomic-blog/`, `/rss-guide`,
# `/best-wp-json-tutorial/`, `/trackbacks-explained/` get wrongly dropped (silent content loss).
_INFRA_SEGMENTS = frozenset((
    "feed", "comments", "rss", "atom", "wp-json", "xmlrpc.php", "wp-admin", "wp-login.php",
    "wp-cron.php", "wp-content", "wp-includes", "cgi-bin", ".well-known", "trackback",
))
# Query markers whose mere PRESENCE (any value) marks a non-content endpoint.
_INFRA_QUERY_KEYS = ("replytocom",)


def is_page_url(u):
    """True if u looks like a crawlable HTML page (not an asset file, feed, or WP infra endpoint).
    Feed/infra matching is on whole PATH SEGMENTS (boundary-aware) so content slugs that merely
    contain 'atom'/'rss'/'wp-json' as a substring (e.g. /atomic-blog/) are NOT dropped."""
    sp = urlsplit(u.lower())
    path = sp.path
    if path.endswith(_ASSET_EXT):
        return False
    if any(m in sp.query for m in _FEED_QUERY_MARKERS):
        return False
    if any(k + "=" in sp.query for k in _INFRA_QUERY_KEYS):
        return False
    segments = set(path.split("/"))
    if segments & _INFRA_SEGMENTS:
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


# Ad / tracker beacon URL markers — an image whose URL matches is 'junk', never the prize (M8: an
# adspeed.net ad was previously classed 'photo' and could be prioritised). Kept minimal + generic.
_JUNK_IMG_MARKERS = ("doubleclick", "googlesyndication", "google-analytics", "adspeed", "/ads/",
                     "ad.php", "/pixel", "/beacon", "quantserve", "scorecardresearch")
# GENERIC kinds only — no vertical vocabulary baked in. A consumer passes `extra_rules` (an ordered
# list of (compiled_regex, kind)) to add domain-specific kinds (e.g. a ski consumer adds piste_map /
# lift_map). Order: junk -> consumer rules -> generic. `unhandled_cb(url, alt)` (optional) is called
# when nothing but the 'photo' fallback matched, so a consumer can log it for the improvement report.
_GENERIC_RULES = (
    (re.compile(r"\bmap\b|karte|\bplan\b"), "map"),
    (re.compile(r"panorama|bird.?eye|\b3d\b"), "panorama"),
    (re.compile(r"\blogo\b|\bicon\b|sprite|favicon|badge"), "logo"),
)


def image_kind(url, alt, extra_rules=None, unhandled_cb=None):
    """Classify an image by url+alt. Engine ships only GENERIC kinds (map|panorama|logo|photo|junk);
    a consumer supplies `extra_rules` [(regex, kind), …] for vertical kinds. Returns the kind; calls
    `unhandled_cb(url, alt)` (if given) when it falls through to the 'photo' default so the consumer can
    record it as an improvement signal."""
    low = url.lower()
    if any(m in low for m in _JUNK_IMG_MARKERS):
        return "junk"
    s = f"{url} {alt}".lower()
    for rx, kind in (extra_rules or ()):
        if rx.search(s):
            return kind
    for rx, kind in _GENERIC_RULES:
        if rx.search(s):
            return kind
    if unhandled_cb:
        try:
            unhandled_cb(url, alt)
        except Exception:
            pass
    return "photo"


def extract_images(html, base_url, extra_rules=None, unhandled_cb=None):
    """Yield (img_url, alt, kind) for every <img> in the HTML (absolute urls, data: skipped). HTML
    entities in src/alt are decoded so the same asset isn't seen under `&amp;` and `&` forms."""
    seen = set()
    for tag in _IMG_TAG_RE.findall(html):
        src = _img_attr(tag, "src") or _img_attr(tag, "data-src") or _img_attr(tag, "data-lazy-src")
        if not src or src.startswith("data:"):
            continue
        src = _html.unescape(src)
        img_url = urljoin(base_url or "", src)
        if img_url in seen:
            continue
        seen.add(img_url)
        alt = _html.unescape(_img_attr(tag, "alt") or "")
        yield img_url, alt, image_kind(img_url, alt, extra_rules, unhandled_cb)
