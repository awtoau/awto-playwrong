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
    await page.goto(url, { waitUntil: "load", timeout: 45000 });
    await page.waitForTimeout(3000);
    for (let round = 1; round <= 3; round++) {
      if (!hasChallenge()) break;
      log("challenge_round", { round, url });
      await visionClick();
      await page.waitForTimeout(2000);
      if (hasChallenge() && round < 3) await clearCfCookies();
    }
    const passed = !hasChallenge();
    return { url, passed, title: (await page.title()).slice(0, 80) };
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
