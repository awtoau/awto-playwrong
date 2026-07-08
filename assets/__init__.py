"""awto-playwrong assets — reusable, content-addressed binary asset store.

Companion to `crawl/`: where `crawl.store` content-addresses PAGES (compressed HTML text), this package
content-addresses ASSETS (images and other binaries fetched from those pages). Same discipline —
sha256 identity, sharded on-disk layout, dedup, ZERO project references.

The engine owns the MECHANICS:
  • store      — hash bytes -> sharded path, write once, dedup; describe an asset (StoredAsset)
  • imgmeta    — best-effort image metadata (dimensions, format, perceptual hash, EXIF, is-photo)
  • classify   — decide if bytes are worth keeping (junk/tracker/1x1-pixel filters), MIME -> ext

The CONSUMER owns the domain glue — the database table, the `connect()`, which rows to write, how
assets relate to pages. `store_asset()` returns a plain dataclass; the consumer INSERTs it wherever it
likes. See `schema.sql` for a recommended relational model a consumer can adopt as-is.

Nothing here knows what the assets are OF or where their rows live. Reuse it for any crawl.
"""

from . import store, classify, imgmeta  # noqa: F401

__all__ = ["store", "classify", "imgmeta"]
