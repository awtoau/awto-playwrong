"""awto-playwrong crawl engine — reusable, domain-agnostic web crawling.

A thin library of crawl MECHANICS with ZERO project references — no application names, no database,
no schema names. Each consumer application provides its own domain glue — which URLs to seed, where
to store results, how to record links — via callbacks and by owning its own database. The engine owns:

  • browser        — attach to the shared awto-playwrong headed Chrome (or drive it via HTTP)
  • challenge       — detect/clear Cloudflare "verify you are human" walls; spot soft-404s
  • netblock       — CDP Fetch resource-type blocking (text-only crawl, tracker-block)
  • render         — consent-dismiss + wait-for-content for JS-SPA pages
  • parse          — extract clean text / page links / image references from HTML
  • store          — content-addressed sha page store (zstd, sharded) on disk
  • db             — optional relational store: SQLite (a file) OR Postgres, one interface
  • graph          — reports over the page→page reference graph (hubs/orphans/dead links)
  • run            — self-contained point-and-go crawler built from the parts (CLI + crawl())
  • report         — re-print a crawl's site-shape report from its DB (no re-crawl)
  • drive          — hand-drive the shared browser (click/scroll/screenshot/step), settle w/o sleep

Nothing here knows what the pages are ABOUT or where they get stored. Reuse it for any crawl. The
sibling `assets/` package is the matching content-addressed store for image/binary bytes. `schema.sql`
at the repo root is the recommended relational model both `db` and a consumer can adopt as-is.

`parse`, `store`, `db`, `graph` are pure/stdlib and safe to import alone. `browser`, `netblock`,
`render`, `run` pull in nodriver (a live browser) — import those only when actually crawling.
"""

from . import parse, store  # noqa: F401  (pure, always safe)

__all__ = ["parse", "store", "netblock", "render", "browser", "challenge",
           "db", "graph", "run", "report", "drive"]


def __getattr__(name):
    # Lazy so `from crawl import parse` (pure) never triggers the nodriver import chain.
    if name in ("netblock", "render", "browser", "challenge", "run",
                "db", "graph", "report", "drive"):
        import importlib
        return importlib.import_module(f".{name}", __name__)
    raise AttributeError(name)
