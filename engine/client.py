"""drive.py — the LOGIC side. Sends primitives to the always-running server (start_server.py).

This file changes as much as we like; the SERVER (and its alive browser + Turnstile clearance)
never restarts. The server is a dumb executor — drive.py decides what to do.

Primitives: goto/move/click/key/text/shot/js  (see start_server.py).

CLI:
  drive.py status
  drive.py goto <url>
  drive.py move <x> <y>            # CDP synthetic move — no real-cursor jump
  drive.py click <x> <y>
  drive.py shot [path]             # returns b64; saves png to tmp/shot.png (for Ollama/testing)
  drive.py text                    # current page html length + title
  drive.py passed                  # is the current page past Turnstile?
"""
import os, sys, json, base64, urllib.request

PORT = int(os.environ.get("PH_PORT","8731")); BASE=f"http://127.0.0.1:{PORT}"

def call(op, **a):
    if op=="status":
        return json.loads(urllib.request.urlopen(BASE+"/status",timeout=120).read())
    req=urllib.request.Request(BASE+"/"+op, data=json.dumps(a).encode(),
                               headers={"Content-Type":"application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req,timeout=120).read())

def save_shot(path="tmp/shot.png"):
    r=call("shot"); open(path,"wb").write(base64.b64decode(r["b64"]))
    return path, len(r["b64"])

CHALLENGE=("Just a moment","Verifying you are human","cf-chl","challenge-platform","turnstile")
def is_passed():
    t=call("text"); h=t["html"]
    return not any(c.lower() in (h+t["title"]).lower() for c in CHALLENGE) and len(h)>2000

def settle(lo=4.0, hi=8.0, i=0):
    """Wait for the page+challenge to fully appear before any mouse action. Turnstile scores
    human-like pacing — instant action reads as a bot. 4-8s, varied by call index (no Math.random
    needed; vary deterministically so it's not a fixed fingerprint)."""
    import time
    d = lo + (hi-lo) * ((i*0.37 + 0.13) % 1.0)   # spread across 4-8s, varies per call
    time.sleep(d); return round(d,1)

def solve(url, attempts=5):
    """Each attempt is FRESH: new tab + clear cookies, goto, settle 4-8s, check. Retry up to N."""
    for i in range(attempts):
        call("newtab"); call("clearcookies")
        r = call("goto", url=url)
        d = settle(4, 8, i)
        ok = is_passed()
        save_shot(f"tmp/solve-attempt-{i}.png")
        print(f"attempt {i}: status={r.get('status')} settled={d}s passed={ok}")
        if ok:
            print("*** PASSED ***", call("text")["url"]); return True
    print("all attempts exhausted — still blocked"); return False

def inject_marker(x, y):
    """TEMPORARY: inject a marker on the ACTUAL page at (x,y) to verify the coord lands on the
    real checkbox, screenshot it, then REMOVE it (page goes back pristine)."""
    js_add = ("(()=>{let d=document.getElementById('ph-tmp')||document.createElement('div');"
              "d.id='ph-tmp';d.style.cssText='position:fixed;left:%dpx;top:%dpx;width:18px;height:18px;"
              "margin:-9px 0 0 -9px;border:2px solid red;border-radius:50%%;background:rgba(255,0,0,.4);"
              "z-index:2147483647;pointer-events:none';document.documentElement.appendChild(d);"
              "return 'marker at %d,%d';})()" % (x, y, x, y))
    print(call("js", expr=js_add))
    save_shot("tmp/inject-check.png")
    print("screenshot -> tmp/inject-check.png")
    # REMOVE it (back to pristine)
    call("js", expr="(()=>{let d=document.getElementById('ph-tmp');if(d)d.remove();return 'removed';})()")
    print("marker removed (page pristine again)")

MODEL="qwen2.5vl:7b"
def detect():
    """THE pipeline: screenshot -> Ollama vision finds the checkbox -> push coord + info to viz."""
    import base64,json,time,struct
    png = base64.b64decode(call("shot")["b64"])
    w,h = struct.unpack(">II", png[16:24])
    prompt=(f"This is a {w}x{h} pixel screenshot. Find the Cloudflare 'Verify you are human' checkbox "
            "(the empty square to the LEFT of that text). Return JSON with its BOUNDING BOX in pixels: "
            "{\"box\":{\"x1\":<left>,\"y1\":<top>,\"x2\":<right>,\"y2\":<bottom>},"
            "\"found\":<bool>,\"description\":\"<what is there>\",\"confidence\":<0-1>}. "
            "x1,y1 = top-left corner of the checkbox; x2,y2 = bottom-right corner.")
    body=json.dumps({"model":MODEL,"prompt":prompt,"images":[base64.b64encode(png).decode()],
                     "stream":False,"format":"json"}).encode()
    req=urllib.request.Request("http://localhost:11434/api/generate",data=body,
                               headers={"Content-Type":"application/json"},method="POST")
    t0=time.monotonic()
    r=json.loads(urllib.request.urlopen(req,timeout=180).read())
    ms=int((time.monotonic()-t0)*1000)
    coord=json.loads(r["response"])
    b=coord.get("box") or {}
    x1,y1,x2,y2=int(b.get("x1",0)),int(b.get("y1",0)),int(b.get("x2",0)),int(b.get("y2",0))
    px,py=(x1+x2)//2,(y1+y2)//2   # center of the box
    info={"model":MODEL,"ms":ms,"found":coord.get("found"),"confidence":coord.get("confidence"),
          "description":coord.get("description"),"box":[x1,y1,x2,y2],"size":f"{x2-x1}x{y2-y1}"}
    print(f"OLLAMA ({MODEL}) {ms}ms -> box[{x1},{y1},{x2},{y2}] center({px},{py}) "
          f"size {x2-x1}x{y2-y1} conf={info['confidence']} '{info['description']}'")
    call("setmarkers", aim=[px,py], cursor=[px,py], box=[x1,y1,x2,y2], path=[], ollama=info)
    vw=call("js",expr="window.innerWidth")["result"]
    sc=vw/float(w); cx,cy=round(px*sc),round(py*sc)
    print(f"-> CSS coords for CDP mouse: ({cx},{cy})  [scale {sc:.3f}]")
    return {"png":[px,py],"css":[cx,cy],"frame":[w,h],"info":info}

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
    elif op=="newtab": print(call("newtab"))
    elif op=="inject": inject_marker(int(av[1]), int(av[2]))
    elif op=="detect": detect()
    elif op=="solvecf": print(call("solve", tries=int(av[1]) if len(av)>1 else 30))
    elif op=="rightmon":
        # real Chrome -> LEFT half of a QHD-width slice on the right monitor (MONITOR @ x=0).
        # QHD width 2560 split: real page = left 1280, viz browser goes on the right 1280.
        wid=call("cdp",method="Browser.getWindowForTarget",params={})["result"]["windowId"]
        print(call("cdp",method="Browser.setWindowBounds",
                   params={"windowId":wid,"bounds":{"left":0,"top":0,"width":1280,"height":1440,"windowState":"normal"}}))
        print("real page = left half. Open the viz on the RIGHT half: http://127.0.0.1:8731/viz")
    elif op=="solve": solve(av[1], int(av[2]) if len(av)>2 else 5)
    elif op=="shutdown":
        try: print(call("shutdown"))      # clean: server closes browser + exits over the port
        except Exception as e: print("shutdown sent (server exiting):", str(e)[:60])
    else: print("unknown:",op)

if __name__=="__main__": main(sys.argv[1:])
