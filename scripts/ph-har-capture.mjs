// Full HAR capture of a powderhounds page — the "lose nothing" network dataset (every request,
// all timing phases, headers, sizes, FAILED requests). Passes Cloudflare via trusted profile +
// human mouse + vision-click. Writes <out>/har-<slug>.har. Python loader ingests -> Postgres.
//
// Usage: node ph-har-capture.mjs <url> [outdir]
import { chromium } from "playwright";
import fs from "node:fs";
import path from "node:path";

const URL = process.argv[2] || "https://www.powderhounds.com/Canada/Fernie.aspx";
const OUTDIR = process.argv[3] || "/home/dan/git/awtoau/powderhounds/tmp";
const slug = URL.replace(/^https?:\/\//, "").replace(/[^a-z0-9]+/gi, "_").slice(0, 60);
const HAR = path.join(OUTDIR, `har-${slug}.har`);
const OLLAMA = "http://localhost:11434/api/generate";
const rnd = (a, b) => a + Math.random() * (b - a);
const log = (a, o = {}) => { const l = `${new Date().toISOString()} ${a} ${Object.entries(o).map(([k, v]) => k + "=" + JSON.stringify(v)).join(" ")}`; fs.appendFileSync(path.join(OUTDIR, "ph-har-capture.log"), l + "\n"); console.log(l); };

const ctx = await chromium.launchPersistentContext(process.env.HOME + "/ai-browser-profiles/ai-profile", {
  channel: "chrome", headless: false, slowMo: 120, viewport: { width: 1440, height: 900 },
  args: ["--disable-blink-features=AutomationControlled", "--window-size=1440,900"],
  recordHar: { path: HAR, content: "omit", mode: "full" },   // <-- full HAR, every request + timings
});
await ctx.addInitScript(() => Object.defineProperty(navigator, "webdriver", { get: () => undefined }));
const page = await ctx.newPage();
let lm = { x: 200, y: 200 };
async function humanMove(x, y) { const s = lm, st = Math.floor(rnd(18, 30)); const cx = (s.x + x) / 2 + rnd(-120, 120), cy = (s.y + y) / 2 + rnd(-90, 90); for (let i = 1; i <= st; i++) { const t = i / st; await page.mouse.move((1 - t) ** 2 * s.x + 2 * (1 - t) * t * cx + t * t * x + rnd(-1.5, 1.5), (1 - t) ** 2 * s.y + 2 * (1 - t) * t * cy + t * t * y + rnd(-1.5, 1.5)); await page.waitForTimeout(rnd(6, 22)); } await page.mouse.move(x, y); lm = { x, y }; }
const hasCh = () => page.frames().some(f => f.url().includes("challenges.cloudflare.com"));
async function solve() { const b64 = (await page.screenshot()).toString("base64"); const r = await fetch(OLLAMA, { method: "POST", body: JSON.stringify({ model: "qwen2.5vl:7b", prompt: 'Cloudflare verify-human checkbox. ONLY {"x":int,"y":int} or {"x":-1,"y":-1}.', images: [b64], stream: false, options: { temperature: 0 } }) }); const m = ((await r.json()).response || "").match(/\{[^}]*\}/); const loc = m ? JSON.parse(m[0]) : null; if (loc && loc.x > 0) { await humanMove(loc.x, loc.y); await page.waitForTimeout(rnd(120, 350)); await page.mouse.down(); await page.waitForTimeout(rnd(50, 120)); await page.mouse.up(); await page.waitForTimeout(4500); } }

const t0 = Date.now();
await page.goto(URL, { waitUntil: "domcontentloaded", timeout: 45000 }).catch(e => log("nav", { err: e.message }));
await page.waitForTimeout(2500);
for (let i = 0; i < 3 && hasCh(); i++) { log("challenge", { i }); await solve(); }
await page.waitForTimeout(6000);  // let async tags fire so they appear in the HAR
log("loaded", { ms: Date.now() - t0, title: (await page.title()).slice(0, 60) });
await ctx.close();   // HAR is flushed on context close
log("har_written", { path: HAR });
console.log(`HAR: ${HAR}`);
