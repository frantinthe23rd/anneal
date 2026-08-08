const { chromium } = require("playwright");
// Point at another host with ANNEAL_URL; loopback is the sensible default
// for a tool that renders this machine's own UI.
const URL = process.env.ANNEAL_URL || "http://127.0.0.1:8001/";
const OUT = process.env.HOME + "/anneal-shots/";

(async () => {
  const browser = await chromium.launch();
  const errors = [];

  async function open(theme) {
    const ctx = await browser.newContext({
      viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2,
      colorScheme: theme === "light" ? "light" : "dark",
    });
    await ctx.addInitScript((t) => {
      try { localStorage.setItem("anneal.theme", t); } catch (e) {}
    }, theme);
    const p = await ctx.newPage();
    p.on("console", (m) => { if (m.type() === "error") errors.push(theme + ": " + m.text()); });
    p.on("pageerror", (e) => errors.push(theme + " PAGEERROR: " + e.message));
    await p.goto(URL, { waitUntil: "networkidle" });
    await p.waitForTimeout(1200);
    return { ctx, p };
  }

  for (const theme of ["dark", "light"]) {
    const { ctx, p } = await open(theme);

    // What theme did the page actually resolve to?
    const applied = await p.getAttribute("html", "data-theme");
    console.log(theme + ": data-theme=" + applied);

    await p.screenshot({ path: OUT + "01-front-" + theme + ".png" });

    const header = await p.$("header");
    if (header) await header.screenshot({ path: OUT + "02-header-" + theme + ".png" });

    await p.click('.hero .card[data-go="music"]');
    await p.waitForTimeout(900);
    await p.screenshot({ path: OUT + "03-studio-" + theme + ".png" });

    await p.click('#subnav button[data-page="about"]');
    await p.waitForTimeout(700);
    await p.screenshot({ path: OUT + "04-about-" + theme + ".png" });

    await p.click('#subnav button[data-page="studio"]');
    await p.click('.tab[data-mode="chat"]');
    await p.waitForTimeout(600);
    await p.screenshot({ path: OUT + "05-chat-" + theme + ".png" });

    await p.click('.tab[data-mode="press"]');
    await p.click("#btnLibrary");
    await p.waitForTimeout(2000);
    await p.screenshot({ path: OUT + "06-library-" + theme + ".png" });

    await ctx.close();
  }

  console.log(errors.length ? "CONSOLE ERRORS:\n" + errors.join("\n") : "no console errors");
  await browser.close();
})();
