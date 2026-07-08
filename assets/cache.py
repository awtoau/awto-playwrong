"""assets.cache — recover image bytes from Chrome's on-disk Simple Cache. ZERO re-fetch.

The shared playwrong Chrome downloads every image it renders (when images aren't network-blocked) and
keeps the response body in its Simple Cache under `<profile>/Default/Cache/Cache_Data/<hash>_0`. This
module walks those entry files, extracts the original image bytes, and hands them to a caller-supplied
sink (typically assets.store.store_asset) — no network, faithful original bytes (correct sha).

Ported from powderhounds/scripts/recover_cache_images.py (which reverse-engineered the on-disk format).
Zero project refs — pass a url_filter to scope to your sites, and a sink to store however you like.

Simple Cache _0 entry layout (verified by byte-dumping v5 entries):
  [SimpleFileHeader 24B: magic u64 | version u32 | key_length u32 | key_hash u32 | pad u32]
  [key: "1/0/_dk_<origin-triple> <REQUEST-URL>"   (key_length bytes)]
  [stream 0 = the response BODY (the image)]        <- what we want, right after the key
  [SimpleFileEOF 24B for stream 0]                  <- body ends at the FIRST EOF magic
  ...
So body = bytes from key_end up to the FIRST SimpleFileEOF record. (Reading the last EOF's stream_size
gives the HEADER length, not the image — that was the historical bug.)

Usage:
    from assets import cache, store, imgmeta
    def sink(url, data, mime):
        m = imgmeta.probe(data)
        sa = store.store_asset(data, ASSET_ROOT, mime=mime, src_url=url, meta=m)
        d.upsert_asset(sa); d.link_page_asset(page_sha_for(url), url, asset_sha=sa.sha256)
    stats = cache.harvest(sink, url_filter=lambda u: 'mysite.com' in u)

CLI:
    python -m assets.cache --profiles-glob '/tmp/uc_*' --root /path/assets --host mysite.com [--dry-run]
"""
from __future__ import annotations
import glob
import io
import os
import struct

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None

SIMPLE_MAGIC = 0xfcfb6d1ba7725c30                      # SimpleFileHeader.initial_magic_number
EOF_MAGIC_LE = struct.pack("<Q", 0xf4fa6f45970d41d8)   # SimpleFileEOF.final_magic (LE)
HEADER_FMT = "<QIII I"                                  # magic u64|version u32|key_len u32|key_hash u32|pad u32
HEADER_SIZE = 24
IMG_EXT_HINT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg")
# image magic bytes — so we recover even images served without a file extension in the URL.
_SIGS = (
    (b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),  # RIFF....WEBP
    (b"BM", "image/bmp"),
)


def parse_entry(path):
    """Return (request_url, body_bytes) for a Simple Cache _0 entry, or None if not parseable.
    body = stream 0 = bytes from key_end up to the FIRST SimpleFileEOF record."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except Exception:
        return None
    if len(data) < HEADER_SIZE + 24:
        return None
    magic, _version, key_len, _key_hash, _pad = struct.unpack(HEADER_FMT, data[:HEADER_SIZE])
    if magic != SIMPLE_MAGIC:
        return None
    key_end = HEADER_SIZE + key_len
    if key_end > len(data):
        return None
    key = data[HEADER_SIZE:key_end].decode("latin-1", "ignore")
    url = key.rsplit(" ", 1)[-1] if " " in key else key   # request URL is the last space-token
    eof = data.find(EOF_MAGIC_LE, key_end)
    if eof <= key_end:
        return url, None
    return url, data[key_end:eof]


def sniff_mime(url, body):
    """Best-effort mime: magic bytes first (authoritative), else the URL extension."""
    if body:
        head = body[:16]
        for sig, mime in _SIGS:
            if head.startswith(sig):
                if sig == b"RIFF" and body[8:12] != b"WEBP":
                    continue
                return mime
    low = url.lower().split("?")[0]
    for ext in IMG_EXT_HINT:
        if low.endswith(ext):
            return "image/" + ("jpeg" if ext in (".jpg", ".jpeg") else ext.lstrip("."))
    return None


def is_image(url, body):
    """True if body looks like a real, decodable image. Uses magic bytes + (if PIL present) verify()."""
    if not body:
        return False
    if sniff_mime(url, body) is None:
        return False
    if Image is None:
        return True   # trust the magic bytes if PIL isn't available
    try:
        Image.open(io.BytesIO(body)).verify()
        return True
    except Exception:
        return False


def iter_cached_images(profiles_glob="/tmp/uc_*", url_filter=None, junk_markers=()):
    """Yield (url, body, mime) for every decodable image in the Chrome caches matched by profiles_glob.
    url_filter(url)->bool scopes to your sites (default: all). junk_markers filters obvious non-content
    (e.g. sprites/pixels). Dedups by URL within the run."""
    entry_files = []
    for prof in sorted(glob.glob(profiles_glob)):
        cache = os.path.join(prof, "Default", "Cache", "Cache_Data")
        entry_files += glob.glob(os.path.join(cache, "*_0"))
    seen = set()
    for path in entry_files:
        parsed = parse_entry(path)
        if not parsed:
            continue
        url, body = parsed
        if not url or url in seen:
            continue
        if url_filter is not None and not url_filter(url):
            continue
        low = url.lower()
        if any(m in low for m in junk_markers):
            continue
        if not is_image(url, body):
            continue
        seen.add(url)
        yield url, body, sniff_mime(url, body)


def harvest(sink, profiles_glob="/tmp/uc_*", url_filter=None, junk_markers=(), limit=None):
    """Walk the caches and call sink(url, data, mime) for each recovered image. Returns a stats dict.
    sink does the storing (e.g. assets.store.store_asset + your DB upsert). Never re-fetches anything."""
    n = stored = 0
    for url, body, mime in iter_cached_images(profiles_glob, url_filter, junk_markers):
        if limit and n >= limit:
            break
        n += 1
        try:
            sink(url, body, mime)
            stored += 1
        except Exception:
            pass
    return {"recovered": n, "stored": stored}


def _main(argv=None):
    import argparse
    from . import store as _store, imgmeta as _imgmeta
    ap = argparse.ArgumentParser(description="Recover images from Chrome's Simple Cache into an asset store.")
    ap.add_argument("--profiles-glob", default="/tmp/uc_*")
    ap.add_argument("--root", required=True, help="asset store root dir")
    ap.add_argument("--host", action="append", default=[], help="only URLs containing this host (repeatable)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    hosts = a.host
    uf = (lambda u: any(h in u for h in hosts)) if hosts else None
    if a.dry_run:
        n = sum(1 for _ in iter_cached_images(a.profiles_glob, uf))
        print(f"dry-run: {n} recoverable images"
              + (f" for hosts {hosts}" if hosts else ""))
        return
    def sink(url, data, mime):
        m = _imgmeta.probe(data)
        _store.store_asset(data, a.root, mime=mime, src_url=url, meta=m)
    stats = harvest(sink, a.profiles_glob, uf, limit=a.limit)
    print(f"harvested {stats['stored']}/{stats['recovered']} images -> {a.root}")


if __name__ == "__main__":
    _main()
