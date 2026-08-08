const { chromium } = require("playwright");
// Point at another host with ANNEAL_URL; loopback is the sensible default
// for a tool that renders this machine's own UI.
const URL = process.env.ANNEAL_URL || "http://127.0.0.1:8001/";
const OUT = process.env.HOME + "/anneal-shots/";

(async () => {
  const browser = await chromium.launch();
  const report = {};
  for (const theme of ["dark", "light"]) {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 },
      deviceScaleFactor: 2, colorScheme: theme });
    await ctx.addInitScript((t) => { try { localStorage.setItem("anneal.theme", t); } catch (e) {} }, theme);
    const p = await ctx.newPage();
    const errs = [];
    p.on("pageerror", (e) => errs.push(e.message));
    await p.goto(URL, { waitUntil: "networkidle" });
    await p.waitForTimeout(1200);
    await p.click("#btnStart");
    await p.waitForTimeout(700);
    await p.click("#btnLibrary");
    await p.waitForTimeout(2500);

    const res = await p.evaluate(() => {
      const faintish = [];
      const small = [];
      const clickable = "a[href], button, [role=tab], select, input, textarea";
      document.querySelectorAll(clickable).forEach((n) => {
        const r = n.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) return;          // hidden
        const cs = getComputedStyle(n);
        if (r.height < 36 && n.offsetParent !== null) {
          small.push((n.id || n.className || n.tagName) + " " + Math.round(r.height) + "px");
        }
        // Is anything interactive painted in the metadata colour?
        const faint = getComputedStyle(document.documentElement)
          .getPropertyValue("--faint").trim();
        if (faint && cs.color && toHex(cs.color) === faint.toLowerCase()) {
          faintish.push(n.id || n.className || n.tagName);
        }
      });
      function toHex(rgb) {
        const m = rgb.match(/\d+/g);
        if (!m) return rgb;
        return "#" + m.slice(0, 3).map((v) => (+v).toString(16).padStart(2, "0")).join("");
      }
      // Card shells should now agree.
      const shell = (sel) => {
        const n = document.querySelector(sel);
        if (!n) return null;
        const c = getComputedStyle(n);
        return { radius: c.borderRadius, shadow: c.boxShadow.slice(0, 24), bg: c.backgroundImage.slice(0, 30) };
      };
      return {
        smallControls: small.slice(0, 12), smallCount: small.length,
        interactiveOnFaint: [...new Set(faintish)],
        item: shell(".item"), album: shell(".album"),
      };
    });
    res.pageErrors = errs;
    report[theme] = res;
    await ctx.close();
  }
  console.log(JSON.stringify(report, null, 1));
  await browser.close();
})();
