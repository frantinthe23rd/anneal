# Anneal — working notes

Local, on-demand generation on a Mac mini M4 (16 GB): music, speech, images and
text behind one gateway, reachable over a tailnet. `README.md` explains what it
is; this file is about working on it.

## Layout

| | |
| --- | --- |
| Repo / scripts | `/Users/jon/dev/AIMusic` — small files only |
| Models, venvs, outputs, logs | `/Volumes/Storage/AIMusic` — the internal disk is nearly full |
| Upstream ACE-Step checkout | `/Volumes/Storage/AIMusic/ACE-Step-1.5` — **not part of this repo** |

`supervisor.py` is the gateway: owns port 8001, routes to backends declared in
`services.py`, and manages their lifecycle. `builder.py` is Press. `jobstore.py`
and `outputs.py` are durability and the library. Backends: `speech_server.py`,
`image_server.py`; music and text are upstream servers we launch.

## Running it

```bash
./start-api.sh     # applies upstream patches, starts the gateway, configures tailscale serve
./stop-api.sh      # stops everything, releases memory
./monitor.py       # paging and pressure during a generation
./update.sh --check --models --deps --smoke
```

**`ui.html` is read from disk per request — UI changes need no restart.** Any
change to `supervisor.py`, `services.py` or `builder.py` does. `assets/` is
served by the gateway (backdrop, favicon, and the two vendored browser
libraries in `assets/vendor/`) so the page fetches nothing externally; adding a
new file type there means touching the content-type map in `supervisor.py`. Forgetting this
has repeatedly produced "my fix didn't work" when the old module was still
loaded; if behaviour doesn't match the code, check the process start time first.

## Conventions

- **Verify against the running system, not by reading.** Reproduce a bug before
  fixing it and confirm the fix afterwards. Several hypotheses here looked
  obviously right and were wrong.
- **`ui.html` needs a screenshot, not a read.** A light theme that painted
  near-black text onto a near-black plate, a forge strip that never rendered on
  first load, and a scrollbar that scrolled nothing all shipped without raising
  an error. `tools/README.md` has the one-line command; `tools/audit.js`
  measures rendered geometry when the question is "does this obey the design
  scheme" rather than "does it look right".
- **`tools/lint-ui.py` before committing `ui.html`.** Stdlib plus the system
  JavaScriptCore, no Node — deliberately, and it must not become the reason to
  install one. It catches the classes that raise nothing in a browser: a
  `$("id")` left bound to markup that was deleted, `var(--x)` with no
  definition, a colour token declared in the dark `:root` and not the light one,
  a duplicate id, a syntax error in the inline script, an `http(s)` subresource
  breaking the "fetches nothing externally" promise. It does not replace the
  screenshot; the two see different halves of the file.
