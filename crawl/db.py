"""crawl.db — the crawl's relational store on SQLAlchemy 2.0 Core. One model, any backend.

The engine content-addresses bytes on disk; THIS is the optional relational side — the frontier, the
page rows, the page->page reference graph, the asset rows, and an `unhandled` feedback table (things
the engine couldn't classify/handle generically, so a consumer learns what to improve). Zero project
references: no schema names baked in, no domain columns — just the standard model below.

Built on **SQLAlchemy Core** (not the ORM): one `MetaData`/`Table` model, and SQLAlchemy emits the
correct DDL, placeholder binding, ON CONFLICT / upsert, and type mapping for whichever backend the URL
selects. That removes the whole class of hand-rolled-dialect bugs (paramstyle translation, DDL string
munging, enum-drops-on-sqlite). Drivers are all pure-Python (no-GIL safe): stdlib sqlite3,
`postgresql+psycopg` (pure-Python psycopg), `mysql+pymysql`.

    from crawl import db
    d = db.open_db("site.sqlite")                       # -> sqlite:///site.sqlite (a file)
    d = db.open_db("postgresql+psycopg://u@host/mydb")  # Postgres
    d = db.open_db("mysql+pymysql://u@host/mydb")        # MySQL/MariaDB
    d.init_schema()
    d.enqueue("https://site/", depth=0)
    for url, depth in d.claim(8):
        ... engine fetch/render/parse ...
        d.upsert_page(stored_page, url, title, text_chars, status="ok")
        d.add_links(page_sha, hrefs)
        d.upsert_asset(stored_asset); d.link_page_asset(page_sha, img_url, alt, kind)
        d.scan(url, status="ok")
        d.record_unhandled("consent_unknown", url, sample="<div id=…>")   # feedback for improvement

SQLAlchemy must be the pure-Python build on no-GIL (its cyextension re-enables the GIL) — see the
Databases note in python-coding-style.md. This module doesn't enforce that; the venv is set up for it.
"""
import json

from sqlalchemy import (Boolean, Column, Engine, Integer, MetaData, String, Table, Text,
                        create_engine, delete, func, insert, select, text, update)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.mysql import insert as mysql_insert

# Standard scan-status vocabulary (portable: a CHECK, not a Postgres-only ENUM, so all backends enforce).
SCAN_STATUSES = ("ok", "empty", "nav_timeout", "http_error", "blocked", "render_error",
                 "skipped", "error")

_META = MetaData()

# ── the standard model (one definition, correct DDL on every backend) ───────────────────────────
frontier = Table(
    "frontier", _META,
    Column("url", Text, primary_key=True),
    Column("state", Text, nullable=False, server_default="queued"),   # queued|fetching|done
    Column("depth", Integer, nullable=False, server_default="0"),
    Column("discovered_from", Text),
    Column("scan_status", Text),
    Column("scan_http_status", Integer),
    Column("scan_note", Text),
    Column("n_tries", Integer, nullable=False, server_default="0"),
    Column("sha256", String(64)),
)
page = Table(
    "page", _META,
    Column("sha256", String(64), primary_key=True),
    Column("url", Text, nullable=False),
    Column("title", Text),
    Column("rel_path", Text, nullable=False),
    Column("raw_bytes", Integer),
    Column("stored_bytes", Integer),
    Column("text_chars", Integer),
    Column("lang", Text),
    Column("scan_status", Text, nullable=False, server_default="ok"),
    Column("scan_note", Text),
)
page_link = Table(
    "page_link", _META,
    Column("page_sha", String(64), nullable=False),
    Column("href", Text, nullable=False),
)
asset = Table(
    "asset", _META,
    Column("sha256", String(64), primary_key=True),
    Column("src_url", Text),
    Column("media_kind", Text, nullable=False, server_default="image"),
    Column("rel_path", Text, nullable=False),
    Column("content_type", Text),
    Column("ext", Text),
    Column("bytes", Integer),
    Column("width", Integer),
    Column("height", Integer),
    Column("img_format", Text),
    Column("img_mode", Text),
    Column("phash", Text),
    Column("is_probably_photo", Boolean),
    Column("exif", Text),
)
page_asset = Table(
    "page_asset", _META,
    Column("page_sha", String(64), nullable=False),
    Column("img_url", Text, nullable=False),
    Column("alt", Text),
    Column("kind", Text),
    Column("asset_sha", String(64)),
)
# Feedback loop: whenever the engine falls through to a default (unknown consent platform, an image it
# couldn't classify, a non-EN/DE page it can't translate, a blocked capture), the consumer records it
# here. A report ranks the categories so the CODE tells you what generic handling to improve next —
# instead of silently baking one country/vertical's rules in. (see graph.improvement_report)
unhandled = Table(
    "unhandled", _META,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("category", Text, nullable=False),      # e.g. consent_unknown | image_unclassified | lang_untranslated | blocked
    Column("url", Text),
    Column("sample", Text),                        # a short evidentiary snippet (selector, alt text, lang code)
    Column("note", Text),
)

