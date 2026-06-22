"""Control surface for the persistent browser service (start_server.py).
Sends commands to the ALREADY-RUNNING browser — never launches a new window.

Usage:
  .venv/bin/python scripts/browser_ctl.py status
  .venv/bin/python scripts/browser_ctl.py goto https://www.powderhounds.com/Canada/Fernie.aspx
  .venv/bin/python scripts/browser_ctl.py shutdown_browser
  .venv/bin/python scripts/browser_ctl.py shutdown
"""
import os, sys, json, urllib.request

PORT = int(os.environ.get("PH_PORT", "8731"))
BASE = f"http://127.0.0.1:{PORT}"


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data: req.add_header("Content-Type", "application/json")
    try:
        return json.loads(urllib.request.urlopen(req, timeout=180).read())
    except urllib.error.URLError as e:
        return {"error": f"server not reachable on {BASE} ({e}). Start it: scripts/start_server.py"}


def main(argv):
    if not argv:
        print(__doc__); return
    cmd = argv[0]
    if cmd == "status":
        print(json.dumps(call("GET", "/status"), indent=2))
    elif cmd == "goto":
        print(json.dumps(call("POST", "/goto", {"url": argv[1]}), indent=2))
    elif cmd == "capture":
        # capture in the open window; save full result JSON to tmp for the loader
        r = call("POST", "/capture", {"url": argv[1]})
        import os
        slug = argv[1].replace("https://", "").replace("http://", "")
        slug = "".join(ch if ch.isalnum() else "_" for ch in slug)[:60]
        out = os.path.join(os.path.dirname(__file__), "..", "tmp", f"srvcap-{slug}.json")
        json.dump(r, open(out, "w"), indent=2)
        print(f"captured: {r.get('n_requests')} reqs, {r.get('n_failed')} failed, "
              f"{r.get('n_render_blocking')} render-blocking, passed={r.get('passed')} -> {out}")
    elif cmd == "shutdown_browser":
        print(json.dumps(call("POST", "/shutdown_browser"), indent=2))
    elif cmd == "shutdown":
        print(json.dumps(call("POST", "/shutdown"), indent=2))
    else:
        print("unknown command:", cmd, "\n", __doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
