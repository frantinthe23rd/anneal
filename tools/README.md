# Tools

## Is anything missing?

```bash
./anneal doctor        # or tools/doctor.py --brief, --json, --prereqs
```

Names every prerequisite, model and optional environment, says which are absent,
and prints the command that installs each one. Exit 1 if anything *required* is
missing, so `setup.sh` can gate on it. Stdlib only and run by `/usr/bin/python3`,
because it has to work on a machine where nothing has been installed yet.

## Tests

```bash
tools/test.sh          # unit always; acceptance too when a gateway answers on 8001
```

304 unit tests need nothing — no network, no models, no running server — and are
what CI runs. The 36 acceptance tests run against a live gateway and are
deliberately confined to the surface that wakes no model; anything that would
generate is skipped unless `ANNEAL_TEST_HEAVY=1`.

Two suites stand real HTTP handlers up in-process on an ephemeral port rather
than mocking them, with `SUPERVISOR_PORT` redirected to a closed port so a
mistake cannot reach the real gateway, and a final assertion that no service
epoch moved.

## Pruning old output

```bash
tools/prune.py --older-than 90              # what would go
tools/prune.py --older-than 90 --delete     # actually go
```

Nothing removes generated work automatically, and that is a decision rather than
an omission: generation is not deterministic, so a deleted take cannot be
regenerated and an automatic policy that removes the wrong one is unrecoverable.
`/health` reports `storage` — bytes and file counts per kind, plus free space on
the volume — so the problem is visible without anything acting on it.

Two safeguards, both tested. It deletes nothing without `--delete`. And it will
not orphan a record: Press stores its tracks and cover by path, so removing
those by age alone leaves an album in the Library that cannot play — referenced
files are skipped unless you pass `--include-pressed`, and the summary says how
many were spared.

## Linting the UI

```bash
tools/lint-ui.py       # silent and exit 0 when clean
```

Seven checks, no JavaScript toolchain: HTML well-formedness, duplicate ids, that
the inline script parses, dangling `$("id")` references, unresolved CSS custom
properties, external subresources, and light/dark token parity. Each one encodes
a fault this project has actually shipped.

## Cutting sprite sheets

`sprites.py` at the repo root finds the frames in a generated sheet, cuts them
out and removes the background. `POST /v1/sprites` runs it; it is also usable on
its own against any image.

```bash
$AIMUSIC_ROOT/tools-venv/bin/python sprites.py sheet.png            # just report the frames
$AIMUSIC_ROOT/tools-venv/bin/python sprites.py sheet.png --out ./frames
```

It runs under **tools-venv, not gen-venv**, and the gateway shells out to it
rather than importing it. Matting uses [rembg](https://github.com/danielgatis/rembg),
which pulls onnxruntime, and gen-venv is version-pinned because it serves the
models — coupling the two would mean an image-model upgrade could be blocked by
a background-removal dependency. `ANNEAL_SPRITE_PYTHON` overrides the
interpreter; without a usable one the endpoint answers 503 and says so.

```bash
$AIMUSIC_ROOT/tools-venv/bin/pip install rembg[cpu] pillow
```

`--no-model` mattes by colour distance instead. It is faster and needs nothing
installed, and it is the fallback rather than the default because it made a
white robot on a white sheet see-through — the background readable through its
head. Use it for high-contrast subjects only.

The first run downloads the u2net weights (~176 MB) into `~/.u2net`.

<!-- lint-prose: off -->
## Linting the prose

```bash
tools/lint-prose.py            # what a visitor reads
tools/lint-prose.py --all      # including code and tests
tools/lint-prose.py --strict   # exit 1 if anything in DROP survives
```

Counts words that characterise the writing instead of informing the reader.
It reports rather than fails, because every word on the list has a legitimate
use and the judgement is the point — a build that blocked on "deliberately"
would only teach people to write around it.

Two lists. **DROP** is near-always self-characterisation: *honest*, *frankly*,
*worth knowing*. "Experimental, and honestly so" was asking the reader for
credit for the word "experimental". **SUSPECT** depends on where it sits —
*deliberately* in a code comment stops the next person fixing what is not
broken, and in user-facing copy usually introduces a defence of a decision
nobody challenged.

It reads the way a visitor does: script blocks and HTML comments stripped from
`ui.html`, only `summary` and `description` taken from `openapi.json`. Three
review passes were needed to find this pattern by hand, and by then it was in
the README, the spec, the UI and the issue tracker.

<!-- lint-prose: on -->

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

Installed outside the repo, under `$AIMUSIC_ROOT`. `gen-venv` is deliberately
untouched — it is version-pinned and serves models, so tooling gets its own
environment.

```bash
source ./env.sh
export PLAYWRIGHT_BROWSERS_PATH="$AIMUSIC_ROOT/playwright-browsers"
"$AIMUSIC_ROOT/tools-venv/bin/python" tools/shot.py /tmp/anneal-shots
```

Then look at the PNGs. `shot.py` captures the front door, studio, chat, Press and
About in **both themes**, prints the `data-theme` each one actually resolved to,
and reports console errors.

To rebuild that environment from scratch:

```bash
./setup.sh --tools
```

That builds `tools-venv` with rembg *and* Playwright and installs Chromium, all
under `$AIMUSIC_ROOT`. By hand, if you want only part of it:

```bash
source ./env.sh
export PLAYWRIGHT_BROWSERS_PATH="$AIMUSIC_ROOT/playwright-browsers"
uv venv --python 3.12 "$AIMUSIC_ROOT/tools-venv"
uv pip install --python "$AIMUSIC_ROOT/tools-venv/bin/python" playwright
"$AIMUSIC_ROOT/tools-venv/bin/playwright" install chromium
```

About 735 MB, all under `$AIMUSIC_ROOT`: 139 MB venv, 554 MB browsers, the rest
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
