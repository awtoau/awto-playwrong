import { chromium } from "playwright";

async function main() {
  const debugPort = process.env.CHROME_DEBUG_PORT || "9222";
  const endpoint = `http://127.0.0.1:${debugPort}`;
  const targetUrls = (process.env.PLAYWRIGHT_TARGET_URLS || "https://accounts.google.com/,https://www.apple.com/")
    .split(",")
    .map((url) => url.trim())
    .filter(Boolean);

  console.log("Attaching Playwright to existing Chrome over CDP");
  console.log(`- endpoint: ${endpoint}`);
  console.log(`- urls: ${targetUrls.join(", ")}`);

  const browser = await chromium.connectOverCDP(endpoint);
  const context = browser.contexts()[0] || (await browser.newContext());

  const firstPage = context.pages()[0] || (await context.newPage());
  await firstPage.goto(targetUrls[0], { waitUntil: "domcontentloaded" });

  for (const url of targetUrls.slice(1)) {
    const page = await context.newPage();
    await page.goto(url, { waitUntil: "domcontentloaded" });
  }

  console.log("Attached session ready.");
  await browser.close();
}

main().catch((error) => {
  console.error("Failed to attach to Chrome:", error);
  console.error("Make sure Chrome is running with npm run login:debug first.");
  process.exitCode = 1;
});
