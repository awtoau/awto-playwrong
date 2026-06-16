import { chromium } from "playwright";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

function defaultUserDataDir() {
  const home = os.homedir();
  const profileName = process.env.PLAYWRIGHT_PROFILE_NAME || "ai-profile";
  return path.join(home, "ai-browser-profiles", profileName);
}

async function main() {
  await runPersistentSession({
    userDataDir: process.env.PLAYWRIGHT_USER_DATA_DIR || defaultUserDataDir(),
    targetUrls: (process.env.PLAYWRIGHT_TARGET_URLS || "https://www.apple.com/,https://accounts.google.com/")
      .split(",")
      .map((url) => url.trim())
      .filter(Boolean),
    channel: process.env.PLAYWRIGHT_CHANNEL || "chrome",
    headless: process.env.PLAYWRIGHT_HEADLESS === "true",
  });
}

export async function runPersistentSession({ userDataDir, targetUrls, channel, headless }) {
  console.log("Launching persistent Playwright context");
  console.log(`- userDataDir: ${userDataDir}`);
  console.log(`- channel: ${channel}`);
  console.log(`- headless: ${headless}`);
  console.log(`- urls: ${targetUrls.join(", ")}`);

  const context = await chromium.launchPersistentContext(userDataDir, {
    channel,
    headless,
  });

  const firstPage = context.pages()[0] || (await context.newPage());
  await firstPage.goto(targetUrls[0], { waitUntil: "domcontentloaded" });

  for (const url of targetUrls.slice(1)) {
    const page = await context.newPage();
    await page.goto(url, { waitUntil: "domcontentloaded" });
  }

  console.log("Browser ready. Log in manually on the opened sites, then keep reusing the same profile path in future sessions.");
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    const message = String(error?.message || "");
    if (message.includes("Opening in existing browser session")) {
      console.error("Profile is already in use by another Chrome/Chromium process.");
      console.error("Close the other browser using this profile, then run npm run session again.");
    }
    if (message.includes("not be secure") || message.includes("may not be secure")) {
      console.error("Google blocked sign-in from the automated browser context.");
      console.error("Run npm run login once to sign in with regular Chrome using the same shared profile.");
    }
    console.error("Failed to launch persistent session:", error);
    process.exitCode = 1;
  });
}
