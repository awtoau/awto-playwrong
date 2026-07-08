"""assets.imgmeta — best-effort image metadata (dimensions, format, perceptual hash, EXIF).

Pillow + imagehash are OPTIONAL: if absent (or they choke on a weird file) every field comes back
None/empty and the caller stores the bytes anyway. Zero project refs. Consumers use the result to
enrich their asset row and to spot tracking pixels (via `is_probably_photo` / width+height).
"""
import io
from dataclasses import dataclass, field

try:
    from PIL import Image
    import imagehash
except Exception:                                   # Pillow optional — degrade gracefully
    Image = imagehash = None

# Drop C0 control chars (except tab/nl/cr) + NUL so EXIF strings are safe for Postgres text/jsonb —
# odd EXIF blobs carry raw binary that otherwise crashes the insert.
_CTRL = {c: None for c in range(32) if c not in (9, 10, 13)}
_CTRL[0] = None


def _scrub(s):
    return s.translate(_CTRL)


@dataclass
class ImageMeta:
    width: int | None = None
    height: int | None = None
    img_format: str | None = None      # 'JPEG' | 'PNG' | 'WEBP' | ...
    img_mode: str | None = None        # 'RGB' | 'RGBA' | 'P' | ...
    phash: str | None = None           # perceptual hash (near-dup detection); None if imagehash absent
    exif: dict = field(default_factory=dict)
    is_probably_photo: bool | None = None   # heuristic: >=200px each side, RGB(A) => a real photo


def probe(data):
    """Return an ImageMeta for the given image bytes. Never raises — all-None on any failure or if
    Pillow is unavailable."""
    m = ImageMeta()
    if not data or Image is None:
        return m
    try:
        im = Image.open(io.BytesIO(data))
        m.width, m.height = im.size
        m.img_format, m.img_mode = im.format, im.mode
        raw = getattr(im, "_getexif", lambda: None)()
        if raw:
            m.exif = {str(k): _scrub(str(v)[:200]) for k, v in raw.items()}
        try:
            m.phash = str(imagehash.phash(im)) if imagehash else None
        except Exception:
            m.phash = None
        m.is_probably_photo = (m.width >= 200 and m.height >= 200 and m.img_mode in ("RGB", "RGBA"))
    except Exception:
        return ImageMeta()
    return m
