# Visual verification

`ui.html` is the one part of Anneal that cannot be verified by reading. Three
faults shipped that a screenshot would have caught immediately:

- the light theme painted near-black text onto the near-black hero and doc
  banners, making the lede, all five card titles and every banner paragraph
  invisible;
- the front door's forge strip never rendered on first load, under copy that
  says "the strip below is the truth about what is warm right now";
- the hero drew a second scrollbar that scrolled nothing.

None of them produced an error. All three were obvious in a picture.

## On this host

Installed outside the repo, on the external volume, because the internal disk is
nearly full. `gen-venv` is deliberately untouched — it is version-pinned and
serves models, so tooling gets its own environment.

```bash
export PLAYWRIGHT_BROWSERS_PATH=/Volumes/Storage/AIMusic/playwright-browsers
/Volumes/Storage/AIMusic/tools-venv/bin/python tools/shot.py /tmp/anneal-shots
```

Then look at the PNGs. `shot.py` captures the front door, studio, chat, Press and
About in **both themes**, prints the `data-theme` each one actually resolved to,
and reports console errors.

To rebuild that environment from scratch:

```bash
export UV_CACHE_DIR=/Volumes/Storage/AIMusic/uv-cache
export UV_PYTHON_INSTALL_DIR=/Volumes/Storage/AIMusic/uv-python
export PLAYWRIGHT_BROWSERS_PATH=/Volumes/Storage/AIMusic/playwright-browsers
uv venv --python 3.12 /Volumes/Storage/AIMusic/tools-venv
uv pip install --python /Volumes/Storage/AIMusic/tools-venv/bin/python playwright
/Volumes/Storage/AIMusic/tools-venv/bin/playwright install chromium
```

About 735 MB, all on `/Volumes/Storage`: 139 MB venv, 554 MB browsers, the rest
wheel cache. Chromium and the headless shell are both fetched; Playwright offers
no supported flag to take only the shell.

**The key gate will eat your screenshot.** A fresh browser profile has an empty
`localStorage`, and loopback carries no tailnet identity, so the UI puts its
blocking API-key modal over the page — every shot is of the modal. `shot.py`
reads the key from `env.local.sh` and seeds `anneal.key` before navigating. If
you write your own script, do the same.

## From another host

Any machine on the tailnet can drive the tailnet address instead, which needs no
key at all: `tailscale serve` stamps the caller's identity and Anneal trusts it.
`shoot.js` and `audit.js` are the Node equivalents, written for that setup.

```bash
node tools/shoot.js     # screenshots, both themes
node tools/audit.js     # measures rendered geometry, not CSS
```

`audit.js` is the one that checks the design scheme's rules against what the
browser actually computed: nothing interactive painted in `--faint`, no chrome
button under 36px, and the card components resolving to the same radius, shadow
and fill. It found a theme toggle pinned to 30px by its own rule, which reading
the CSS did not.

They need `playwright` from npm and expect `ANNEAL_URL` to be reachable; edit the
constant at the top if the hostname differs.
