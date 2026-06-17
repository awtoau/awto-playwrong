// Ollama-vision click loop: screenshot -> local VLM locates the Cloudflare checkbox ->
// Playwright clicks those coordinates. Best-effort: Turnstile may still reject a programmatic
// click (it scores behaviour, not just the click), but this is a real vision-guided-click
// capability (feeds #7). It's our own site, so passing our own challenge is legitimate.
import { chromium } from "playwright";
import fs from "node:fs";

const URL = process.argv[2] || "https://www.powderhounds.com/Japan/Hokkaido/Furano.aspx";
const OLLAMA = "http://localhost:11434/api/generate";
const MODEL = process.env.PH_VLM || "qwen2.5vl:7b";

async function askVLM(pngBase64, w, h) {
  const prompt =
    `This is a ${w}x${h} screenshot of a web page showing a Cloudflare "Verify you are human" ` +
    `challenge with a checkbox. Return ONLY JSON {"x":<int>,"y":<int>} = pixel coordinates of the ` +
    `CENTER of the checkbox to click. If you cannot see a checkbox, return {"x":-1,"y":-1}.`;
  const r = await fetch(OLLAMA, {
    method: "POST",
    body: JSON.stringify({ model: MODEL, prompt, images: [pngBase64], stream: false,
      options: { temperature: 0 } }),
  });
  const j = await r.json();
  const m = (j.response || "").match(/\{[^}]*\}/);
  if (!m) return null;
  try { return JSON.parse(m[0]); } catch { return null; }
}

const ctx = await chromium.launchPersistentContext(
  process.env.HOME + "/ai-browser-profiles/ai-profile",
  { channel: "chrome", headless: false, slowMo: 200 }
);
const page = await ctx.newPage();
await page.goto(URL, { waitUntil: "load", timeout: 45000 }).catch(e => console.log("nav:", e.message));
await page.waitForTimeout(3000);

const hasChallenge = () => page.frames().some(f => f.url().includes("challenges.cloudflare.com"));

for (let attempt = 1; attempt <= 4 && hasChallenge(); attempt++) {
  const vp = page.viewportSize() || { width: 1280, height: 720 };
  const shot = await page.screenshot();
  const b64 = shot.toString("base64");
  console.log(`attempt ${attempt}: asking ${MODEL} to locate the checkbox...`);
  const loc = await askVLM(b64, vp.width, vp.height);
  console.log("  VLM says:", JSON.stringify(loc));
  if (loc && loc.x > 0 && loc.y > 0) {
    await page.mouse.move(loc.x - 20, loc.y - 10);   // a little human-ish movement first
    await page.waitForTimeout(300);
    await page.mouse.click(loc.x, loc.y);
    console.log(`  clicked (${loc.x},${loc.y})`);
    await page.waitForTimeout(4000);
  } else {
    console.log("  VLM couldn't locate a checkbox; waiting...");
    await page.waitForTimeout(3000);
  }
}

console.log(hasChallenge() ? "RESULT: still challenged (Turnstile likely rejected the programmatic click)"
                           : "RESULT: challenge passed!");
await ctx.close();
