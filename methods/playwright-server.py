"""start_server.py — DUMB, STABLE browser primitive-executor. Keeps ONE headed browser ALIVE.

This code should rarely change. It does NOT contain crawl/Turnstile logic — it only executes
PRIMITIVES sent by a local driver script (drive.py), so the driver can change as much as we like
while the browser (and its Turnstile clearance) stays alive. Listens on PH_PORT (default 8731).

Primitives (POST JSON {"op":..., ...}):
  goto     {url}            -> navigate, {status,url,title}
  move     {x,y}           -> CDP synthetic mouse move (does NOT move the real OS cursor)
  click    {x,y}           -> CDP synthetic click at x,y
  key      {key}           -> press a key (e.g. "Enter")
  text     {}              -> current page HTML  {html,title,url}
  shot     {path?}         -> screenshot; returns {b64} inline (for Ollama vision) + writes path if given
  js       {expr}          -> evaluate JS in the page, return its result
GET /status -> {alive,url}

real Chrome channel + async Playwright (sync segfaults under python3.14t). headless=False (visible).
Run:  .venv/bin/python scripts/start_server.py   (background; tmp/browser-server.log)
"""
import os, sys, json, asyncio, threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, os.path.dirname(__file__))
from ph_common import UA
from playwright.async_api import async_playwright

PORT = int(os.environ.get("PH_PORT", "8731"))
LOG = os.path.join(os.path.dirname(__file__), "..", "tmp", "browser-server.log")

def log(action, **kw):
    ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    with open(LOG, "a") as f:
        f.write(f"{ts} {action} " + " ".join(f"{k}={v}" for k,v in kw.items()) + "\n")