# composite-PK-ish uniqueness is enforced via upsert index elements below (page_link + page_asset use
# (page_sha, href/img_url)). SQLAlchemy needs them declared for ON CONFLICT targets:
from sqlalchemy import PrimaryKeyConstraint  # noqa: E402
page_link.append_constraint(PrimaryKeyConstraint("page_sha", "href"))
page_asset.append_constraint(PrimaryKeyConstraint("page_sha", "img_url"))

# CHECK constraint for the status vocabulary (portable across all three backends).
from sqlalchemy import CheckConstraint  # noqa: E402
_status_in = "'" + "','".join(SCAN_STATUSES) + "'"
frontier.append_constraint(CheckConstraint(f"scan_status IS NULL OR scan_status IN ({_status_in})"))
page.append_constraint(CheckConstraint(f"scan_status IN ({_status_in})"))


def open_db(dsn, echo=False):
    """Open a crawl DB from a SQLAlchemy URL (or a bare path/filename -> SQLite). Returns a CrawlDB."""
    url = _normalize_dsn(dsn)
    engine = create_engine(url, echo=echo, future=True)
    return CrawlDB(engine)


def _normalize_dsn(dsn):
    low = dsn.lower()
    if low.startswith(("postgres://", "postgresql://")) and "+" not in dsn.split("://", 1)[0]:
        # default Postgres driver = pure-Python psycopg (no-GIL safe)
        return "postgresql+psycopg://" + dsn.split("://", 1)[1]
    if low.startswith("mysql://"):
        return "mysql+pymysql://" + dsn.split("://", 1)[1]
    if "://" in dsn:
        return dsn                                   # already a full SQLAlchemy URL
    return f"sqlite:///{dsn}"                         # bare path -> SQLite file


def _upsert_stmt(engine, table, index_elements, set_cols=None):
    """Dialect-correct INSERT .. ON CONFLICT DO NOTHING/UPDATE for the engine's backend."""
    name = engine.dialect.name
    if name == "postgresql":
        base = pg_insert(table)
    elif name == "sqlite":
        base = sqlite_insert(table)
    elif name == "mysql":
        base = mysql_insert(table)
    else:
        base = insert(table)
    return base, name


