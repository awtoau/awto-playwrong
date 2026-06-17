// Runtime third-party capture: load a powderhounds page in a real browser, let JS run,
// record EVERY network request -> reveals what GTM injects (Effective Measure etc.) that
// static HTML scans miss. Writes JSON to the powderhounds tmp dir.
import { chromium } from "playwright";
import fs from "node:fs";

const URL = process.argv[2] || "https://www.powderhounds.com/Japan/Hokkaido/Furano.aspx";
const OUT = "/home/dan/git/awtoau/powderhounds/tmp/runtime-capture.json";

const HEADLESS = process.env.PH_HEADLESS !== "0";  // PH_HEADLESS=0 -> visible window you can watch
// Use the REAL trusted profile (already passes Cloudflare) unless PH_PROFILE overrides.
const PROFILE = process.env.PH_PROFILE ||
  process.env.HOME + "/ai-browser-profiles/ai-profile";
const ctx = await chromium.launchPersistentContext(
  PROFILE,
  { channel: "chrome", headless: HEADLESS, slowMo: HEADLESS ? 0 : 300 }
);
const reqs = [];
const rawUrls = [];
const scripts = {};  // src_url -> {host, bytes, body} for third-party JS (the web-bug code)
// Attach via CDP-style page event BEFORE creating/navigating. Bind on context so all pages/frames.
ctx.on("request", (r) => {
  const url = r.url();
  rawUrls.push(url);
  try {
    const u = new URL(url);
    reqs.push({ host: u.host, path: u.pathname.slice(0, 60), type: r.resourceType(), method: r.method() });
  } catch {
    reqs.push({ host: url.slice(0, 40), path: "", type: r.resourceType(), method: r.method() });
  }
});
const page = await ctx.newPage();
// Grab the actual source of third-party scripts (the web-bug code) for later review.
page.on("response", async (resp) => {
  try {
    const r = resp.request();
    const url = resp.url();
    const u = new URL(url);
    if (u.host.includes("powderhounds.com")) return;            // third-party only
    if (r.resourceType() !== "script") return;
    if (scripts[url]) return;
    const body = await resp.text().catch(() => null);
    if (body && body.length) {
      scripts[url] = { host: u.host, bytes: body.length, body: body.slice(0, 500000) };
    }
  } catch {}
});
const consoleErrors = [];
page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text().slice(0, 200)); });
page.on("requestfailed", (r) => reqs.push({ host: (()=>{try{return new URL(r.url()).host}catch{return "?"}})(), failed: r.failure()?.errorText }));

const t0 = Date.now();
// 'load' not 'networkidle' — sites with beacons/long-poll never go idle.
await page.goto(URL, { waitUntil: "load", timeout: 45000 }).catch(e => console.log("nav note:", e.message));
// Cloudflare Turnstile handling.
// PH_MANUAL=1 -> headed, pause for the human to click the checkbox, then continue (reliable).
// Otherwise best-effort auto-click (often fails — challenges are automation-resistant).
const hasTurnstile = () => page.frames().some(f => f.url().includes("challenges.cloudflare.com"));
if (hasTurnstile()) {
  if (process.env.PH_MANUAL === "1") {
    console.log("\n>>> Cloudflare challenge detected. Please CLICK THE CHECKBOX in the browser window.");
    console.log(">>> Waiting up to 60s for you to pass it...\n");
    // wait until the challenge frame goes away (you solved it) or timeout
    const start = Date.now();
    while (hasTurnstile() && Date.now() - start < 60000) {
      await page.waitForTimeout(1000);
    }
    console.log(hasTurnstile() ? ">>> still challenged (timeout)" : ">>> challenge passed, continuing");
    await page.waitForTimeout(3000);
  } else {
    console.log("Turnstile detected — attempting auto-click (best-effort)...");
    try {
      const tsFrame = page.frames().find(f => f.url().includes("challenges.cloudflare.com"));
      await tsFrame.locator('input[type="checkbox"], .cb-c, label').first()
        .click({ timeout: 5000 }).catch(() => console.log("  (couldn't auto-click — try PH_MANUAL=1)"));
      await page.waitForTimeout(3000);
    } catch {}
  }
}
// let runtime tags fire (GTM injects async). Settle window, justified: GTM + tag chains
// typically fire within a few seconds of load; 6s covers the cascade without waiting forever.
await page.waitForTimeout(6000);
const loadMs = Date.now() - t0;

// group by host
const byHost = {};
for (const r of reqs) {
  const h = r.host || "?";
  byHost[h] = byHost[h] || { count: 0, types: new Set() };
  byHost[h].count++;
  if (r.type) byHost[h].types.add(r.type);
}
const firstParty = (h) => h.includes("powderhounds.com");
const result = {
  url: URL,
  loadMs,
  totalRequests: reqs.length,
  thirdPartyHosts: Object.entries(byHost)
    .filter(([h]) => !firstParty(h))
    .map(([h, v]) => ({ host: h, count: v.count, types: [...v.types] }))
    .sort((a, b) => b.count - a.count),
  firstPartyRequestCount: Object.entries(byHost).filter(([h]) => firstParty(h)).reduce((s,[,v])=>s+v.count,0),
  consoleErrors,
  scripts: Object.entries(scripts).map(([url, s]) => ({ url, ...s })),
};
fs.writeFileSync(OUT, JSON.stringify(result, null, 2));
console.log(`captured ${result.scripts.length} third-party script sources`);
console.log(`captured ${reqs.length} requests, ${result.thirdPartyHosts.length} third-party hosts, ${loadMs}ms`);
console.log("third-party hosts:");
for (const t of result.thirdPartyHosts) console.log(`  ${String(t.count).padStart(3)}x  ${t.host}  [${t.types.join(",")}]`);
console.log(`console errors: ${consoleErrors.length}`);
await ctx.close();