class Browser:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.pw=self.browser=self.ctx=self.page=self.cdp=None
        self.pos = self._load_pos()
        threading.Thread(target=lambda:(asyncio.set_event_loop(self.loop),self.loop.run_forever()),
                         daemon=True).start()
    @staticmethod
    def _load_pos():
        raw = os.environ.get("PH_WINDOW_BOUNDS", "0,0,1280,720")
        try:
            return tuple(int(v.strip()) for v in raw.split(",", 3))
        except Exception:
            return (0, 0, 1280, 720)
    def run(self, coro): return asyncio.run_coroutine_threadsafe(coro, self.loop).result()
    async def _ensure(self):
        if self.page: return
        try:
            log("launch_step", step="playwright_start")
            self.pw = await async_playwright().start()
            # NOTE: --remote-debugging-port is a KNOWN Turnstile detection tell — removed.
            # (re-add only if real-DevTools access is needed and detection isn't a concern.)
            log("launch_step", step="chromium_launch")
            # Launch flags ported from nodriver (the lib that beats Turnstile). KEY:
            #   --disable-features=IsolateOrigins,site-per-process + --disable-site-isolation-trials
            # turn off site isolation, so JS/find/click can reach INTO the cross-origin Turnstile
            # iframe (what blocked our harness). Plus the anti-detect/clean flag set.
            self.browser = await self.pw.chromium.launch(
                headless=False, channel="chrome",
                args=["--start-maximized","--disable-blink-features=AutomationControlled",
                      "--ozone-platform=x11",
                      "--disable-features=IsolateOrigins,site-per-process",
                      "--disable-site-isolation-trials",
                      "--no-first-run","--no-default-browser-check","--no-service-autorun",
                      "--password-store=basic","--disable-infobars","--no-pings"])
        except Exception as e:
            log("launch_FAILED", err=repr(e)[:300]); raise   # fail fast + loud, never silent
        # DO NOT force a UA (Firefox-on-Chrome contradiction = instant Turnstile flag).
        # DO NOT force a viewport: a fixed viewport makes screen==window (impossible on real hardware)
        # + fake devicePixelRatio — a classic Playwright tell. no_viewport=True lets Chrome use the
        # REAL OS screen geometry (screen >= window, correct DPR), matching a genuine browser.
        self.ctx = await self.browser.new_context(no_viewport=True)
        # chrome.runtime present (real logged-in Chrome has it; bare automation context doesn't)
        await self.ctx.add_init_script(
            "if(window.chrome&&!window.chrome.runtime){window.chrome.runtime={};}")
        await self.ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        self.page = await self.ctx.new_page()
        self.cdp = await self.ctx.new_cdp_session(self.page)
        await self._position()   # optional placement via PH_WINDOW_BOUNDS=x,y,w,h
        # mouse recorder that SURVIVES challenge reloads: a Python binding + an init-script that
        # re-installs on every navigation and streams each event back to the server (stamped log).
        self.recording = False
        async def _rec(source, ev):
            if self.recording:
                log("mouse", t=ev.get("t"), x=ev.get("x"), y=ev.get("y"), kind=ev.get("k"))
        await self.ctx.expose_binding("phRec", _rec)
        await self.ctx.add_init_script("""
          (() => {
            const send=(k)=>(e)=>{ if(window.phRec) window.phRec({t:Math.round(performance.now()),x:e.clientX,y:e.clientY,k}); };
            addEventListener('mousemove', send('m'), true);
            addEventListener('mousedown', send('d'), true);
            addEventListener('mouseup',   send('u'), true);
          })();
        """)
        # NOTE: real challenge page is kept PRISTINE — NO overlay injection (injection itself could
        # be a detection tell). Visualization (detected box + simulated path) is rendered in a
        # SEPARATE viz tab over a screenshot mirror — see the /viz endpoint + drive.py.
        log("browser_launched")
    # --- primitives (each is small + stable) ---
    async def _goto(self, url):
        await self._ensure()
        r = await self.page.goto(url, timeout=60000, wait_until="domcontentloaded")
        return {"status": r.status if r else 0, "url": self.page.url, "title": await self.page.title()}
    async def _move(self, x, y):
        await self._ensure()
        await self.cdp.send("Input.dispatchMouseEvent", {"type":"mouseMoved","x":x,"y":y}); return {"ok":1}
    async def _click(self, x, y):
        await self._ensure()
        for t in ("mousePressed","mouseReleased"):
            await self.cdp.send("Input.dispatchMouseEvent",
                {"type":t,"x":x,"y":y,"button":"left","clickCount":1})
        return {"ok":1}
    async def _key(self, key):
        await self._ensure(); await self.page.keyboard.press(key); return {"ok":1}
    async def _text(self):
        await self._ensure()
        return {"html": await self.page.content(), "title": await self.page.title(), "url": self.page.url}
    async def _shot(self, path=None):
        """screenshot -> base64 inline (so the driver can hand it to Ollama vision / save for testing)."""
        await self._ensure()
        png = await self.page.screenshot(path=path) if path else await self.page.screenshot()
        import base64
        return {"b64": base64.b64encode(png).decode(), "path": path}
    async def _js(self, expr):
        await self._ensure(); return {"result": await self.page.evaluate(expr)}
    async def _rec(self, on):
        await self._ensure(); self.recording = bool(on)
        log("recording", on=self.recording); return {"recording": self.recording}
    async def _clearcookies(self):
        await self._ensure(); await self.ctx.clear_cookies()
        log("cookies_cleared"); return {"cleared": True}
    async def _solve(self, tries=30):
        """Wait for the Turnstile checkbox iframe to render (appears seconds after load), then click
        into it. Site isolation is OFF (launch flags) so we can reach the cross-origin iframe + use
        Playwright's frame_locator. Polls up to `tries` (~0.7s each)."""
        await self._ensure()
        import asyncio
        for i in range(tries):
            title = await self.page.title()
            html = await self.page.content()
            if "just a moment" not in title.lower() and "verify you are human" not in html.lower():
                log("solve_passed", iter=i); return {"passed": True, "iter": i}
            # the Turnstile widget lives in an iframe from challenges.cloudflare.com
            for fr in self.page.frames:
                if "challenges.cloudflare.com" in (fr.url or ""):
                    try:
                        cb = fr.locator("input[type=checkbox], label")
                        if await cb.count():
                            await cb.first.click(timeout=2000)
                            log("solve_clicked", iter=i, frame=fr.url[:50])
                            await asyncio.sleep(3)
                    except Exception as e:
                        log("solve_click_err", err=str(e)[:60])
            await asyncio.sleep(0.7)
        log("solve_exhausted")
        return {"passed": False, "iter": tries}
    # window placement via wmctrl (Chrome under Xwayland), configurable via PH_WINDOW_BOUNDS.
    async def _position(self):
        # xdotool works under GNOME/Mutter+Xwayland where wmctrl -e and CDP setWindowBounds are
        # ignored. Find the Chrome window, un-maximize, then move+size.
        import subprocess
        try:
            x, y, w, h = self.pos
            wid = subprocess.run(["xdotool","search","--name","Google Chrome"],
                                 capture_output=True, text=True).stdout.split()
            if not wid:
                log("position_FAILED", err="no chrome window"); return
            wid = wid[-1]
            subprocess.run(["wmctrl","-i","-r","0x%08x"%int(wid),
                            "-b","remove,maximized_vert,maximized_horz"], capture_output=True)
            subprocess.run(["xdotool","windowsize",wid,str(w),str(h)], capture_output=True)
            subprocess.run(["xdotool","windowmove",wid,str(x),str(y)], capture_output=True)
            geo = subprocess.run(["xdotool","getwindowgeometry",wid],capture_output=True,text=True).stdout
            log("positioned", target=f"{x},{y},{w},{h}", got=geo.replace("\n"," ")[:80])
        except Exception as e:
            log("position_FAILED", err=repr(e)[:200])
    async def _cdpcmd(self, method, params):
        """raw CDP passthrough — send any DevTools command (e.g. Browser.setWindowBounds)."""
        await self._ensure()
        return {"result": await self.cdp.send(method, params or {})}
    async def _newtab(self):
        """Fresh tab for a clean attempt: close old page, open a new one (recorder re-installs via
        the context init-script). CDP session re-attaches to the new page."""
        await self._ensure()
        try:
            if self.page: await self.page.close()
        except Exception: pass
        self.page = await self.ctx.new_page()
        self.cdp = await self.ctx.new_cdp_session(self.page)
        await self._position()
        log("newtab"); return {"newtab": True}
    async def _frame(self):
        """latest screenshot bytes of the REAL page (for the viz mirror)."""
        await self._ensure(); return await self.page.screenshot()
    # dispatch table
    def do(self, op, a):
        m = {"goto":lambda:self._goto(a["url"]), "move":lambda:self._move(a["x"],a["y"]),
             "click":lambda:self._click(a["x"],a["y"]), "key":lambda:self._key(a["key"]),
             "text":lambda:self._text(), "shot":lambda:self._shot(a.get("path")),
             "js":lambda:self._js(a["expr"]), "rec":lambda:self._rec(a.get("on",True)),
             "clearcookies":lambda:self._clearcookies(), "newtab":lambda:self._newtab(),
             "cdp":lambda:self._cdpcmd(a["method"], a.get("params")),
             "solve":lambda:self._solve(a.get("tries",30))}
        if op not in m: log("op_unknown", op=op); return {"error":f"unknown op {op}"}
        try:
            r = self.run(m[op]())
            log("op_ok", op=op); return r
        except Exception as e:
            log("op_FAILED", op=op, err=repr(e)[:300]); return {"error": repr(e)[:300]}

