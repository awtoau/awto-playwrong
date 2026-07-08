"""crawl.report — re-print the site-shape report from an existing crawl DB (no re-crawl).

    python -m crawl.report --db competitor.sqlite
    python -m crawl.report --db postgresql://u@host/mydb --schema crawl --top 25
"""
import argparse
import sys

from . import db, graph


def main(argv=None):
    p = argparse.ArgumentParser(prog="crawl.report", description="Print a crawl's site-shape report.")
    p.add_argument("--db", required=True, help="SQLite path or postgres:// URL")
    p.add_argument("--schema", default="crawl", help="Postgres schema (ignored for SQLite)")
    p.add_argument("--top", type=int, default=15, help="How many rows per section")
    a = p.parse_args(argv if argv is not None else sys.argv[1:])
    d = db.open_db(a.db, schema=a.schema)
    print(graph.summary_text(d, top=a.top))
    d.close()


if __name__ == "__main__":
    main()
