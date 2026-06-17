// Persistent browser SERVER (Node + Playwright). Opens ONE window, keeps it alive, exposes an HTTP
// port API so commands hit the EXISTING window — no pop-up-and-die. ms-timestamped action log.
//
// Start once:  node ph-browser-server.mjs           (PH_PORT default 8731)
// Drive it:    python browser_ctl.py goto <url>  (or curl)
// Shutdown:    python browser_ctl.py shutdown
import { chromium } from "playwright";
import http from "node:http";
import fs from "node:fs";

const PORT = parseInt(process.env.PH_PORT || "8731", 10);
const PROFILE = process.env.HOME + "/ai-browser-profiles/ai-profile";
const OLLAMA = "http://localhost:11434/api/generate";
const VLM = process.env.PH_VLM || "qwen2.5vl:7b";
const LOG = "/home/dan/git/awtoau/powderhounds/tmp/ph-browser-server.log";

// ---- ms-timestamped action logger (awto-dan discipline) ----
function log(action, fields = {}) {
  const ts = new Date().toISOString(); // ms precision
  const kv = Object.entries(fields).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(" ");
  const line = `${ts} ${action} ${kv}`.trim();
  fs.appendFileSync(LOG, line + "\n");
  console.log(line);
}
async function timed(action, fields, fn) {
  const t0 = Date.now();
  try { const r = await fn(); log(action, { ...fields, ok: true, ms: Date.now() - t0 }); return r; }
  catch (e) { log(action, { ...fields, ok: false, ms: Date.now() - t0, err: e.message }); throw e; }
}

const rnd = (a, b) => a + Math.random() * (b - a);
let page, ctx, lastMouse = { x: 200, y: 200 };

async function humanMove(x, y) {
  const s = lastMouse, steps = Math.floor(rnd(18, 30));
  const cx = (s.x + x) / 2 + rnd(-120, 120), cy = (s.y + y) / 2 + rnd(-90, 90);
  for (let i = 1; i <= steps; i++) {
    const t = i / steps;
    const bx = (1 - t) ** 2 * s.x + 2 * (1 - t) * t * cx + t * t * x;
    const by = (1 - t) ** 2 * s.y + 2 * (1 - t) * t * cy + t * t * y;
    await page.mouse.move(bx + rnd(-1.5, 1.5), by + rnd(-1.5, 1.5));
    await page.waitForTimeout(rnd(6, 22));
  }
  await page.mouse.move(x + rnd(-4, 4), y + rnd(-4, 4)); await page.waitForTimeout(rnd(40, 120));
  await page.mouse.move(x, y); lastMouse = { x, y };
}
const hasChallenge = () => page.frames().some(f => f.url().includes("challenges.cloudflare.com"));

async function visionClick() {
  const b64 = (await page.screenshot()).toString("base64");
  const prompt = 'Cloudflare "Verify you are human" checkbox (small square left of a wide widget). '
    + 'Return ONLY {"x":int,"y":int} center, or {"x":-1,"y":-1}.';
  const r = await fetch(OLLAMA, { method: "POST", body: JSON.stringify(
    { model: VLM, prompt, images: [b64], stream: false, options: { temperature: 0 } }) });
  const m = ((await r.json()).response || "").match(/\{[^}]*\}/);
  const loc = m ? JSON.parse(m[0]) : null;
  if (loc && loc.x > 0) {
    await humanMove(loc.x, loc.y); await page.waitForTimeout(rnd(120, 350));
    await page.mouse.down(); await page.waitForTimeout(rnd(50, 120)); await page.mouse.up();
    await page.waitForTimeout(4500);
    log("vision_click", { x: loc.x, y: loc.y });
    return loc;
  }
  log("vision_click", { found: false });
  return null;
}

async function clearCfCookies() {
  const all = await ctx.cookies();
  const keep = all.filter(c => !/cf_clearance|cf_chl|__cf_bm/i.test(c.name) && !/cloudflare/i.test(c.domain));
  await ctx.clearCookies(); await ctx.addCookies(keep);
  log("clear_cf_cookies", { removed: all.length - keep.length, kept: keep.length });
}

async function goto(url) {
  return timed("goto", { url }, async () => {
    // 'commit' = resolve as soon as response starts; heavy pages never fire 'load'. Then settle.
    await page.goto(url, { waitUntil: "commit", timeout: 45000 }).catch(e => log("goto_note", { err: e.message }));
    await page.waitForTimeout(3000);
    for (let round = 1; round <= 3; round++) {
      if (!hasChallenge()) break;
      log("challenge_round", { round, url });
      await visionClick();
      await page.waitForTimeout(2000);
      if (hasChallenge() && round < 3) await clearCfCookies();
    }
    await page.waitForTimeout(5000);  // let DOM + async tags settle (no 'load' wait — heavy pages hang it)
    const passed = !hasChallenge();
    return { url, passed, title: (await page.title()).slice(0, 80) };
  });
}