B = Browser()
MARKERS = {"aim": None, "cursor": None, "path": [], "ollama": None, "box": None}  # box = [x1,y1,x2,y2]

# side-by-side viz page: LEFT = live mirror of the real page + overlay; RIGHT = info/status.
# The real Chrome window stays pristine; open this in a browser tiled beside it.
VIZ_HTML = """<!doctype html><html><head><meta charset=utf-8><title>PH turnstile viz</title>
<style>body{margin:0;font:13px monospace;background:#111;color:#ddd;display:flex;height:100vh}
#l{flex:1;position:relative;border-right:2px solid #333;overflow:hidden}
#r{width:280px;padding:10px;overflow:auto}
img{width:100%;display:block}#ov{position:absolute;inset:0;pointer-events:none}
.k{color:#6cf}</style></head><body>
<div id=l><img id=shot><canvas id=ov></canvas></div>
<div id=r><b>turnstile viz</b><div id=info></div></div>
<script>
const img=document.getElementById('shot'),ov=document.getElementById('ov'),info=document.getElementById('info');
async function tick(){
  img.src='/frame?'+Date.now();
  const m=await (await fetch('/markers')).json();
  await img.decode().catch(()=>{});
  ov.width=img.clientWidth; ov.height=img.clientHeight;
  const sx=img.clientWidth/(img.naturalWidth||1), sy=img.clientHeight/(img.naturalHeight||1);
  const c=ov.getContext('2d'); c.clearRect(0,0,ov.width,ov.height);
  if(m.path&&m.path.length){c.strokeStyle='rgba(0,150,255,.8)';c.beginPath();
    m.path.forEach((p,i)=>{const x=p[0]*sx,y=p[1]*sy;i?c.lineTo(x,y):c.moveTo(x,y)});c.stroke();}
  if(m.box){c.strokeStyle='lime';c.lineWidth=2;
    c.strokeRect(m.box[0]*sx,m.box[1]*sy,(m.box[2]-m.box[0])*sx,(m.box[3]-m.box[1])*sy);}
  if(m.aim){c.strokeStyle='red';c.lineWidth=2;c.strokeRect(m.aim[0]*sx-12,m.aim[1]*sy-12,24,24);}
  if(m.cursor){c.fillStyle='rgba(255,0,0,.6)';c.beginPath();c.arc(m.cursor[0]*sx,m.cursor[1]*sy,9,0,7);c.fill();}
  let o=m.ollama||{};
  info.innerHTML='<span class=k>aim</span> '+JSON.stringify(m.aim)+'<br><span class=k>cursor</span> '+
    JSON.stringify(m.cursor)+'<br><span class=k>path pts</span> '+(m.path?m.path.length:0)+
    '<hr><b>ollama</b><br><span class=k>model</span> '+(o.model||'-')+
    '<br><span class=k>time</span> '+(o.ms!=null?o.ms+'ms':'-')+
    '<br><span class=k>found</span> '+(o.found!=null?o.found:'-')+
    '<br><span class=k>conf</span> '+(o.confidence!=null?o.confidence:'-')+
    '<br><span class=k>desc</span> '+(o.description||'-');
}
setInterval(tick,100); tick();   // 10fps
</script></body></html>"""

