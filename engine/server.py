"""nd_server.py — single-browser server backed by NODRIVER (not Playwright).

Playwright is the Turnstile tell (CF serves it a dead challenge). nodriver drives Chrome via raw
CDP and passes. This server gives our harness's control port + viz, but ONE nodriver browser does
everything: solve Turnstile, crawl, and feed the viz screenshots. No second browser, no orphans.

Control port (POST JSON): goto{url} solve newtab clearcookies text shot frame
GET /status /viz /frame /markers ; POST /setmarkers /shutdown

Run: PYTHONPATH=vendor .venv/bin/python scripts/nd_server.py   (tmp/nd-server.log)
"""
import sys, os, json, asyncio, threading, base64
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "vendor"))
import nodriver as uc
from nodriver import cdp

PORT = int(os.environ.get("PH_PORT", "8731"))
TMP = os.path.join(os.path.dirname(__file__), "..", "tmp")
os.makedirs(TMP, exist_ok=True)                 # ensure tmp/ exists on a fresh checkout
LOG = os.path.join(TMP, "nd-server.log")
CHALLENGE = ("just a moment", "verify you are human", "cf-chl", "challenge-platform")

def log(a, **k):
    ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    try:
        open(LOG, "a").write(f"{ts} {a} " + " ".join(f"{x}={y}" for x,y in k.items()) + "\n")
    except Exception:
        pass

class ND:
    """nodriver browser on its own asyncio loop in a thread; sync-facing .do() for the HTTP handler."""
    def __init__(self):
        self.loop = asyncio.new_event_loop(); self.browser=self.tab=None
        threading.Thread(target=lambda:(asyncio.set_event_loop(self.loop),self.loop.run_forever()),
                         daemon=True).start()
    def run(self, coro): return asyncio.run_coroutine_threadsafe(coro, self.loop).result()
    async def _ensure(self):
        if self.tab: return
        self.browser = await uc.start(headless=False)
        self.tab = await self.browser.get("about:blank")
        log("nd_started")
    async def _goto(self, url):
        await self._ensure(); await self.tab.get(url); await self.tab.sleep(2)
        return {"title": await self.tab.evaluate("document.title"), "url": url}
    def _is_chal(self, t, h):
        t=(t or "").lower(); h=(h or "").lower()
        return any(k in t for k in CHALLENGE) or "verify you are human" in h
    async def _solve(self, tries=20):
        await self._ensure()
        for i in range(tries):
            t=await self.tab.evaluate("document.title"); h=await self.tab.get_content()
            if not self._is_chal(t,h): log("solved",i=i); return {"passed":True,"iter":i}
            try:
                el=await self.tab.find("verify you are human", best_match=True, timeout=3)
                if el: await el.mouse_click(); log("clicked",i=i); await self.tab.sleep(4)
            except Exception as e: log("click_err",e=str(e)[:50])
            await self.tab.sleep(1)
        return {"passed":False,"iter":tries}
    async def _text(self):
        await self._ensure()
        return {"title":await self.tab.evaluate("document.title"),"html":await self.tab.get_content(),
                "url": self.tab.url if hasattr(self.tab,'url') else ""}
    async def _frame(self):
        await self._ensure()
        p=os.path.join(TMP,"nd-frame.png")
        await self.tab.save_screenshot(p); return open(p,"rb").read()
    async def _shot(self):
        return {"b64": base64.b64encode(await self._frame()).decode()}
    async def _clearcookies(self):
        await self._ensure(); await self.browser.cookies.clear(); return {"cleared":True}
    # --- added verbs (CDP input + tab ops) so the full client surface works on the nodriver engine ---
    async def _move(self, x, y):
        await self._ensure()
        await self.tab.send(cdp.input_.dispatch_mouse_event(type_="mouseMoved", x=float(x), y=float(y)))
        return {"ok":1,"x":x,"y":y}
    async def _click(self, x, y):
        await self._ensure()
        for ty in ("mousePressed","mouseReleased"):
            await self.tab.send(cdp.input_.dispatch_mouse_event(
                type_=ty, x=float(x), y=float(y), button=cdp.input_.MouseButton.LEFT, click_count=1))
        return {"ok":1,"x":x,"y":y}
    async def _key(self, key):
        await self._ensure()
        # named keys (Enter/Tab/...) carry a code; printable chars go as text
        named = {"Enter":13,"Tab":9,"Escape":27,"Backspace":8,"ArrowDown":40,"ArrowUp":38}
        if key in named:
            for ty in ("keyDown","keyUp"):
                await self.tab.send(cdp.input_.dispatch_key_event(
                    type_=ty, key=key, windows_virtual_key_code=named[key]))
        else:
            await self.tab.send(cdp.input_.dispatch_key_event(type_="char", text=key))
        return {"ok":1,"key":key}
    async def _newtab(self, url="about:blank"):
        await self._ensure()
        self.tab = await self.browser.get(url, new_tab=True)
        return {"ok":1,"url":url}
    async def _js(self, expr):
        await self._ensure(); return {"result": await self.tab.evaluate(expr)}
    async def _cookies(self):
        await self._ensure()
        cks = await self.browser.cookies.get_all()
        return {"cookies":[{"name":c.name,"value":c.value,"domain":getattr(c,"domain",None)} for c in cks]}
    def do(self, op, a):
        m={"goto":lambda:self._goto(a["url"]),"solve":lambda:self._solve(a.get("tries",20)),
           "text":lambda:self._text(),"shot":lambda:self._shot(),"clearcookies":lambda:self._clearcookies(),
           "move":lambda:self._move(a["x"],a["y"]),"click":lambda:self._click(a["x"],a["y"]),
           "key":lambda:self._key(a["key"]),"newtab":lambda:self._newtab(a.get("url","about:blank")),
           "js":lambda:self._js(a["expr"]),"cookies":lambda:self._cookies()}
        if op not in m: return {"error":f"unknown {op}"}
        try: return self.run(m[op]())
        except Exception as e: log("op_err",op=op,e=repr(e)[:120]); return {"error":repr(e)[:160]}