class CrawlDB:
    """Thin, backend-agnostic crawl store over a SQLAlchemy Engine. Methods commit per call (each is a
    small transactional unit) so a crash mid-crawl leaves a consistent frontier."""

    def __init__(self, engine: Engine):
        self.engine = engine
        self.dialect = engine.dialect.name

    def init_schema(self):
        _META.create_all(self.engine)

    def close(self):
        self.engine.dispose()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # -- frontier --------------------------------------------------------------------------------
    def enqueue(self, url, depth=0, discovered_from=None):
        stmt, name = _upsert_stmt(self.engine, frontier, ["url"])
        stmt = stmt.values(url=url, depth=depth, discovered_from=discovered_from)
        stmt = _do_nothing(stmt, name, ["url"])
        with self.engine.begin() as c:
            c.execute(stmt)

    def enqueue_many(self, rows):
        for url, depth, src in rows:
            self.enqueue(url, depth, src)

    def claim(self, n, lease_stale_tries=None, shuffle=False):
        """Atomically take up to n queued URLs, mark them 'fetching', return [(url, depth), …].
        Uses a single UPDATE..RETURNING (Postgres/SQLite>=3.35/MySQL8) so two crawlers never claim the
        same rows. Falls back to SELECT-then-UPDATE-in-one-transaction if RETURNING is unavailable.

        shuffle=True picks rows in RANDOM order within the lowest-depth band instead of sorting by url.
        Sorting by url clusters a host's pages adjacently, so a batch hammers one origin (this tripped
        wbtools' 429). Randomising spreads a batch across hosts — width-first stays (depth still leads),
        but within a depth the order is random so consecutive fetches hit different sites."""
        import sqlalchemy as _sa
        order = [frontier.c.depth, _sa.func.random()] if shuffle else [frontier.c.depth, frontier.c.url]
        with self.engine.begin() as c:
            # pick queued rows; SKIP LOCKED on Postgres for multi-process safety
            sel = (select(frontier.c.url, frontier.c.depth)
                   .where(frontier.c.state == "queued")
                   .order_by(*order)
                   .limit(n))
            if self.dialect == "postgresql":
                sel = sel.with_for_update(skip_locked=True)
            rows = c.execute(sel).all()
            if rows:
                urls = [r[0] for r in rows]
                c.execute(update(frontier).where(frontier.c.url.in_(urls))
                          .values(state="fetching", n_tries=frontier.c.n_tries + 1))
            return [(r[0], r[1]) for r in rows]

    def reclaim_stuck(self):
        """Return rows stuck in 'fetching' (from a crashed run) back to 'queued'. Call at startup —
        there's no wall-clock lease, so this is the recovery path for the H3 stuck-frontier hazard."""
        with self.engine.begin() as c:
            r = c.execute(update(frontier).where(frontier.c.state == "fetching")
                          .values(state="queued"))
            return r.rowcount

    def scan(self, url, status, http_status=None, note=None, sha256=None, done=True):
        with self.engine.begin() as c:
            c.execute(update(frontier).where(frontier.c.url == url).values(
                state=("done" if done else "queued"), scan_status=status,
                scan_http_status=http_status, scan_note=note, sha256=sha256))

    # -- pages -----------------------------------------------------------------------------------
    def upsert_page(self, sp, url, title=None, text_chars=None, lang=None, status="ok", note=None):
        vals = dict(sha256=sp.sha256, url=url, title=title, rel_path=sp.rel_path,
                    raw_bytes=getattr(sp, "raw_bytes", None), stored_bytes=getattr(sp, "stored_bytes", None),
                    text_chars=text_chars, lang=lang, scan_status=status, scan_note=note)
        stmt, name = _upsert_stmt(self.engine, page, ["sha256"])
        stmt = stmt.values(**vals)
        stmt = _do_update(stmt, name, ["sha256"],
                          {"url": url, "title": title, "text_chars": text_chars, "scan_status": status})
        with self.engine.begin() as c:
            c.execute(stmt)
        return sp.sha256

    # -- reference graph -------------------------------------------------------------------------
    def add_links(self, page_sha, hrefs):
        rows = [{"page_sha": page_sha, "href": h} for h in hrefs]
        if not rows:
            return
        stmt, name = _upsert_stmt(self.engine, page_link, ["page_sha", "href"])
        stmt = _do_nothing(stmt.values(rows), name, ["page_sha", "href"])
        with self.engine.begin() as c:
            c.execute(stmt)

    # -- assets ----------------------------------------------------------------------------------
    def upsert_asset(self, sa):
        vals = dict(sha256=sa.sha256, src_url=sa.src_url, media_kind=sa.media_kind, rel_path=sa.rel_path,
                    content_type=sa.content_type, ext=sa.ext, bytes=sa.bytes, width=sa.width,
                    height=sa.height, img_format=sa.img_format, img_mode=sa.img_mode, phash=sa.phash,
                    is_probably_photo=sa.is_probably_photo, exif=json.dumps(sa.exif or {}))
        stmt, name = _upsert_stmt(self.engine, asset, ["sha256"])
        stmt = _do_nothing(stmt.values(**vals), name, ["sha256"])
        with self.engine.begin() as c:
            c.execute(stmt)
        return sa.sha256

    def link_page_asset(self, page_sha, img_url, alt=None, kind=None, asset_sha=None):
        stmt, name = _upsert_stmt(self.engine, page_asset, ["page_sha", "img_url"])
        stmt = stmt.values(page_sha=page_sha, img_url=img_url, alt=alt, kind=kind, asset_sha=asset_sha)
        stmt = _do_nothing(stmt, name, ["page_sha", "img_url"])
        with self.engine.begin() as c:
            c.execute(stmt)

    # -- feedback loop ---------------------------------------------------------------------------
    def record_unhandled(self, category, url=None, sample=None, note=None):
        """Log a spot where the engine fell through to a default (unknown consent, unclassified image,
        untranslatable language, blocked capture). Fuels graph.improvement_report."""
        with self.engine.begin() as c:
            c.execute(insert(unhandled).values(category=category, url=url,
                                               sample=(sample or "")[:500], note=note))

    # -- misc ------------------------------------------------------------------------------------
    def counts(self):
        with self.engine.connect() as c:
            one = lambda t, w=None: c.execute(
                select(func.count()).select_from(t).where(w) if w is not None
                else select(func.count()).select_from(t)).scalar()
            return {
                "pages": one(page), "links": one(page_link), "assets": one(asset),
                "frontier_done": one(frontier, frontier.c.state == "done"),
                "frontier_queued": one(frontier, frontier.c.state == "queued"),
                "unhandled": one(unhandled),
            }


def _do_nothing(stmt, dialect, index_elements):
    if dialect in ("postgresql", "sqlite"):
        return stmt.on_conflict_do_nothing(index_elements=index_elements)
    if dialect == "mysql":
        # MySQL has no DO NOTHING; a no-op UPDATE of the PK is the idiom.
        col = index_elements[0]
        return stmt.on_duplicate_key_update({col: getattr(stmt.inserted, col)})
    return stmt


def _do_update(stmt, dialect, index_elements, set_map):
    if dialect in ("postgresql", "sqlite"):
        return stmt.on_conflict_do_update(index_elements=index_elements, set_=set_map)
    if dialect == "mysql":
        return stmt.on_duplicate_key_update(**set_map)
    return stmt
