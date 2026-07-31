"""cli.py — the `playwrong` command. One url in, readable page out. Nothing to start first.

    playwrong https://awto.au                 # text of the page
    playwrong https://a.com https://b.com     # several, reusing the SAME warm browser
    playwrong https://awto.au --links         # keep hrefs, so you can pick the next url
    playwrong https://awto.au --html          # raw markup
    playwrong https://awto.au --shot page.png # also save a screenshot
    playwrong https://awto.au --json          # {text,title,url,challenge} for scripting
    playwrong --status                        # is anything running?
    playwrong --stop                          # stop the shared engine (affects everyone)

**You do not manage a host:port.** The engine is a long-lived local server on a fixed default port
(PH_PORT, 8731). The first command starts it and launches Chrome; every later command finds it
already up and reuses it — same browser, same cookies, same cleared Turnstile session, no cold start.
That is the entire session model: fire one command per url, whenever, and they compound. Pass --port
only when you deliberately want a SECOND, isolated browser.

Progress notes go to stderr and the page to stdout, so `playwrong <url> > page.txt` stays clean.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine import connect                                                   # noqa: E402


def _note(msg):
    print(f"… {msg}", file=sys.stderr, flush=True)


def _shot_path(path, i, n):
    """One url -> the name you gave. Several -> name-0.png, name-1.png, so they don't overwrite."""
    if n == 1:
        return path
    stem, ext = os.path.splitext(path)
    return f"{stem}-{i}{ext or '.png'}"


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="playwrong",
        description="Fetch web pages through a real headed Chrome that beats Cloudflare Turnstile. "
                    "The browser starts itself and stays warm between commands.",
        epilog="Interactive driving (click/key/js/tabs) lives in engine/client.py; the MCP server "
               "for agents is in engine/mcp_server.py (see docs/MCP.md).")
    p.add_argument("urls", nargs="*", help="one or more urls to fetch, in order")
    p.add_argument("-s", "--search", metavar="QUERY",
                   help="DuckDuckGo search -> title + url per result (curl gets a CAPTCHA now; a "
                        "real browser is not challenged)")
    p.add_argument("-n", type=int, default=10, metavar="N", help="max search results (default 10)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--html", action="store_true", help="raw markup instead of text")
    mode.add_argument("--links", action="store_true", help="text, keeping hrefs as `anchor <url>`")
    p.add_argument("--max-chars", type=int, default=40000,
                   help="truncate each page (0 = no limit; default 40000)")
    p.add_argument("--shot", metavar="PATH", help="also save a PNG screenshot of each page")
    p.add_argument("--json", action="store_true", help="emit JSON instead of text")
    p.add_argument("--no-solve", action="store_true", help="don't auto-clear Cloudflare challenges")
    p.add_argument("--tries", type=int, default=20, help="max challenge-solve iterations")
    p.add_argument("--port", type=int, help="use/start an engine on this port (a second browser)")
    p.add_argument("-q", "--quiet", action="store_true", help="no progress notes on stderr")
    p.add_argument("--status", action="store_true", help="report engine/browser state and exit")
    p.add_argument("--stop", action="store_true",
                   help="shut the engine down — it is SHARED, this stops it for everyone")
    a = p.parse_args(argv)
    note = None if a.quiet else _note

    if a.stop:
        try:
            connect.call("shutdown", port=a.port, timeout=10.0)
            print("engine stopped")
        except connect.EngineError:
            print("engine was not running")           # already-stopped is the desired state, not an error
        return 0

    if a.search:
        hits = connect.search(a.search, max_results=a.n, port=a.port, on_start=note)
        if a.json:
            print(json.dumps(hits, indent=2))
        else:
            for i, h in enumerate(hits, 1):
                print(f"{i:2}. {h['title']}\n    {h['url']}")
        if not hits:
            print("no results parsed — DuckDuckGo may have changed its markup", file=sys.stderr)
            return 1
        return 0

    if a.status or not a.urls:
        port = a.port or connect.default_port()
        if not connect.reachable(port):
            print(json.dumps({"server": False, "alive": False, "port": port}))
            if not a.status:
                p.print_help(sys.stderr)
                return 2
            return 0
        st = connect.call("status", port=port, method="GET")
        st["port"] = port
        try:
            st["tabs"] = connect.call("tabs", port=port, method="GET").get("count")
        except connect.EngineError:
            pass
        print(json.dumps(st, indent=2))
        if not a.status:
            p.print_help(sys.stderr)
            return 2
        return 0

    fmt = "html" if a.html else ("text+links" if a.links else "text")
    results, failed = [], 0
    for i, url in enumerate(a.urls):
        try:
            r = connect.capture(url, mode=fmt, solve=not a.no_solve, max_chars=a.max_chars,
                                tries=a.tries, port=a.port, shot=bool(a.shot), on_start=note)
        except connect.EngineError as e:
            failed += 1
            print(f"{url}: {e}", file=sys.stderr)
            results.append({"url": url, "error": str(e)})
            continue
        if a.shot:
            import base64
            path = _shot_path(a.shot, i, len(a.urls))
            with open(path, "wb") as f:
                f.write(base64.b64decode(r.pop("b64")))
            r["shot"] = path
            if note:
                note(f"screenshot -> {path}")
        results.append(r)
        if not a.json:
            if i:
                print("\n" + "─" * 72 + "\n")
            print(r["text"])
            if r.get("challenge"):
                print(f"\n[cloudflare challenge: {r['challenge']}]", file=sys.stderr)
            if r.get("shot"):
                print(f"[screenshot: {r['shot']}]", file=sys.stderr)

    if a.json:
        print(json.dumps(results if len(results) > 1 else results[0], indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
