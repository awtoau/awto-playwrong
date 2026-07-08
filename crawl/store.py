"""crawl.store — content-addressed page store on disk (zstd, sharded). Zero project refs.

The engine handles the FILESYSTEM side of storing a page: hash the raw bytes, zstd-compress, and
shard-write to <root>/<sha[0:2]>/<sha[2:4]>/<sha>.<ext>.zst. It returns a StoredPage with all the
metadata a consumer needs to record its OWN database row (sha, sizes, title, text, …). The engine
never touches a database.
"""
import hashlib
import os
from dataclasses import dataclass
from typing import Optional

import zstandard

from . import parse

_ZC = zstandard.ZstdCompressor(level=19)   # high ratio; page bodies compress ~5x
_ZD = zstandard.ZstdDecompressor()


@dataclass
class StoredPage:
    sha256: str
    rel_path: str          # relative shard path, e.g. "aa/bb/<sha>.html.zst"
    full_path: str         # absolute path written
    ext: str
    raw_bytes: int
    stored_bytes: int
    title: str
    text: str
    text_chars: int
    text_sha: Optional[str]
    written: bool          # False if the file already existed (dedup by content)


def shard_path(root: str, sha: str, ext: str):
    """(rel, full) content-addressed shard path under `root`."""
    rel = os.path.join(sha[:2], sha[2:4], f"{sha}.{ext}.zst")
    return rel, os.path.join(root, "pages", rel)


def store_page(html: str, root: str, ext: str = "html") -> StoredPage:
    """Hash + compress + shard-write ONE page body. Idempotent on content (same bytes -> same sha ->
    file left as-is). Extracts title+text for the consumer's row. No DB — caller records that."""
    raw = html.encode("utf-8", "replace")
    sha = hashlib.sha256(raw).hexdigest()
    title, text = parse.extract_text(html)
    text_sha = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest() if text else None
    rel, full = shard_path(root, sha, ext)
    comp = _ZC.compress(raw)
    written = False
    if not os.path.exists(full):
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(comp)
        written = True
    return StoredPage(sha256=sha, rel_path=rel, full_path=full, ext=ext,
                      raw_bytes=len(raw), stored_bytes=len(comp), title=title,
                      text=text, text_chars=len(text), text_sha=text_sha, written=written)


def load_page(root: str, sha: str, ext: str = "html") -> Optional[str]:
    """Read a stored page back to HTML (decompress). None if missing/corrupt."""
    _, full = shard_path(root, sha, ext)
    if not os.path.exists(full):
        return None
    try:
        return _ZD.decompress(open(full, "rb").read()).decode("utf-8", "replace")
    except Exception:
        return None