- **API endpoints are written test-first.** Before the handler exists: the
  request shape, the `{data, code, error}` envelope it returns, what an
  unauthenticated call gets, and each failure mode with its status. Not a
  general rule about tests — specifically endpoints, because they are the part
  other people build against, and three have already got past everything else.
  `/v1/press/cancel` shipped and was documented nowhere — not the spec, not the
  guide, not the endpoint tables. `init_image` and `retention` shipped and
  appeared in no spec, no guide and no page; outside the server they existed
  only in the UI's own JavaScript. `JobStore.prune()` existed and nothing called
  it (#27 — it has a caller now, and takes its dependent rows with it).
  A test written first catches all three, because you cannot write one
  without naming the path, the payload and the response — and once they are
  named, the difference between that and `openapi.json` is something you can
  see. `tools/test.sh` runs the suite in `tests/`.
- **Docs change with code.** `README.md`, `INTEGRATION.md` and `openapi.json`
  are part of the change, not follow-up. An endpoint that gains a parameter,
  a status code or a whole path updates `openapi.json` and `INTEGRATION.md` in
  the same commit — "document it next" is how the three above got out. Correct
  earlier claims that turn out wrong rather than quietly moving on.
- **Commit messages explain why**, including what was measured and what was
  rejected. Several decisions here only make sense with the measurement attached.
- **Secrets**: `env.local.sh` is gitignored and generated on first run. Check
  `git diff --cached` before every commit.
- **Upstream fixes go in `patches/apply_patches.py`**, never as hand edits to the
  checkout — anchored string replacements, idempotent, re-applied at every start,
  loud when an anchor stops matching. Never edit `/Volumes/Storage/AIMusic/ACE-Step-1.5`
  directly; it is lost on update.
- **Backlog goes to GitHub issues** with the reasoning, not just a title.

## Traps that have already cost time

- **Only one heavy model fits.** Requesting an image evicts music. Press orders
  its stages (all text → all music → cover) so each heavy model loads once.
- **RSS is meaningless for MLX.** `ps` reports ~120 MB for a backend holding
  21 GB, because MLX allocates through Metal. Use `footprint` / phys_footprint.
- **Judge memory pressure by paging rate and the kernel's own level**, never by
  swap volume. A large swap file here is normal; under load pagein hits
  thousands/sec while pageout stays near zero.
- **`HF_HUB_DISABLE_XET=1` is mandatory.** Xet silently wrote sparse weight files
  to this APFS volume that failed later with "invalid JSON in header".
  `verify-models.py` detects exactly that.
- **`ACE-Step-1.5/checkpoints` must stay a symlink** to the shared model dir;
  upstream hardcodes that path. Without it the server re-downloads 9.4 GB.
- **ACE-Step's REST API assumes turbo.** Non-turbo models need `dcw_enabled=false`
  and must not use the MLX DiT, which is a turbo-specific port. Both are patched;
  see #8.
- **Generation is not deterministic** even with a fixed seed. The reason given
  here for months — "the planning LM samples at temperature" — was never tested
  and is wrong. Measured: with `thinking: false`, a fixed seed *and* `bpm` /
  `key_scale` pinned, the reported `metas` come back identical and the audio
  still differs. `thinking: false` does not disable the planning LM (upstream:
  "regardless of thinking, if some metas are missing, server may use LM to fill
  them"), and something below the plan samples too. Byte-comparing two runs
  proves nothing. See README → Determinism.
- **Never assert against a copied list.** A test that froze the service names,
  the output kinds or the health payload's services went stale the moment one
  was added, and each time it failed *after* the change shipped rather than
  during it. The UI had the same bug where no test could see it: the forge strip
  was a hardcoded array and silently omitted `video`. Assert against
  `services.SERVICES`, `outputs.KINDS` or the source file, and build UI lists
  from `/health`. Four occurrences so far.
- **`grep -m1` / `head -1` mid-pipeline** SIGPIPEs upstream stages; under the
  launchers' `set -euo pipefail` that silently aborts the whole script.
- **Streamed responses must be close-delimited.** `Transfer-Encoding` is
  hop-by-hop so the relay strips it, leaving no framing and a client that hangs.

## Verifying generated output

A metric moving the expected way is **not** evidence the output is good — noise
is broadband, so it raises high-frequency energy. Garbled audio has shipped from
here twice on that reasoning.

What actually discriminates, short of listening:

- **Spectrogram** — `ffmpeg -i x.flac -lavfi showspectrumpic=s=900x360 out.png`,
  then look at it. Music shows harmonic bands, onsets, sections and silence;
  noise is a uniform wash.
- **Near-silent frame fraction** — the sharpest single signal. Music breathes;
  a garbled take had *zero* quiet frames against 22.8% for a good one.
- **Spectral flatness** — noise measures several times higher.

Use these to catch catastrophe. They do not catch "worse", and neither do I —
say so and ask, rather than declaring a quality improvement.

## Things I cannot do here

I can read images, so spectrograms and generated artwork are reviewable. **I
cannot listen to audio.** Do not offer to.