B = ND()
MARKERS={"aim":None,"cursor":None,"path":[],"box":None,"ollama":None}
VIZ_HTML="""<!doctype html><meta charset=utf-8><title>nd viz</title>
<style>body{margin:0;font:13px monospace;background:#111;color:#ddd;display:flex;height:100vh}
#l{flex:1;position:relative}#r{width:280px;padding:10px}img{width:100%}#ov{position:absolute;inset:0}</style>
<div id=l><img id=s><canvas id=ov></canvas></div><div id=r><b>nd viz</b><div id=i></div></div>
<script>const s=document.getElementById('s'),ov=document.getElementById('ov'),inf=document.getElementById('i');
async function t(){s.src='/frame?'+Date.now();const m=await(await fetch('/markers')).json();
await s.decode().catch(()=>{});ov.width=s.clientWidth;ov.height=s.clientHeight;
const sx=s.clientWidth/(s.naturalWidth||1),sy=s.clientHeight/(s.naturalHeight||1),c=ov.getContext('2d');
c.clearRect(0,0,ov.width,ov.height);
if(m.box){c.strokeStyle='lime';c.lineWidth=2;c.strokeRect(m.box[0]*sx,m.box[1]*sy,(m.box[2]-m.box[0])*sx,(m.box[3]-m.box[1])*sy);}
if(m.aim){c.strokeStyle='red';c.strokeRect(m.aim[0]*sx-12,m.aim[1]*sy-12,24,24);}
let o=m.ollama||{};inf.innerHTML='model '+(o.model||'-')+'<br>time '+(o.ms||'-')+'ms<br>conf '+(o.confidence||'-')+'<br>'+(o.description||'');}
setInterval(t,100);t();</script>"""

class H(BaseHTTPRequestHandler):
    def _j(self,o,c=200):
        b=json.dumps(o).encode();self.send_response(c)
        self.send_header("Content-Type","application/json");self.send_header("Content-Length",str(len(b)))
        self.end_headers();self.wfile.write(b)
    def _raw(self,b,ct,c=200):
        self.send_response(c);self.send_header("Content-Type",ct)
        self.send_header("Content-Length",str(len(b)));self.end_headers();self.wfile.write(b)
    def log_message(self,*a):pass
    def do_GET(self):
        if self.path=="/status":self._j({"alive":B.tab is not None})
        elif self.path=="/viz":self._raw(VIZ_HTML.encode(),"text/html")
        elif self.path.startswith("/frame"):
            try:self._raw(B.run(B._frame()),"image/png")
            except Exception as e:self._raw(b"","text/plain",500)
        elif self.path=="/markers":self._j(MARKERS)
        else:self._j({"error":"?"},404)
    def do_POST(self):
        n=int(self.headers.get("Content-Length") or 0);a=json.loads(self.rfile.read(n) or b"{}")
        op=self.path.strip("/")
        if op=="shutdown":self._j({"ok":1});threading.Thread(target=lambda:os._exit(0)).start();return
        if op=="setmarkers":MARKERS.update(a);self._j(MARKERS);return
        self._j(B.do(op,a))

if __name__=="__main__":
    log("server_start",port=PORT)
    ThreadingHTTPServer(("127.0.0.1",PORT),H).serve_forever()