class H(BaseHTTPRequestHandler):
    def _send(self,o,c=200):
        b=json.dumps(o).encode(); self.send_response(c)
        self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def _raw(self,b,ct,c=200):
        self.send_response(c); self.send_header("Content-Type",ct)
        self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def log_message(self,*a): pass
    def do_GET(self):
        if self.path=="/status":
            self._send({"alive":B.page is not None,"url":(B.page.url if B.page else None)})
        elif self.path=="/viz": self._raw(VIZ_HTML.encode(),"text/html")
        elif self.path.startswith("/frame"):
            try: self._raw(B.run(B._frame()),"image/png")
            except Exception as e: self._raw(b"",("text/plain"),500)
        elif self.path=="/markers": self._send(MARKERS)
        else: self._send({"error":"unknown GET"},404)
    def do_POST(self):
        n=int(self.headers.get("Content-Length") or 0); a=json.loads(self.rfile.read(n) or b"{}")
        op=self.path.strip("/")
        if op=="shutdown": self._send({"ok":1}); threading.Thread(target=lambda:os._exit(0)).start(); return
        if op=="setmarkers": MARKERS.update(a); self._send(MARKERS); return
        try: self._send(B.do(op,a))
        except Exception as e: self._send({"error":str(e)[:200]},500)

if __name__=="__main__":
    log("server_start", port=PORT)
    ThreadingHTTPServer(("127.0.0.1",PORT),H).serve_forever()
