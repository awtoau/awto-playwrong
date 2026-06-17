// CDP capture — the data HAR lacks: render-blocking status per resource, main-thread/CPU time,
// long tasks, Web Vitals. Uses raw CDP (Network + Performance + PerformanceObserver). Passes
// Cloudflare. Writes <out>/cdp-<slug>.json -> Python loader -> Postgres.
//
// Usage: node ph-cdp-trace.mjs <url> [outdir]
import { chromium } from "playwright";
import fs from "node:fs";
import path from "node:path";

const URL = process.argv[2] || "https://www.powderhounds.com/Canada/Fernie.aspx";
const OUTDIR = process.argv[3] || "/home/dan/git/awtoau/powderhounds/tmp";
const slug = URL.replace(/^https?:\/\//, "").replace(/[^a-z0-9]+/gi, "_").slice(0, 60);
const OUT = path.join(OUTDIR, `cdp-${slug}.json`);
const OLLAMA = "http://localhost:11434/api/generate";
const rnd = (a, b) => a + Math.random() * (b - a);
const log = (a, o = {}) => { const l = `${new Date().toISOString()} ${a} ${Object.entries(o).map(([k, v]) => k + "=" + JSON.stringify(v)).join(" ")}`; fs.appendFileSync(path.join(OUTDIR, "ph-cdp-trace.log"), l + "\n"); console.log(l); };

const ctx = await chromium.launchPersistentContext(process.env.HOME + "/ai-browser-profiles/ai-profile", {
  channel: "chrome", headless: false, slowMo: 120, viewport: { width: 1440, height: 900 },
  args: ["--disable-blink-features=AutomationControlled", "--window-size=1440,900"],
});
await ctx.addInitScript(() => Object.defineProperty(navigator, "webdriver", { get: () => undefined }));
const page = await ctx.newPage();

// raw CDP session for render-blocking + priority (Network domain gives renderBlockingStatus)
const cdp = await ctx.newCDPSession(page);
await cdp.send("Network.enable");
await cdp.send("Performance.enable");
const netByReq = {};
cdp.on("Network.requestWillBeSent", (e) => { netByReq[e.requestId] = { url: e.request.url, blocking: e.request.isLinkPreload ? "preload" : undefined, initiator: e.initiator?.type, priority: e.request.initialPriority }; });
cdp.on("Network.responseReceived", (e) => { if (netByReq[e.requestId]) netByReq[e.requestId].renderBlocking = e.response.renderBlockingStatus; });

let lm = { x: 200, y: 200 };
async function humanMove(x, y) { const s = lm, st = Math.floor(rnd(18, 30)); const cx = (s.x + x) / 2 + rnd(-120, 120), cy = (s.y + y) / 2 + rnd(-90, 90); for (let i = 1; i <= st; i++) { const t = i / st; await page.mouse.move((1 - t) ** 2 * s.x + 2 * (1 - t) * t * cx + t * t * x + rnd(-1.5, 1.5), (1 - t) ** 2 * s.y + 2 * (1 - t) * t * cy + t * t * y + rnd(-1.5, 1.5)); await page.waitForTimeout(rnd(6, 22)); } await page.mouse.move(x, y); lm = { x, y }; }
const hasCh = () => page.frames().some(f => f.url().includes("challenges.cloudflare.com"));
async function solve() { const b64 = (await page.screenshot()).toString("base64"); const r = await fetch(OLLAMA, { method: "POST", body: JSON.stringify({ model: "qwen2.5vl:7b", prompt: 'Cloudflare verify-human checkbox. ONLY {"x":int,"y":int} or {"x":-1,"y":-1}.', images: [b64], stream: false, options: { temperature: 0 } }) }); const m = ((await r.json()).response || "").match(/\{[^}]*\}/); const loc = m ? JSON.parse(m[0]) : null; if (loc && loc.x > 0) { await humanMove(loc.x, loc.y); await page.waitForTimeout(rnd(120, 350)); await page.mouse.down(); await page.waitForTimeout(rnd(50, 120)); await page.mouse.up(); await page.waitForTimeout(4500); } }

await page.goto(URL, { waitUntil: "domcontentloaded", timeout: 45000 }).catch(e => log("nav", { err: e.message }));
await page.waitForTimeout(2500);
for (let i = 0; i < 3 && hasCh(); i++) { log("challenge", { i }); await solve(); }
await page.waitForTimeout(6000);

// Web Vitals + long tasks + per-resource render-blocking, from the page
const vitals = await page.evaluate(() => new Promise((resolve) => {
  const out = { longTasks: [], resources: [], paint: {} };
  for (const p of performance.getEntriesByType("paint")) out.paint[p.name] = Math.round(p.startTime);
  const nav = performance.getEntriesByType("navigation")[0];
  if (nav) out.nav = { ttfb: Math.round(nav.responseStart), domContentLoaded: Math.round(nav.domContentLoadedEventEnd), load: Math.round(nav.loadEventEnd), transfer: nav.transferSize };
  out.resources = performance.getEntriesByType("resource").map(r => ({ name: r.name, type: r.initiatorType, dur: Math.round(r.duration), blocking: r.renderBlockingStatus, transfer: r.transferSize, start: Math.round(r.startTime) }));
  try { new PerformanceObserver((l) => { for (const e of l.getEntries()) out.longTasks.push({ start: Math.round(e.startTime), dur: Math.round(e.duration) }); }).observe({ type: "longtask", buffered: true }); } catch {}
  setTimeout(() => resolve(out), 500);
}));

const metrics = await cdp.send("Performance.getMetrics").catch(() => ({ metrics: [] }));
const result = { url: URL, vitals, cdpMetrics: metrics.metrics, networkRenderBlocking: Object.values(netByReq) };
fs.writeFileSync(OUT, JSON.stringify(result, null, 2));
log("done", { resources: vitals.resources.length, longTasks: vitals.longTasks.length, fcp: vitals.paint["first-contentful-paint"], out: OUT });
await ctx.close();
console.log(`CDP: ${OUT}`);
