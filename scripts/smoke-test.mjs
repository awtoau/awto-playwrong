import { chromium } from "playwright";

async function main() {
  const executablePath = chromium.executablePath();
  if (typeof executablePath !== "string" || executablePath.length === 0) {
    throw new Error("Unable to resolve Playwright Chromium executable path");
  }

  // This smoke test intentionally avoids launching Chromium so it passes
  // even when browser binaries have not been installed yet.
  console.log("Playwright smoke test passed: package import is healthy");
  console.log(`Chromium executable path: ${executablePath}`);
}

main().catch((error) => {
  console.error("Playwright smoke test failed:", error);
  process.exitCode = 1;
});
