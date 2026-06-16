# Playwright Persistent Profile Starter

## Why This Exists
This workspace is set up to reuse one dedicated Chrome/Chromium profile for Playwright automation.

The goal is to persist authentication across VS Code sessions and workspaces (cookies, localStorage, site data) with a simple default flow for Apple and Google.

## Core Rule
Use Playwright persistent context mode, not temporary contexts.

Use:
- `chromium.launchPersistentContext(userDataDir, options)`

Do not use:
- `browser.newContext()` for login persistence use cases

## Shared Profile Location
Pick a profile path outside the repo/workspace so multiple VS Code windows can reuse it.

Suggested defaults:
- Linux/macOS: `~/ai-browser-profiles/ai-profile`
- Windows: `C:\Users\<you>\ai-browser-profiles\ai-profile`

Use a dedicated AI profile only. Do not point Playwright at your personal daily-use Chrome profile.

## Included Script
The script `scripts/st-persistent-session.mjs` launches Playwright using a persistent profile and opens Apple + Google by default:

```js
import { chromium } from "playwright";

const context = await chromium.launchPersistentContext(userDataDir, {
  channel: "chrome",
  headless: false,
});
```

It reads configuration from environment variables:
- `PLAYWRIGHT_USER_DATA_DIR`
- `PLAYWRIGHT_PROFILE_NAME` (used when `PLAYWRIGHT_USER_DATA_DIR` is not set)
- `PLAYWRIGHT_TARGET_URLS` (comma-separated URLs)
- `PLAYWRIGHT_CHANNEL`
- `PLAYWRIGHT_HEADLESS`

Defaults are defined in the script and `.env.example`.

## Requirements
- Node.js and npm installed
- Playwright package dependencies installed:
  - `npm install`
- Playwright browser binaries installed (required before launching sessions):
  - `npx playwright install`
- Linux only (if browser startup fails with missing native libraries):
  - `sudo npx playwright install-deps`

## Current Checklist (2026-05-25)
Done:
- [x] Smoke test script exists and is wired to `npm test`
- [x] `npm test` passes in this workspace
- [x] Shared profile path usage is documented (`~/ai-browser-profiles/ai-profile`)
- [x] ST + Apple startup flow launches successfully with:
  - `PLAYWRIGHT_TARGET_URLS=https://id.servicetitan.com/,https://www.apple.com/`
- [x] Playwright browser binaries installed for local execution
- [x] Dedicated ST + Apple convenience script exists: `npm run st:apple`

Not done yet:
- [ ] Confirm manual sign-in persistence for both Apple and ServiceTitan in headed mode
- [ ] Add full end-to-end automated tests with `@playwright/test`
- [ ] Add CI workflow for automated test execution

## Quick Start
1. Install dependencies:
	- `npm install`
2. Install Playwright browser binaries:
	- `npx playwright install`
3. Optionally copy `.env.example` into your own environment setup.
4. Do first-time sign-in in regular Chrome (recommended for Google/Apple):
	- `npm run login`
5. Start persistent Playwright browser session:
	- `npm run session`

If Google blocks sign-in, use the CDP attach flow:
- `npm run login:debug`
- Sign in manually in that Chrome window
- `npm run session:attach`

If `session:attach` cannot connect to `127.0.0.1:9222`, close all Chrome windows for that profile and run `npm run login:debug` again.

## First Login Flow
1. Run `npm run login`.
2. Log in manually on Apple and Google in the regular Chrome window.
3. Enable remember-me if offered.
4. Close that Chrome window when done.
5. Run `npm run session` for automated reuse with Playwright.
6. Re-run later with the same `PLAYWRIGHT_USER_DATA_DIR` to reuse auth.

## Site Selection
You can customize which tabs open by setting `PLAYWRIGHT_TARGET_URLS`:

Example:
- `PLAYWRIGHT_TARGET_URLS=https://www.apple.com/,https://accounts.google.com/`

## Profile Lock Troubleshooting
If you see `Opening in existing browser session`, the profile directory is already locked by another Chrome/Chromium process.

Use one of these approaches:
- Close the existing browser process using that profile and retry `npm run session`.

## Google Sign-In Warning
If Google shows `This browser or app may not be secure`, use the regular Chrome login flow:
- `npm run login`

This launches non-automated Chrome with the same shared profile directory so authentication can be stored. After login, close Chrome and run `npm run session`.

If that still fails, use:
- `npm run login:debug` (regular Chrome + remote debugging)
- `npm run session:attach` (Playwright attaches to existing Chrome via CDP)

## Apple and Google Browser Protection
Some Apple and Google authentication flows apply browser/app protection checks that can reject fully automated contexts.

This starter handles that by using regular Chrome for manual authentication first, then reusing the same profile with Playwright:
- `npm run login` for manual sign-in in regular Chrome
- `npm run session` for Playwright persistent-context reuse
- `npm run login:debug` + `npm run session:attach` for stricter Google flows

This pattern improves reliability while keeping automation local and profile-based.

## Safety and Reliability Notes
- One profile path should have one active browser writer at a time.
- Avoid running multiple Playwright sessions concurrently against the same profile directory.
- Keep the profile directory stable and outside project folders.

## Session Log
- 2026-05-19: Playwright smoke test confirmed in VS Code browser tools.
- 2026-05-19: Added persistent shared-profile starter script and usage docs.
- 2026-05-19: Generalized session launcher for Apple + Google + ST multi-site login reuse.
- 2026-05-19: Simplified to single-profile, Apple + Google default workflow.
