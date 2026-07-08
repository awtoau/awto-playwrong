-- ─────────────────────────────────────────────────────────────────────────────────────────────
--  awto-playwrong — RECOMMENDED relational model for a crawl consumer
-- ─────────────────────────────────────────────────────────────────────────────────────────────
--  The crawl/ and assets/ engine libraries store bytes on disk (content-addressed) and hand back
--  plain dataclasses. They do NOT own a database — each consumer keeps its own. This file is the
--  schema we RECOMMEND a consumer adopt: standardised page + asset metadata, a scan-status /
--  error-code vocabulary, and the frontier that drives the crawl. Copy it into your own schema
--  (rename the schema, add your domain columns) — an agent can use it as-is.
--
--  Design notes
--   • sha256 is the identity of stored bytes (pages AND assets). It's the join key to disk.
--   • rel_path is the on-disk shard path the engine returned ('aa/bb/<sha>.<ext>'); disk root lives
--     in consumer config, never in the DB, so the store can be relocated.
--   • Every fetchable thing carries a LAST-SCAN block: status enum + numeric error code + note +
--     timestamp. An agent reads scan_status to know what to retry, skip, or trust.
--   • Nothing here is project-specific. Add your own columns (country, resort_id, tags, audience)
--     on top; don't modify the standard block so tooling stays portable across consumers.
--
--  Put everything in one schema so a consumer can host several crawls side by side (e.g. one schema
--  per crawled site). Rename `crawl` below to taste.
-- ─────────────────────────────────────────────────────────────────────────────────────────────

CREATE SCHEMA IF NOT EXISTS crawl;

-- Standard scan-status vocabulary. Consumers store the last outcome of trying to fetch a URL.
--   ok            — fetched, rendered, captured cleanly
--   empty         — fetched but no usable content (stub / blank body after render)
--   nav_timeout   — navigation did not complete within the budget
--   http_error    — server returned a non-2xx (see scan_http_status)
--   blocked       — challenge / bot-wall / consent could not be cleared
--   render_error  — page loaded but the render/capture step threw
--   skipped       — deliberately not fetched (filtered: feed, infra endpoint, off-host, depth)
--   error         — anything else (see scan_note)
CREATE TYPE crawl.scan_status AS ENUM (
    'ok', 'empty', 'nav_timeout', 'http_error', 'blocked', 'render_error', 'skipped', 'error');


-- ── frontier ────────────────────────────────────────────────────────────────────────────────
-- The work list. One row per discovered URL; state drives the crawl loop. Keep the last-scan block
-- here so a re-crawl can decide (from status + tried_at) what to refetch without touching page/.
CREATE TABLE IF NOT EXISTS crawl.frontier (
    url               text PRIMARY KEY,
    state             text NOT NULL DEFAULT 'queued',   -- queued | fetching | done  (loop control)
    depth             int  NOT NULL DEFAULT 0,           -- link distance from a seed
    discovered_from   text,                              -- the page whose links surfaced this url
    first_seen        timestamptz NOT NULL DEFAULT now(),
    -- last-scan block (standard) ──────────────────────────────────────────────────────────────
    scan_status       crawl.scan_status,
    scan_http_status  int,                               -- HTTP code when known (e.g. 404, 503)
    scan_note         text,                              -- short human/agent-readable reason
    tried_at          timestamptz,                       -- when it was last attempted
    n_tries           int NOT NULL DEFAULT 0,            -- attempt count (for backoff / give-up)
    sha256            char(64)                           -- page captured on the last OK scan (FK below)
);
CREATE INDEX IF NOT EXISTS frontier_state_idx  ON crawl.frontier (state, depth);
CREATE INDEX IF NOT EXISTS frontier_status_idx ON crawl.frontier (scan_status);


-- ── page ────────────────────────────────────────────────────────────────────────────────────
-- One row per DISTINCT captured page (sha256 of the stored HTML). Content-addressed => a URL that
-- didn't change re-points at the same row; a URL that changed makes a new row. url here is the
-- canonical URL last seen for this sha (a page can be reachable by several URLs).
CREATE TABLE IF NOT EXISTS crawl.page (
    sha256        char(64) PRIMARY KEY,                  -- identity of the stored HTML bytes
    url           text NOT NULL,                         -- canonical URL last seen for these bytes
    title         text,
    rel_path      text NOT NULL,                         -- on-disk shard path the engine returned
    raw_bytes     int,                                   -- size of the captured HTML
    stored_bytes  int,                                   -- size on disk after compression
    text_chars    int,                                   -- extracted visible-text length (quality signal)
    lang          text,
    fetched_at    timestamptz NOT NULL DEFAULT now(),
    -- last-scan block (standard) — the scan that produced THIS capture
    scan_status   crawl.scan_status NOT NULL DEFAULT 'ok',
    scan_note     text
);
CREATE INDEX IF NOT EXISTS page_url_idx ON crawl.page (url);


-- ── page_link ───────────────────────────────────────────────────────────────────────────────
-- Directed page→page link graph (only crawlable page links; assets/feeds/infra already filtered).
CREATE TABLE IF NOT EXISTS crawl.page_link (
    page_sha  char(64) NOT NULL REFERENCES crawl.page(sha256) ON DELETE CASCADE,
    href      text NOT NULL,
    PRIMARY KEY (page_sha, href)
);


-- ── asset ───────────────────────────────────────────────────────────────────────────────────
-- One row per DISTINCT stored binary asset (sha256 of the bytes). Populated from assets.store's
-- StoredAsset. media_kind lets one table hold images + pdfs + others.
CREATE TABLE IF NOT EXISTS crawl.asset (
    sha256             char(64) PRIMARY KEY,
    src_url            text,                              -- canonical source URL (cache-bust stripped)
    media_kind         text NOT NULL DEFAULT 'image',
    rel_path           text NOT NULL,                     -- on-disk shard path
    content_type       text,
    ext                text,
    bytes              int,
    width              int,
    height             int,
    img_format         text,                              -- JPEG | PNG | WEBP | ...
    img_mode           text,                              -- RGB | RGBA | P | ...
    phash              text,                              -- perceptual hash (near-dup grouping)
    is_probably_photo  boolean,                           -- heuristic content-vs-chrome signal
    exif               jsonb,
    fetched_at         timestamptz NOT NULL DEFAULT now(),
    fetch_note         text
);
CREATE INDEX IF NOT EXISTS asset_phash_idx ON crawl.asset (phash);


-- ── page_asset ──────────────────────────────────────────────────────────────────────────────
-- Which assets a page references (the <img>/media refs parsed from its HTML). kind is the engine's
-- classification (piste_map | lift_map | map | panorama | logo | photo). sha256 is NULL until/unless
-- the asset bytes are actually fetched into crawl.asset — so this table doubles as the image FRONTIER.
CREATE TABLE IF NOT EXISTS crawl.page_asset (
    page_sha    char(64) NOT NULL REFERENCES crawl.page(sha256) ON DELETE CASCADE,
    img_url     text NOT NULL,
    alt         text,
    kind        text,                                    -- engine's parse.image_kind()
    asset_sha   char(64) REFERENCES crawl.asset(sha256), -- set once the bytes are stored
    PRIMARY KEY (page_sha, img_url)
);
CREATE INDEX IF NOT EXISTS page_asset_kind_idx ON crawl.page_asset (kind);
