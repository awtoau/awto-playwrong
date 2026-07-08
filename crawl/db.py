"""crawl.db — one storage interface, two backends: SQLite (a file, zero-config) or Postgres.

The engine content-addresses bytes on disk; THIS is the optional relational side — the frontier, the
page rows, the page->page reference graph, the asset rows. It exists so ripping a small site is a
one-liner (SQLite, no server) while the same code scales to Postgres for a big pipeline. Zero project
references: no schema names baked in, no domain columns — just the standard model in schema.sql.

    from crawl import db
    d = db.open_db("mysite.sqlite")                 # -> SQLite file
    d = db.open_db("postgresql://u@host/crawl")     # -> Postgres (needs psycopg)
    d.init_schema()                                  # idempotent DDL (portable)
    d.enqueue("https://site/", depth=0)
    for url, depth in d.claim(8):                    # take work
        ...                                          # (engine fetches/renders/parses)
        d.upsert_page(stored_page, url, title, text_chars, status="ok")
        d.add_links(page_sha, hrefs)                 # the reference graph
        d.upsert_asset(stored_asset)                 # from assets.store
        d.link_page_asset(page_sha, img_url, alt, kind)
        d.scan(url, status="ok")                     # record last-scan outcome

Backends share one SQL dialect surface: we use `?`-style params internally and translate to `%s` for
Postgres, and use INSERT .. ON CONFLICT (supported by both modern SQLite and Postgres). Consumers get
identical behaviour either way; `crawl.graph` runs its reports against whichever backend you opened.
"""
import os

# schema.sql lives at the engine root (one level above this package).
_SCHEMA_SQL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql")


def open_db(dsn, schema="crawl"):
    """Open a crawl DB. `dsn` is a SQLite path (…​.sqlite / …​.db / a bare path) OR a
    postgres:// / postgresql:// URL. `schema` is the Postgres schema name (ignored by SQLite, which
    has no schemas — table names are used bare). Returns a CrawlDB."""
    low = dsn.lower()
    if low.startswith(("postgres://", "postgresql://")):
        return _PostgresDB(dsn, schema)
    return _SqliteDB(dsn)


# ── shared SQL (written in the portable subset both engines accept) ─────────────────────────────
# {p} is the table-name prefix: '' for SQLite, 'crawl.' for Postgres. Params are '?'; Postgres
# translation swaps them to '%s'. now() works on Postgres; SQLite uses CURRENT_TIMESTAMP (patched).

