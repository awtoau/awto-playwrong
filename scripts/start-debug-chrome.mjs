import { spawn } from "node:child_process";
import os from "node:os";
import path from "node:path";
import http from "node:http";

function resolveUserDataDir() {
  if (process.env.PLAYWRIGHT_USER_DATA_DIR) {
    return process.env.PLAYWRIGHT_USER_DATA_DIR;
  }
  return path.join(os.homedir(), "ai-browser-profiles", "ai-profile");
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

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function checkDebugEndpoint(port) {
  return new Promise((resolve) => {
    const req = http.get(`http://127.0.0.1:${port}/json/version`, (res) => {
      resolve(res.statusCode === 200);
      res.resume();
    });

    req.on("error", () => resolve(false));
    req.setTimeout(1500, () => {
      req.destroy();
      resolve(false);
    });
  });
}

async function main() {
  const chromeCommand = resolveChromeCommand();
  const userDataDir = resolveUserDataDir();
  const debugPort = process.env.CHROME_DEBUG_PORT || "9222";
  const urls = (process.env.PLAYWRIGHT_TARGET_URLS || "https://accounts.google.com/,https://www.apple.com/")
    .split(",")
    .map((url) => url.trim())
    .filter(Boolean);

  const args = [
    `--user-data-dir=${userDataDir}`,
    `--remote-debugging-port=${debugPort}`,
    "--remote-debugging-address=127.0.0.1",
    ...urls,
  ];

  console.log("Starting regular Chrome with remote debugging");
  console.log(`- command: ${chromeCommand}`);
  console.log(`- userDataDir: ${userDataDir}`);
  console.log(`- debugPort: ${debugPort}`);
  console.log(`- urls: ${urls.join(", ")}`);

  const child = spawn(chromeCommand, args, {
    detached: true,
    stdio: "ignore",
  });

  child.on("error", (error) => {
    console.error("Failed to start Chrome:", error.message);
    process.exitCode = 1;
  });

  child.unref();

  let ready = false;
  for (let i = 0; i < 6; i += 1) {
    await wait(500);
    ready = await checkDebugEndpoint(debugPort);
    if (ready) {
      break;
    }
  }

  if (ready) {
    console.log("Chrome started with CDP enabled. Sign in manually, then run npm run session:attach.");
    return;
  }

  console.log("Chrome started, but CDP port did not open.");
  console.log("Most likely cause: an existing Chrome instance reused the session and ignored debug flags.");
  console.log("Close all Chrome windows using this profile, then run npm run login:debug again.");
}

main().catch((error) => {
  console.error("Failed to start debug Chrome:", error);
  process.exitCode = 1;
});
