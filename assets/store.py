"""assets.store — content-addressed binary asset store (sha256 identity, sharded, dedup). No DB.

Mirrors crawl.store (which does this for page text) but for arbitrary binary assets — images first.
sha256(bytes) is the identity: the same bytes hash to the same path, so re-crawling is idempotent and
storage is automatically deduped. Layout: <root>/<aa>/<bb>/<sha>.<ext>  (fan-out on the first 2 bytes
of the hash keeps any one directory small).

`store_asset()` writes the bytes ONCE and returns a StoredAsset describing what to record — it does
NOT touch a database. The consumer decides whether it's a new asset (check the sha against its own
table first) and INSERTs the StoredAsset's fields wherever it likes. See schema.sql for a model.

Typical consumer flow:
    from assets import store, classify, imgmeta
    if classify.is_junk_url(src_url):            # ad/tracker beacon -> skip
        return
    meta = imgmeta.probe(data)                   # dims/format/phash (best-effort)
    if classify.is_tracking_pixel(meta.width, meta.height):
        return
    if consumer_already_has(sha256(data)):       # dedup against the consumer's own table
        return
    sa = store.store_asset(data, root, mime=content_type, src_url=src_url, meta=meta)
    consumer_insert_row(sa)                       # write sa.* into your asset table
"""
import hashlib
import os
from dataclasses import dataclass, field

from . import classify as _classify


@dataclass
class StoredAsset:
    """Everything the consumer needs to record one stored asset. Pure data — no DB, no project refs."""
    sha256: str
    rel_path: str                     # path under `root`, forward-slashed: 'aa/bb/<sha>.<ext>'
    ext: str
    bytes: int
    src_url: str | None = None        # canonicalised (cache-bust params stripped)
    content_type: str | None = None
    media_kind: str = "image"
    width: int | None = None
    height: int | None = None
    img_format: str | None = None
    img_mode: str | None = None
    phash: str | None = None
    is_probably_photo: bool | None = None
    exif: dict = field(default_factory=dict)
    already_on_disk: bool = False     # True if these exact bytes were already stored (dedup)


def shard_rel(sha, ext):
    """Forward-slashed relative path for a sha: 'aa/bb/<sha>.<ext>'. Portable across OSes."""
    return f"{sha[:2]}/{sha[2:4]}/{sha}.{ext}"


def shard_path(root, sha, ext):
    """Absolute on-disk path for a sha under `root`."""
    return os.path.join(root, sha[:2], sha[2:4], f"{sha}.{ext}")


def store_asset(data, root, mime=None, src_url=None, media_kind="image", meta=None,
                drop_params=("_phcb",)):
    """Hash `data`, write it once under `root` (sharded), and return a StoredAsset. Idempotent: if the
    file already exists (same bytes) it is NOT rewritten and `already_on_disk` is True. Does NOT dedup
    against any database — the consumer checks the returned sha against its own table.

    `meta` is an optional assets.imgmeta.ImageMeta (dimensions/format/phash/exif); pass it to enrich
    the row. Raises nothing you wouldn't expect from a normal file write."""
    sha = hashlib.sha256(data).hexdigest()
    ext = _classify.ext_for_mime(mime)
    rel = shard_rel(sha, ext)
    full = shard_path(root, sha, ext)
    existed = os.path.exists(full)
    if not existed:
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(data)
    sa = StoredAsset(
        sha256=sha, rel_path=rel, ext=ext, bytes=len(data),
        src_url=_classify.canonical_url(src_url, drop_params) if src_url else None,
        content_type=mime, media_kind=media_kind, already_on_disk=existed,
    )
    if meta is not None:
        sa.width, sa.height = meta.width, meta.height
        sa.img_format, sa.img_mode = meta.img_format, meta.img_mode
        sa.phash, sa.is_probably_photo, sa.exif = meta.phash, meta.is_probably_photo, meta.exif
    return sa


def load_asset(root, sha, ext):
    """Read stored bytes back for a sha/ext, or None if absent."""
    p = shard_path(root, sha, ext)
    if not os.path.exists(p):
        return None
    with open(p, "rb") as f:
        return f.read()
