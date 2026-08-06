#!/usr/bin/env python
"""Screenshot the running UI, on this host, in both themes.

Visual verification is not optional for this interface. Reading the CSS missed
a light theme that painted near-black text onto a near-black plate, a forge
strip that never rendered on first load, and a scrollbar that scrolled nothing.
All three were obvious in a screenshot.

    export PLAYWRIGHT_BROWSERS_PATH=/Volumes/Storage/AIMusic/playwright-browsers
    /Volumes/Storage/AIMusic/tools-venv/bin/python tools/shot.py [outdir]

See tools/README.md for how that environment was built.
"""
import json
import os
import re
import sys

URL = os.environ.get("ANNEAL_URL", "http://127.0.0.1:8001/")
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/anneal-shots"
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def api_key():
    """The key the browser would otherwise stop and ask for.

    A fresh browser profile has an empty localStorage, so loopback — which
    carries no tailnet identity — puts the key gate over the whole page and
    every screenshot is of that modal. Seeding the key before navigation is the
    difference between photographing the app and photographing its doorbell.
    """
    try:
        with open(os.path.join(HERE, "env.local.sh")) as fh:
            m = re.search(r'ACESTEP_API_KEY="([^"]+)"', fh.read())
            return m.group(1) if m else None
    except OSError:
        return None


def main():
    from playwright.sync_api import sync_playwright

    os.makedirs(OUT, exist_ok=True)
    key = api_key()
    problems = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for theme in ("dark", "light"):
            ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                      device_scale_factor=2, color_scheme=theme)
            if key:
                ctx.add_init_script(
                    "try { localStorage.setItem('anneal.key', %s); } catch (e) {}"
                    % json.dumps(key))
            ctx.add_init_script(
                "try { localStorage.setItem('anneal.theme', %s); } catch (e) {}"
                % json.dumps(theme))

            page = ctx.new_page()
            page.on("pageerror", lambda e, t=theme: problems.append("%s pageerror: %s" % (t, e)))
            page.on("console", lambda m, t=theme:
                    problems.append("%s console: %s" % (t, m.text)) if m.type == "error" else None)
            page.goto(URL, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(1200)

            applied = page.get_attribute("html", "data-theme")
            print("%s: data-theme=%s" % (theme, applied))
            page.screenshot(path=os.path.join(OUT, "front-%s.png" % theme))

            page.click("#btnStart")
            page.wait_for_timeout(800)
            page.screenshot(path=os.path.join(OUT, "studio-%s.png" % theme))

            for tab, name in (("chat", "chat"), ("press", "press")):
                page.click('.tab[data-mode="%s"]' % tab)
                page.wait_for_timeout(500)
                page.screenshot(path=os.path.join(OUT, "%s-%s.png" % (name, theme)))

            page.click('#subnav .tab[data-page="about"]')
            page.wait_for_timeout(600)
            page.screenshot(path=os.path.join(OUT, "about-%s.png" % theme))
            ctx.close()
        browser.close()

    print("\n".join(problems) if problems else "no console errors")
    print("written to " + OUT)


if __name__ == "__main__":
    main()
