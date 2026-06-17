// ONE browser window, reused for ALL pages. Open once, solve Cloudflare once (via Ollama vision
// if needed), then navigate the SAME page through every URL — capturing runtime third-party
// requests + script source each time. Avoids window-spam and keeps the cf_clearance cookie alive
// in one live session. Writes one JSON per URL into the powderhounds tmp dir.
import { chromium } from "playwright";
import fs from "node:fs";

const URLS = process.argv.slice(2);
if (!URLS.length) { console.error("usage: node ph-capture-batch.mjs <url> [url...]"); process.exit(1); }

const OUTDIR = "/home/dan/git/awtoau/powderhounds/tmp";
const OLLAMA = "http://localhost:11434/api/generate";
const MODEL = process.env.PH_VLM || "qwen2.5vl:7b";

// Human-like cursor motion: Cloudflare scores mouse behaviour. Move in a curved, variable-speed
// path with micro-jitter + slight overshoot-and-correct, pause, then click. Not a teleport/straight line.
const rnd = (a, b) => a + Math.random() * (b - a);
async function humanMove(page, x, y) {
  const start = page._lastMouse || { x: rnd(100, 400), y: rnd(100, 300) };
  const steps = Math.floor(rnd(18, 30));
  // a control point off the straight line -> curved (bezier-ish) path
  const cx = (start.x + x) / 2 + rnd(-120, 120);
  const cy = (start.y + y) / 2 + rnd(-90, 90);
  for (let i = 1; i <= steps; i++) {
    const t = i / steps;
    // quadratic bezier
    const bx = (1 - t) ** 2 * start.x + 2 * (1 - t) * t * cx + t * t * x;
    const by = (1 - t) ** 2 * start.y + 2 * (1 - t) * t * cy + t * t * y;
    await page.mouse.move(bx + rnd(-1.5, 1.5), by + rnd(-1.5, 1.5));  // micro-jitter
    await page.waitForTimeout(rnd(6, 22));   // variable speed (faster mid-path, slower at ends)
  }
  // slight overshoot then correct back, like a real hand
  await page.mouse.move(x + rnd(-4, 4), y + rnd(-4, 4));
  await page.waitForTimeout(rnd(40, 120));
  await page.mouse.move(x, y);
  page._lastMouse = { x, y };
}

async function visionClick(page) {
  const vp = page.viewportSize() || { width: 1280, height: 720 };
  const b64 = (await page.screenshot()).toString("base64");
  const prompt = `This ${vp.width}x${vp.height} screenshot shows a Cloudflare "Verify you are human" `
    + `checkbox (a small square checkbox on the left of a wide light-grey/blue widget, mid-page). `
    + `Return ONLY JSON {"x":int,"y":int} for the CENTER of that checkbox, or {"x":-1,"y":-1} if none.`;
  const r = await fetch(OLLAMA, { method:"POST", body: JSON.stringify(
    { model: MODEL, prompt, images:[b64], stream:false, options:{temperature:0} }) });
  const m = ((await r.json()).response || "").match(/\{[^}]*\}/);
  const loc = m ? JSON.parse(m[0]) : null;
  if (loc && loc.x > 0) {
    await humanMove(page, loc.x, loc.y);     // <-- human-like approach
    await page.waitForTimeout(rnd(120, 350)); // settle before clicking
    await page.mouse.down(); await page.waitForTimeout(rnd(50, 120)); await page.mouse.up();
    console.log(`  vision-clicked (${loc.x},${loc.y}) with human motion`);
    await page.waitForTimeout(4500);
    return true;
  }
  return false;
}

const hasChallenge = (page) => page.frames().some(f => f.url().includes("challenges.cloudflare.com"));

// ONE persistent context, ONE page — reused for everything.
// FORCE HEADED: headless reliably gets flagged by Cloudflare. Reduce automation fingerprints.
const ctx = await chromium.launchPersistentContext(
  process.env.HOME + "/ai-browser-profiles/ai-profile",
  {
    channel: "chrome",
    headless: false,                 // headed always — headless fails Turnstile
    slowMo: 120,
    viewport: { width: 1440, height: 810 },   // 75% of 1920x1080 HD
    args: [
      "--disable-blink-features=AutomationControlled",  // hide the automation flag
      "--window-size=1440,810",
      "--window-position=120,80",
    ],
  }
);
// strip navigator.webdriver and other obvious bot tells before any page script runs
await ctx.addInitScript(() => {
  Object.defineProperty(navigator, "webdriver", { get: () => undefined });
  Object.defineProperty(navigator, "plugins", { get: () => [1, 2, 3] });
  Object.defineProperty(navigator, "languages", { get: () => ["en-US", "en"] });
});
const page = await ctx.newPage();

// Is the REAL page loaded (not the challenge)? Check for actual powderhounds content.
async function passed() {
  if (page.frames().some(f => f.url().includes("challenges.cloudflare.com"))) return false;
  // real pages have the powderhounds nav/footer; challenge pages don't
  const ok = await page.locator("text=Powderhounds").first().count().catch(() => 0);
  const title = await page.title().catch(() => "");
  return ok > 0 && !/just a moment|attention required|verify you are human/i.test(title);
}

// per-URL capture state (reset each navigation)
let reqs = [], scripts = {};
ctx.on("request", (r) => { try { const u=new URL(r.url()); reqs.push({host:u.host,type:r.resourceType()}); } catch{} });
page.on("response", async (resp) => {
  try { const u=new URL(resp.url()); if (u.host.includes("powderhounds.com")) return;
    if (resp.request().resourceType()!=="script" || scripts[resp.url()]) return;
    const b = await resp.text().catch(()=>null);
    if (b) scripts[resp.url()] = { host:u.host, bytes:b.length, body:b.slice(0,500000) };
  } catch{}
});

for (let i=0; i<URLS.length; i++) {
  const url = URLS[i];
  reqs = []; scripts = {};
  console.log(`\n[${i+1}/${URLS.length}] ${url}`);
  const t0 = Date.now();
  await page.goto(url, { waitUntil:"load", timeout:45000 }).catch(e=>console.log("  nav:",e.message));
  await page.waitForTimeout(2500);
  // solve challenge ONCE — after first solve the session is trusted for the rest
  for (let a=0; a<3 && hasChallenge(page); a++) {
    console.log(`  challenge present, vision attempt ${a+1}...`);
    await visionClick(page);
  }
  await page.waitForTimeout(5000);  // let GTM tags fire

  const byHost = {};
  for (const r of reqs) { const h=r.host||"?"; (byHost[h] ??= {count:0,types:new Set()}); byHost[h].count++; byHost[h].types.add(r.type); }
  const tp = Object.entries(byHost).filter(([h])=>!h.includes("powderhounds.com"))
    .map(([h,v])=>({host:h,count:v.count,types:[...v.types]})).sort((a,b)=>b.count-a.count);
  const result = { url, loadMs:Date.now()-t0, totalRequests:reqs.length, thirdPartyHosts:tp,
    scripts:Object.entries(scripts).map(([u,s])=>({url:u,...s})), consoleErrors:[] };
  const fn = `${OUTDIR}/capture-${i+1}.json`;
  fs.writeFileSync(fn, JSON.stringify(result,null,2));
  const real = tp.filter(h=>!h.host.includes("cloudflare")).length;
  console.log(`  -> ${tp.length} 3p hosts (${real} non-cloudflare), ${result.scripts.length} scripts -> ${fn}`);
}

console.log("\ndone (one window, reused).");
await ctx.close();
