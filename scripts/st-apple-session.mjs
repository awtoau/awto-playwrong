import { runPersistentSession } from "./st-persistent-session.mjs";
import os from "node:os";
import path from "node:path";

function defaultUserDataDir() {
  const home = os.homedir();
  const profileName = process.env.PLAYWRIGHT_PROFILE_NAME || "ai-profile";
  return path.join(home, "ai-browser-profiles", profileName);
}

const targetUrls = (process.env.PLAYWRIGHT_TARGET_URLS || "https://id.servicetitan.com/,https://www.apple.com/")
  .split(",")
  .map((url) => url.trim())
  .filter(Boolean);

await runPersistentSession({
  userDataDir: process.env.PLAYWRIGHT_USER_DATA_DIR || defaultUserDataDir(),
  targetUrls,
  channel: process.env.PLAYWRIGHT_CHANNEL || "chrome",
  headless: process.env.PLAYWRIGHT_HEADLESS === "true",
});