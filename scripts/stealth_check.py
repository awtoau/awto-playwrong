"""stealth_check.py — what a CDP-leak bot detector sees of the engine (#41).

- Loads https://bot-detector.rebrowser.net/ (tests for Runtime.enable, navigator.webdriver,
  sourceURL and main-world leaks) in a tagged tab on an isolated engine.
- Pass 1 reads the verdicts with the `text` op only (no JS in the page). Pass 2 then runs the
  calls the page asks for through the `js` op, which is what the engine itself does to pages.
- Prints every verdict. Exit 1 on any red outside KNOWN_RED.

    python scripts/stealth_check.py                 # isolated :8739, own Xvfb, stopped after

Results: tmp/logs/stealth-check.log
"""
import argparse
import html as _html
import os
import re
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from engine import connect  # noqa: E402

LOG = os.path.join(REPO, "tmp", "logs", "stealth-check.log")
URL = "https://bot-detector.rebrowser.net/"
# Red verdicts that are not the engine's doing: name -> (why, predicate on the verdict's note).
# Anything else red fails the check.
KNOWN_RED = {
    # Chrome on a Wayland desktop lists canShare/share as own navigator properties; the test wants
    # none. Only that form is excused: "navigator.webdriver = true" is a real leak (#41: port 0).
    "navigatorWebdriver": ("canShare/share listed (Chrome on a Wayland desktop)",
                           lambda note: "canShare" in note and "webdriver = true" not in note),
    # Runtime.evaluate runs in the page's main world; every engine op does. Isolated-world
    # evaluation would hide it (nodriver did the same).
    "mainWorldExecution": ("engine evaluates in the main world", lambda note: True),
}
VERDICT = re.compile(r"(🟢|🔴|🟡|⚪️?)\s*([A-Za-z]+)\s+[\d.]+ ms\s+(.*?)(?=🟢|🔴|🟡|⚪|\bJSON\b|$)")
# Detector settle after load: its async checks (useragent) finished by 0.5 s on the shared engine.
SETTLE = 2.0


def say(*a):
    line = " ".join(str(x) for x in a)
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def verdicts(port, tag):
    raw = connect.call("text", port=port, tab=tag)["html"]
    text = _html.unescape(re.sub(r"<[^>]+>", " ", raw))
    text = re.sub(r"\s+", " ", text)
    return {name: (mark, note.strip()) for mark, name, note in VERDICT.findall(text)}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8739, help="isolated engine port (never 8731)")
    ap.add_argument("--headed", action="store_true", help="show the browser on your desktop")
    ap.add_argument("--keep", action="store_true", help="leave the engine running")
    a = ap.parse_args()
    if a.port == 8731:
        sys.exit("refusing to run on 8731: use an isolated engine")
    if not a.headed:
        os.environ["PH_DISPLAY"] = "xvfb"
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    open(LOG, "w").close()
    tag = "stealth-check"
    bad = []
    try:
        connect.ensure(a.port, want_browser=True)
        connect.call("newtab", port=a.port, url="about:blank", tag=tag)
        connect.call("goto", port=a.port, url=URL, tab=tag, timeout=60)
        time.sleep(SETTLE)
        for phase in ("passive (no JS from us)", "after the engine's js op"):
            if phase.startswith("after"):
                for expr in ("window.dummyFn()", "document.getElementById('detections-json') && 1",
                             "document.getElementsByClassName('div').length"):
                    connect.call("js", port=a.port, expr=expr, tab=tag)
                time.sleep(SETTLE)
            v = verdicts(a.port, tag)
            say(f"\n── {phase}: {len(v)} verdicts")
            for name, (mark, note) in v.items():
                known = mark == "🔴" and name in KNOWN_RED and KNOWN_RED[name][1](note)
                tail = f"  [known: {KNOWN_RED[name][0]}]" if known else ""
                say(f"  {mark} {name:20} {note[:90]}{tail}")
                if mark == "🔴" and not known:
                    bad.append(f"{phase}: {name}")
            if not v:
                bad.append(f"{phase}: no verdicts parsed (page changed?)")
    finally:
        try:
            connect.call("closetab", port=a.port, tag=tag)
            if not a.keep:
                connect.call("shutdown", port=a.port, timeout=15.0)
        except Exception as e:
            say(f"  cleanup: {str(e)[:80]}")
    say("\nFAIL: " + "; ".join(bad) if bad else "\nOK: no unexpected red")
    say(f"log: {LOG}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