class _CrawlDBBase:
    """Common query logic. Subclasses provide connect/execute/param-style/DDL specifics."""

    def __init__(self):
        self._p = ""            # table prefix (schema.)
        self._now = "now()"     # current-timestamp expression

    # -- lifecycle -------------------------------------------------------------------------------
    def init_schema(self):
        raise NotImplementedError

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # -- low-level (subclass-specific param style) ----------------------------------------------
    def _ex(self, sql, args=()):
        raise NotImplementedError

    def _q(self, sql, args=()):
        raise NotImplementedError

    def commit(self):
        self._conn.commit()

    # -- frontier --------------------------------------------------------------------------------
    def enqueue(self, url, depth=0, discovered_from=None):
        """Add a URL to the frontier if new (no-op if already known)."""
        self._ex(f"INSERT INTO {self._p}frontier (url, depth, discovered_from) VALUES (?,?,?) "
                 f"ON CONFLICT (url) DO NOTHING", (url, depth, discovered_from))

    def enqueue_many(self, rows):
        """rows: iterable of (url, depth, discovered_from). Batched enqueue."""
        for url, depth, src in rows:
            self.enqueue(url, depth, src)

    def claim(self, n):
        """Atomically take up to n queued URLs, mark them 'fetching', return [(url, depth), …]."""
        got = self._q(f"SELECT url, depth FROM {self._p}frontier WHERE state='queued' "
                      f"ORDER BY depth, url LIMIT ?", (n,))
        for url, _ in got:
            self._ex(f"UPDATE {self._p}frontier SET state='fetching', tried_at={self._now}, "
                     f"n_tries=n_tries+1 WHERE url=?", (url,))
        self.commit()
        return got

    def scan(self, url, status, http_status=None, note=None, sha256=None, done=True):
        """Record the last-scan outcome for a frontier URL (status enum + optional http code/note)."""
        state = "done" if done else "queued"
        self._ex(f"UPDATE {self._p}frontier SET state=?, scan_status=?, scan_http_status=?, "
                 f"scan_note=?, sha256=?, tried_at={self._now} WHERE url=?",
                 (state, status, http_status, note, sha256, url))
        self.commit()

    # -- pages -----------------------------------------------------------------------------------
    def upsert_page(self, sp, url, title=None, text_chars=None, lang=None, status="ok", note=None):
        """Insert/refresh a page row from an engine StoredPage (sp has sha256/rel_path/raw_bytes/
        stored_bytes). Content-addressed => re-crawl of unchanged bytes just refreshes url/fetched."""
        self._ex(
            f"INSERT INTO {self._p}page (sha256,url,title,rel_path,raw_bytes,stored_bytes,text_chars,"
            f"lang,scan_status,scan_note) VALUES (?,?,?,?,?,?,?,?,?,?) "
            f"ON CONFLICT (sha256) DO UPDATE SET url=excluded.url, title=excluded.title, "
            f"text_chars=excluded.text_chars, scan_status=excluded.scan_status",
            (sp.sha256, url, title, sp.rel_path, getattr(sp, "raw_bytes", None),
             getattr(sp, "stored_bytes", None), text_chars, lang, status, note))
        return sp.sha256

    # -- reference graph -------------------------------------------------------------------------
    def add_links(self, page_sha, hrefs):
        """Record the page->page reference edges for one page (the 'what references what' graph)."""
        for h in hrefs:
            self._ex(f"INSERT INTO {self._p}page_link (page_sha, href) VALUES (?,?) "
                     f"ON CONFLICT (page_sha, href) DO NOTHING", (page_sha, h))

    # -- assets ----------------------------------------------------------------------------------
    def upsert_asset(self, sa):
        """Insert an asset row from an assets.store StoredAsset (dedup on sha256)."""
        import json
        self._ex(
            f"INSERT INTO {self._p}asset (sha256,src_url,media_kind,rel_path,content_type,ext,bytes,"
            f"width,height,img_format,img_mode,phash,is_probably_photo,exif) "
            f"VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (sha256) DO NOTHING",
            (sa.sha256, sa.src_url, sa.media_kind, sa.rel_path, sa.content_type, sa.ext, sa.bytes,
             sa.width, sa.height, sa.img_format, sa.img_mode, sa.phash, sa.is_probably_photo,
             json.dumps(sa.exif or {})))
        return sa.sha256

    def link_page_asset(self, page_sha, img_url, alt=None, kind=None, asset_sha=None):
        """Record that a page references an image (the image frontier + page<->asset edge)."""
        self._ex(f"INSERT INTO {self._p}page_asset (page_sha,img_url,alt,kind,asset_sha) "
                 f"VALUES (?,?,?,?,?) ON CONFLICT (page_sha, img_url) DO UPDATE SET "
                 f"asset_sha=COALESCE(excluded.asset_sha, {self._p}page_asset.asset_sha)",
                 (page_sha, img_url, alt, kind, asset_sha))

    # -- misc ------------------------------------------------------------------------------------
    def counts(self):
        """Quick {pages, links, assets, frontier_done, frontier_queued} snapshot."""
        one = lambda s: (self._q(s) or [[0]])[0][0]
        return {
            "pages": one(f"SELECT count(*) FROM {self._p}page"),
            "links": one(f"SELECT count(*) FROM {self._p}page_link"),
            "assets": one(f"SELECT count(*) FROM {self._p}asset"),
            "frontier_done": one(f"SELECT count(*) FROM {self._p}frontier WHERE state='done'"),
            "frontier_queued": one(f"SELECT count(*) FROM {self._p}frontier WHERE state='queued'"),
        }


