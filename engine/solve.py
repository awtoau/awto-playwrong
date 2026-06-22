"""nd_solve.py — solve Cloudflare Turnstile with nodriver (vendored, patched for 3.14t).

nodriver is purpose-built for this: real CDP (no webdriver tells), anti-detect, and a dedicated
tab.cf_verify() + iframe-aware find('verify you are human'). Once it passes, we have a cleared
session whose cookies/HTML we can hand to the rip pipeline.

Usage: PYTHONPATH=vendor .venv/bin/python scripts/nd_solve.py [url]
"""
import sys, os, asyncio
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "vendor"))
import nodriver as uc

URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.powderhounds.com/Japan/Hokkaido/Niseko.aspx"

async def main():
    # headed real Chrome; nodriver handles the anti-detect launch flags itself.
    browser = await uc.start(headless=False)
    tab = await browser.get(URL)
    print("loaded:", URL)
    # give the challenge a moment to render, then try nodriver's CF verify
    await tab.sleep(4)
    title = await tab.evaluate("document.title")
    print("title:", title)
    passed = False
    # No opencv needed: nodriver's find() searches INSIDE iframes (the cross-origin checkbox our
    # own harness couldn't reach). Find the Turnstile checkbox text/element and click it.
    try:
        el = await tab.find("verify you are human", best_match=True, timeout=15)
        if el:
            print("found turnstile element:", (el.text or "")[:40])
            await el.mouse_click()
            print("clicked it")
            await tab.sleep(5)
        else:
            print("checkbox element not found via find()")
    except Exception as e:
        print("find/click error:", repr(e)[:160])
    # check result — wait for the real page to settle after the redirect
    await tab.sleep(3)
    title = await tab.evaluate("document.title")
    html = await tab.get_content()
    passed = "just a moment" not in title.lower() and "verify you are human" not in html.lower()
    print(f"title now: {title!r}  html={len(html)}  PASSED={passed}")
    if passed:
        # cleared — save cookies + html for the rip pipeline
        cookies = await browser.cookies.get_all()
        print(f"cleared session: {len(cookies)} cookies")
        open(os.path.join(os.path.dirname(__file__), "..", "tmp", "nd-cleared.html"), "w").write(html)
        print("saved tmp/nd-cleared.html")
    await tab.sleep(2)
    browser.stop()

if __name__ == "__main__":
    uc.loop().run_until_complete(main())
