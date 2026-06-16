// Drive GCP + Play Console using the persistent ai-profile (already Google-logged-in).
// Phase 1 (this run): launch persistent context, open GCP console, report whether we
// are signed in and what account. We do NOT click anything destructive yet — we first
// confirm the session is alive and dump page state so the human can see it worked.
//
// Output: logs to stdout (captured to awto-l8-app/logs/play_provision.log).
// Screenshots: awto-l8-app/tmp/play_*.png so the user can eyeball each step.

import { chromium } from "playwright";
import os from "node:os";
import path from "node:path";

const PROFILE = process.env.PLAYWRIGHT_USER_DATA_DIR
  || path.join(os.homedir(), "ai-browser-profiles", "ai-profile");
const SHOT_DIR = "/home/dan/git/awto-l8-app/tmp";

function log(...a) { console.log(new Date().toISOString(), ...a); }

async function main() {
  log("Launching persistent context:", PROFILE);
  const context = await chromium.launchPersistentContext(PROFILE, {
    channel: "chrome",
    headless: false,
    viewport: { width: 1400, height: 1000 },
  });

  const page = context.pages()[0] || await context.newPage();

  log("Navigating to GCP console...");
  await page.goto("https://console.cloud.google.com/", { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.waitForLoadState("networkidle", { timeout: 30000 }).catch(() => {});

  const url = page.url();
  const title = await page.title().catch(() => "(no title)");
  log("Landed URL:", url);
  log("Page title:", title);

  // Detect signed-in vs sign-in-wall.
  const signedOut = /accounts\.google\.com|ServiceLogin|signin/i.test(url);
  log("Signed in?", signedOut ? "NO — hit a sign-in wall" : "LIKELY YES");

  // Try to read the active account email from the top-right account button aria-label.
  let account = "(unknown)";
  try {
    const btn = await page.locator('[aria-label*="@"], a[aria-label*="Google Account"]').first();
    const lbl = await btn.getAttribute("aria-label", { timeout: 5000 });
    if (lbl) account = lbl;
  } catch {}
  log("Account hint:", account);

  await page.screenshot({ path: `${SHOT_DIR}/play_01_gcp_home.png`, fullPage: false }).catch(() => {});
  log("Screenshot:", `${SHOT_DIR}/play_01_gcp_home.png`);

  log("PHASE 1 DONE — leaving browser OPEN for inspection. Close it manually when done.");
  // Intentionally do NOT close context, so the window stays for the user / next phase.
}

main().catch((e) => {
  console.error("PROVISION ERROR:", e?.message || e);
  process.exitCode = 1;
});