# ── SQLite backend ──────────────────────────────────────────────────────────────────────────
class _SqliteDB(_CrawlDBBase):
    def __init__(self, path):
        super().__init__()
        import sqlite3
        self._sqlite3 = sqlite3
        self._conn = sqlite3.connect(path)
        self._conn.execute("PRAGMA journal_mode=WAL")   # concurrent readers during a crawl
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._p = ""
        self._now = "CURRENT_TIMESTAMP"

    def _ex(self, sql, args=()):
        self._conn.execute(sql, args)

    def _q(self, sql, args=()):
        return [tuple(r) for r in self._conn.execute(sql, args).fetchall()]

    def init_schema(self):
        with open(_SCHEMA_SQL, encoding="utf-8") as f:
            portable = _to_sqlite_ddl(f.read())
        self._conn.executescript(portable)
        self._conn.commit()


# ── Postgres backend ──────────────────────────────────────────────────────────────────────────
class _PostgresDB(_CrawlDBBase):
    def __init__(self, dsn, schema="crawl"):
        super().__init__()
        import psycopg
        # On free-threaded (no-GIL) Python the C/binary psycopg impl is broken — require pure-Python.
        if getattr(psycopg.pq, "__impl__", "python") != "python":
            raise RuntimeError("crawl.db needs the pure-Python psycopg impl (psycopg.pq.__impl__="
                               f"{psycopg.pq.__impl__!r}); the C/binary impl breaks under no-GIL. "
                               "Install plain `psycopg`, not psycopg[binary]/[c].")
        self._conn = psycopg.connect(dsn)
        self._schema = schema
        self._p = f"{schema}."
        self._now = "now()"

    def _sub(self, sql):
        return sql.replace("?", "%s")

    def _ex(self, sql, args=()):
        self._conn.execute(self._sub(sql), args)

    def _q(self, sql, args=()):
        return self._conn.execute(self._sub(sql), args).fetchall()

    def init_schema(self):
        with open(_SCHEMA_SQL, encoding="utf-8") as f:
            ddl = f.read().replace("CREATE SCHEMA IF NOT EXISTS crawl", f"CREATE SCHEMA IF NOT EXISTS {self._schema}")
            ddl = ddl.replace("crawl.", f"{self._schema}.")
        self._conn.execute(ddl)
        self._conn.commit()


def _to_sqlite_ddl(sql):
    """Translate the Postgres schema.sql to the SQLite-accepted subset:
      • drop the `CREATE SCHEMA` + the ENUM type (SQLite has neither) — scan_status becomes TEXT
      • strip the `crawl.` schema prefix (SQLite tables are bare)
      • timestamptz/char(N)/jsonb -> TEXT ; now() default handled at insert time
    SQLite is forgiving on unknown type names, so most columns pass through untouched."""
    import re
    # strip line comments first (both full-line and trailing '-- …'); they'd survive the ';' split.
    sql = re.sub(r"--[^\n]*", "", sql)
    out = []
    for stmt in sql.split(";"):
        s = stmt.strip()
        if not s:
            continue
        low = s.lower()
        if low.startswith("create schema") or low.startswith("create type"):
            continue                                    # no schemas / no enums in SQLite
        s = s.replace("crawl.", "")
        s = re.sub(r"\bcrawl\.scan_status\b", "TEXT", s)
        s = re.sub(r"\btimestamptz\b", "TEXT", s, flags=re.I)
        s = re.sub(r"\bjsonb\b", "TEXT", s, flags=re.I)
        s = re.sub(r"\bchar\(\d+\)\b", "TEXT", s, flags=re.I)
        s = re.sub(r"scan_status\s+scan_status", "scan_status TEXT", s, flags=re.I)
        s = re.sub(r"\bnow\(\)", "CURRENT_TIMESTAMP", s, flags=re.I)   # SQLite has no now()
        out.append(s + ";")
    return "\n".join(out)
