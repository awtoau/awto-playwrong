"""client.py — interactive CLI: drive a page on the shared engine (started if down).

One page as text: use `playwrong <url>` (engine/cli.py). This drives the CLI's own tab ("client"),
kept across invocations, so it never touches another agent's page.

  client.py status                 # engine/browser state (the one verb that won't auto-start)
  client.py goto <url>
  client.py read [--links]         # the page as readable text
  client.py text                   # title, url, html length
  client.py js <expr>              # evaluate in the page (main world)
  client.py move <x> <y> | click <x> <y> | key <key>     # CDP input, viewport CSS pixels
  client.py shot [path]            # PNG, default tmp/shot.png
  client.py passed                 # is the page past the challenge?
  client.py solvecf [tries]        # clear a Turnstile challenge
  client.py cookies | clearcookies # clearcookies wipes the SHARED session
  client.py tabs | closetab | closeextra   # closetab: the CLI's own tab
  client.py cdp                    # the shared browser's CDP endpoint
  client.py cdp <Method> ['{json}'] [--browser]   # raw CDP on the CLI's tab (or the browser)
  client.py rightmon               # place the window (PH_RIGHTMON_BOUNDS=x,y,w,h)
  client.py shutdown               # stops the SHARED engine, for everyone
"""
import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine import connect

PORT = connect.default_port()


def rightmon_bounds():
    raw = os.environ.get("PH_RIGHTMON_BOUNDS", "0,0,1280,720")
    try:
        x, y, w, h = [int(v.strip()) for v in raw.split(",", 3)]
        return {"left": x, "top": y, "width": w, "height": h}
    except Exception:
        return {"left": 0, "top": 0, "width": 1280, "height": 720}

def _note(m): print(f"… {m}", file=sys.stderr, flush=True)

# The CLI's own tab, kept across invocations, so it never drives another agent's page. The owner
# label carries no pid, so closeextra never sweeps it as an orphan.
TAG, OWNER = "client", "client@cli"
DRIVING = {"goto", "text", "shot", "js", "click", "move", "key", "solve", "window", "cdp"}

def call(op, **a):
    """One engine op, engine and Chrome started if down. Driving ops act on the CLI's own tab.
    `status` does not auto-start: it reports what is true."""
    if op=="status":
        if not connect.reachable(): return {"server":False,"alive":False,"port":PORT}
        return connect.call("status", method="GET")
    if op in DRIVING and not (a.get("body") or {}).get("browser"):
        if "body" in a:
            a["body"].setdefault("tab", TAG)
        else:
            a.setdefault("tab", TAG)
    try:
        return connect.op(op, on_start=_note, **a)
    except connect.EngineError as e:
        if "no such tab" not in str(e):
            raise
        connect.op("newtab", url="about:blank", tag=TAG, owner=OWNER)
        return connect.op(op, on_start=_note, **a)

def save_shot(path="tmp/shot.png"):
    r=call("shot"); open(path,"wb").write(base64.b64decode(r["b64"]))
    return path, len(r["b64"])

def is_passed():
    return not connect.is_challenge(call("text"))

def main(av):
    if not av: print(__doc__); return
    op=av[0]
    if op=="status": print(json.dumps(call("status"),indent=2))
    elif op=="goto": print(json.dumps(call("goto",url=av[1]),indent=2))
    elif op=="move": print(call("move",x=int(av[1]),y=int(av[2])))
    elif op=="click": print(call("click",x=int(av[1]),y=int(av[2])))
    elif op=="key": print(call("key",key=av[1]))
    elif op=="shot":
        p,n=save_shot(av[1] if len(av)>1 else "tmp/shot.png"); print(f"saved {p} ({n} b64 chars)")
    elif op=="text":
        t=call("text"); print(f"title={t['title']!r} url={t['url']} html={len(t['html'])} bytes")
    elif op=="passed": print("PASSED" if is_passed() else "BLOCKED (challenge)")
    elif op=="clearcookies": print(call("clearcookies"))
    elif op=="js": print(json.dumps(call("js", expr=av[1])["result"], indent=2, default=str))
    elif op=="read":
        t=call("text"); print(connect.render(t, "text+links" if "--links" in av else "text", 0))
    elif op=="tabs": print(json.dumps(call("tabs"), indent=2))
    elif op=="closetab": print(call("closetab", tag=TAG))
    elif op=="closeextra": print(call("closeextra"))
    elif op=="cookies": print(json.dumps(call("cookies")["cookies"], indent=2))
    elif op=="solvecf": print(call("solve", tries=int(av[1]) if len(av)>1 else 30))
    elif op=="cdp":
        args = [x for x in av[1:] if x != "--browser"]
        # body=: "method"/"params" collide with call()'s own parameters
        body = {"method": args[0], "params": json.loads(args[1]) if len(args) > 1 else {},
                "browser": "--browser" in av} if args else {}
        print(json.dumps(call("cdp", body=body), indent=2, default=str))
    elif op=="rightmon": print(call("window", bounds=rightmon_bounds()))
    elif op=="shutdown":
        try: print(call("shutdown"))      # clean: server closes browser + exits over the port
        except Exception as e: print("shutdown sent (server exiting):", str(e)[:60])
    else: print("unknown:",op)

if __name__=="__main__":
    try:                        # an engine-side failure is a message, not a traceback
        main(sys.argv[1:])
    except connect.EngineError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
