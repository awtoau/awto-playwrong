import { spawn } from "node:child_process";
import os from "node:os";
import path from "node:path";

function resolveUserDataDir() {
  if (process.env.PLAYWRIGHT_USER_DATA_DIR) {
    return process.env.PLAYWRIGHT_USER_DATA_DIR;
  }

  const profileName = process.env.PLAYWRIGHT_PROFILE_NAME || "ai-profile";
  return path.join(os.homedir(), "ai-browser-profiles", profileName);
}

function resolveUrls() {
  return (process.env.PLAYWRIGHT_TARGET_URLS || "https://accounts.google.com/,https://www.apple.com/")
    .split(",")
    .map((url) => url.trim())
    .filter(Boolean);
}

function resolveChromeCommand() {
  if (process.platform === "win32") {
    return "chrome";
  }

  if (process.platform === "darwin") {
    return "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  }

  return process.env.CHROME_BIN || "google-chrome";
}

function main() {
  const userDataDir = resolveUserDataDir();
  const urls = resolveUrls();
  const chromeCommand = resolveChromeCommand();

  const args = [`--user-data-dir=${userDataDir}`, ...urls];

  console.log("Opening regular Chrome for manual secure sign-in");
  console.log(`- command: ${chromeCommand}`);
  console.log(`- userDataDir: ${userDataDir}`);
  console.log(`- urls: ${urls.join(", ")}`);

  const child = spawn(chromeCommand, args, {
    detached: true,
    stdio: "ignore",
  });

  child.on("error", (error) => {
    console.error("Failed to launch Chrome:", error.message);
    console.error("Set CHROME_BIN to your browser binary path and try again.");
    process.exitCode = 1;
  });

  child.unref();
  console.log("Chrome launched. Complete sign-in there, close it, then run npm run session.");
}

main();
