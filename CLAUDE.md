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
change to `supervisor.py`, `services.py` or `builder.py` does. Forgetting this
has repeatedly produced "my fix didn't work" when the old module was still
loaded; if behaviour doesn't match the code, check the process start time first.

## Conventions

- **Verify against the running system, not by reading.** Reproduce a bug before
  fixing it and confirm the fix afterwards. Several hypotheses here looked
  obviously right and were wrong.
- **Docs change with code.** `README.md`, `INTEGRATION.md` and `openapi.json`
  are part of the change, not follow-up. Correct earlier claims that turn out
  wrong rather than quietly moving on.
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
