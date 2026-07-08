"""assets.classify — decide whether bytes are a real asset worth keeping, and name the file.

Pure, stdlib-only, zero project refs. Consumers call these before/around store_asset to skip
ad-beacons, tracking pixels and junk, and to pick a file extension from the MIME type.
"""
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

# MIME -> file extension. Covers the image types a web crawl actually meets; extend per consumer.
EXT_BY_MIME = {
    "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png", "image/webp": "webp",
    "image/gif": "gif", "image/avif": "avif", "image/svg+xml": "svg",
    "image/x-icon": "ico", "image/vnd.microsoft.icon": "ico", "image/bmp": "bmp",
    "image/tiff": "tiff", "application/pdf": "pdf",
}

# URL substrings that mark a request as an ad / analytics / tracker beacon — never a content asset.
JUNK_URL_MARKERS = ("ad.php", "/ads/", "doubleclick", "googlesyndication", "google-analytics",
                    "/pixel", "/beacon", "/imp?", "quantserve", "scorecardresearch", "/telemetry")


def ext_for_mime(mime, default="bin"):
    """File extension for a Content-Type (params like '; charset' stripped)."""
    return EXT_BY_MIME.get((mime or "").split(";")[0].strip().lower(), default)


def is_junk_url(url):
    """True if the URL looks like an ad/tracker beacon rather than a content asset."""
    return any(m in (url or "").lower() for m in JUNK_URL_MARKERS)


def is_tracking_pixel(width, height):
    """True for 1x1 / 2x2 spacer or tracking pixels (once dimensions are known)."""
    return (width is not None and width <= 2) and (height is not None and height <= 2)


def canonical_url(url, drop_params=("_phcb",)):
    """Strip cache-bust / churn query params so the same asset gets ONE canonical src_url.
    `drop_params` defaults to the ('_phcb',) cache-buster used to force re-requests under CDP."""
    if not url:
        return url
    try:
        sp = urlsplit(url)
        q = [(k, v) for k, v in parse_qsl(sp.query, keep_blank_values=True) if k not in drop_params]
        return urlunsplit((sp.scheme, sp.netloc, sp.path, urlencode(q), ""))
    except Exception:
        out = url
        for p in drop_params:
            out = out.split(f"?{p}=")[0].split(f"&{p}=")[0]
        return out