// Capture HAR-ish network + vitals + render-blocking in the ALREADY-OPEN page (one window reused).
async function capture(url) {
  return timed("capture", { url }, async () => {
    const cdp = await ctx.newCDPSession(page);
    const entries = [];
    const reqs = {};
    await cdp.send("Network.enable");
    cdp.on("Network.requestWillBeSent", e => { reqs[e.requestId] = { url: e.request.url, type: e.type, start: e.timestamp, initiator: e.initiator?.type, t0: Date.now() }; });
    cdp.on("Network.responseReceived", e => { const r = reqs[e.requestId]; if (r) { r.status = e.response.status; r.mime = e.response.mimeType; r.cf = e.response.headers?.["cf-cache-status"]; r.renderBlocking = e.response.renderBlockingStatus; r.ip = e.response.remoteIPAddress; } });
    cdp.on("Loading.loadingFinished", e => { const r = reqs[e.requestId]; if (r) { r.ms = Date.now() - r.t0; r.bytes = e.encodedDataLength; } });
    cdp.on("Network.loadingFailed", e => { const r = reqs[e.requestId]; if (r) { r.failed = e.errorText; r.ms = Date.now() - r.t0; } });

    await page.goto(url, { waitUntil: "commit", timeout: 45000 }).catch(e => log("cap_nav", { err: e.message }));
    await page.waitForTimeout(3000);
    for (let i = 0; i < 3 && hasChallenge(); i++) { await visionClick(); }
    await page.waitForTimeout(7000);  // let async tags fire

    const vitals = await page.evaluate(() => {
      const out = { paint: {}, resources: [] };
      for (const p of performance.getEntriesByType("paint")) out.paint[p.name] = Math.round(p.startTime);
      const nav = performance.getEntriesByType("navigation")[0];
      if (nav) out.nav = { ttfb: Math.round(nav.responseStart), dcl: Math.round(nav.domContentLoadedEventEnd), load: Math.round(nav.loadEventEnd) };
      out.resources = performance.getEntriesByType("resource").map(r => ({ name: r.name, type: r.initiatorType, dur: Math.round(r.duration), blocking: r.renderBlockingStatus, transfer: r.transferSize }));
      return out;
    }).catch(() => ({ paint: {}, resources: [] }));

    await cdp.detach().catch(() => {});
    const net = Object.values(reqs);
    return { url, title: (await page.title()).slice(0, 80), passed: !hasChallenge(),
             network: net, vitals, n_requests: net.length,
             n_failed: net.filter(r => r.failed).length,
             n_render_blocking: net.filter(r => r.renderBlocking === "blocking").length };
  });
}

// ---- HTTP control surface ----
function send(res, obj, code = 200) {
  const body = JSON.stringify(obj);
  res.writeHead(code, { "Content-Type": "application/json" }); res.end(body);
}
const server = http.createServer((req, res) => {
  let data = "";
  req.on("data", c => data += c);
  req.on("end", async () => {
    const body = data ? JSON.parse(data) : {};
    try {
      if (req.url === "/status") return send(res, { browser: !!page, url: page ? page.url() : null });
      if (req.url === "/goto") return send(res, await goto(body.url));
      if (req.url === "/capture") return send(res, await capture(body.url));
      if (req.url === "/shutdown_browser") {
        await ctx.close(); page = ctx = null; log("shutdown_browser", {});
        return send(res, { msg: "browser closed; server up" });
      }
      if (req.url === "/shutdown") {
        send(res, { msg: "server shutting down" }); log("shutdown", {});
        setTimeout(() => process.exit(0), 200); return;
      }
      send(res, { error: "unknown" }, 404);
    } catch (e) { send(res, { error: e.message }, 500); }
  });
});

(async () => {
  log("server_boot", { port: PORT });
  ctx = await timed("launch", { profile: PROFILE }, () => chromium.launchPersistentContext(PROFILE, {
    channel: "chrome", headless: false, slowMo: 120, viewport: { width: 1440, height: 810 },
    args: ["--disable-blink-features=AutomationControlled", "--window-size=1440,810", "--window-position=120,80"],
  }));
  await ctx.addInitScript(() => Object.defineProperty(navigator, "webdriver", { get: () => undefined }));
  page = await ctx.newPage();
  server.listen(PORT, "127.0.0.1", () => log("listening", { url: `http://127.0.0.1:${PORT}` }));
})();
