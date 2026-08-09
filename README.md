# Anneal

**Local, on-demand generation — music, speech, images and text behind one API.**

Five models across four services on an Apple silicon Mac, behind a single HTTP
gateway. Nothing leaves the machine.

Named for what it does to its models: heat one up on demand, let it cool and
release the memory when idle. On 16 GB that is not an optimisation — it is the
only way they all fit.

**Nothing leaves the machine.** `env.sh` sets `HF_HUB_OFFLINE=1` and
`TRANSFORMERS_OFFLINE=1`, so a missing or mis-pinned model raises rather than
downloading mid-request. `download-models.sh` and `update.sh` are the only
scripts that fetch anything, and no hosted-model client is installed. The one
external request is `/docs`, which loads Swagger UI from a CDN to render the
spec; the spec itself is local, and the UI at `/` fetches nothing.

| Service | Model | Licence | Output |
| --- | --- | --- | --- |
| Music | [ACE-Step 1.5](https://github.com/ace-step/ACE-Step-1.5) | MIT | Songs with vocals, instrumentals, covers, continuation — up to 10 min. Two quality tiers. |
| Speech | [Kokoro-82M](https://huggingface.co/prince-canuma/Kokoro-82M) via [mlx-audio](https://github.com/Blaizzy/mlx-audio) | Apache-2.0 | 28 voices, American and British English |
| Speech, directed | [Qwen3-TTS CustomVoice](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice) 4-bit via mlx-audio | Apache-2.0 | 9 named voices that take a written performance direction |
| Image | FLUX.1-schnell 4-bit via [mflux](https://github.com/filipstrand/mflux) | Apache-2.0 | Up to ~1536px, plus variations of an earlier image |
| Text | Gemma 4 e4b 4-bit via [mlx-lm](https://github.com/ml-explore/mlx-lm) | Gemma Terms of Use | Chat completions, streaming |

Full attribution for every model and library is in [Credits](#credits) and on
the UI's About page.

**Press** (`POST /v1/press`) chains everything: one brief becomes a title, an
artist, a tracklist, lyrics, the music and a cover. Poll `/v1/press?id=`; the
finished record downloads as a zip, transcoded from FLAC masters. Send
`review: true` to stop after the plan and lyrics so they can be edited before
twenty minutes of audio is committed to.

**Speech** comes in two sets, chosen by the voice you name. Kokoro is the
default: 350 MB, a second or two, 28 voices, and no expressive control — that is
the whole of what the model exposes. The nine Qwen3-TTS voices take an
`instruct` describing the performance ("panicked and breathless", "quietly
furious") with the speaker held fixed, so a character can carry a scene. Sending
a direction to a Kokoro voice returns 400 rather than ignoring it.

**Sprites** (`POST /v1/sprites`) turns a brief into an animation set: transparent
PNGs, one per frame, plus an atlas. API-only — a frame sequence is not something
anyone makes by hand. Two limits before you build on it: the frame
count is a request rather than a guarantee, so read the response; and identity
comes free while motion does not, so pass `poses` to get real movement, at the
cost of more design drift between frames.
[Details](INTEGRATION.md#6c-sprites-an-animation-set-that-stays-the-same-character).

**Vector** (`POST /v1/vector`) draws an SVG icon with the text model — seconds,
no new weights, nothing evicted. **Experimental**: the output is reliably
well-formed and reliably not recognisable as its subject. See
[Vector output](#vector-output) before building on it. Everything returned is
sanitised.

**What it does not do:** video. It was built, measured and removed — nine frames
at 480×272 took three minutes at a 22 GB peak, and the 4-bit transformer was
only 837 MB of that against 11.4 GB for the text encoder. See
[#37](https://github.com/frantinthe23rd/anneal/issues/37) for the animation that
replaced it.

**Using it by hand?** Open the web UI at **`/`** — prompt window, output view,
and a forge strip showing which models are hot.

**Integrating it? Live API docs at `/docs`, spec at `/openapi.json`, plus
[INTEGRATION.md](INTEGRATION.md) for the things a spec can't tell you.**

| Path | |
| --- | --- |
| `/` | Web UI |
| `/docs` | Swagger UI |
| `/openapi.json` | Raw spec |

## Install

```bash
git clone https://github.com/frantinthe23rd/anneal.git
cd anneal
./setup.sh
./anneal start
open http://127.0.0.1:8001
```

`setup.sh` asks one question — where to install — and does the rest: checks the
prerequisites, clones the upstream ACE-Step repo at the pinned commit, builds the
model environment and downloads the weights, in the order they depend on each
other. It is safe to re-run; every step notices what is already done. If it stops,
run it again and it picks up from where it stopped.

**Before it: `brew install uv ffmpeg`, and Xcode's command line tools
(`xcode-select --install`).** `./anneal doctor` checks those and everything else,
names anything missing, and prints the command that installs it — run it first if
you would rather find out before starting.

**`./setup.sh --dry-run`** prints every step, the install root it would use and
the weights it would fetch with their sizes, and writes nothing — not the root,
not the checkout, not the API key in `env.local.sh`.

**Sound effects** are optional and are the one model that competes with nothing
else — it peaks at 1.49 GB and exits, so an effect never unloads the music
model. `./setup.sh --sfx` builds its environment and `./anneal models sfx`
fetches the weights (1.8 GB, Stability AI Community licensed — free below
US $1M annual revenue). Apple silicon only; the runner is pure MLX.

**How much disk.** `./anneal models list` prints every model, its size, and
whether it is optional, before anything is downloaded:

| | | |
| --- | --- | --- |
| Music — ACE-Step bundle + planning LM | 10.7 GB | required for music |
| Music — `acestep-v15-sft` | 4.5 GB | optional: the `high` quality tier |
| Speech — Kokoro-82M | 0.3 GB | required for speech |
| Speech — Qwen3-TTS CustomVoice | 2.2 GB | optional: the nine directed voices |
| Image — FLUX.1-schnell 4-bit | 9.0 GB | required for images |
| Text — Gemma 4 e4b 4-bit | 4.8 GB | required for text, and for Press |
| Sprites — FLUX.1-Kontext 4-bit | 9.0 GB | optional, **non-commercial licence**; the method that uses it is declared but not yet implemented ([#37](https://github.com/frantinthe23rd/anneal/issues/37)) |

Plus ~2.7 GB for the ACE-Step checkout, ~1.3 GB for the model virtualenv and a
few GB of wheel cache. About 45 GB for all of it; about 20 GB for music and
speech alone. Take a subset:

```bash
./setup.sh --models music,speech      # just those two
./anneal models image                 # add another later
./anneal models all                   # including the optional ones
./anneal models list speech           # price it first, download nothing
```

Naming a service takes all of its models, optional ones included — asking for
speech and getting half the voices would be the more surprising rule. The bare
default (`./setup.sh`, `./anneal models required`) takes only what each service
cannot run without: about 24 GB, leaving the high music tier, directed speech
and Kontext behind.

Everything is pinned in `models.lock.json` and downloaded with the Xet transfer
backend disabled — it silently wrote sparse, zero-filled weight files to an
external APFS volume here, which then failed deep inside model loading with
"invalid JSON in header". `verify-models.py` runs afterwards and checks both the
header and the file's allocated blocks, and downloads resume rather than restart.

**Where it installs.** `AIMUSIC_ROOT` — `~/anneal` by default. `setup.sh` records
the choice in `.anneal-root` (gitignored, per machine) so the launchers, the
gateway, the launchd job and the tests all agree without anyone exporting a
variable. Put it on an external volume if the internal disk is tight:

```bash
./setup.sh --root /Volumes/Something/anneal
```

The parent directory has to exist, so mount the volume first. A root whose
parent is missing stops the run and names it, rather than installing into
whatever the path collapses to.

**The first music request takes about 3-4 minutes** and looks like a hang if you
do not know why. It is not: only one heavy model fits in 16 GB, so each is loaded
when it is first asked for and released once idle. Everything after that is
faster, and `./anneal warm music` loads it before you need it.

### One front door

```
./anneal setup            install, or finish an install
./anneal doctor           what is missing, and the command that fixes it
./anneal status           what is installed, what is warm, what it is using
./anneal start | stop | restart
./anneal models [list | all | music,speech]
./anneal warm | cool <service>
./anneal logs [supervisor | api | speech | image | text | launchd]
./anneal update [--check | --deps | --smoke]
./anneal test | prune | monitor | generate | service
```

Every one of these delegates to the script that already did the job —
`start-api.sh`, `update.sh`, `service.sh` and the rest all still work and are
still what runs. `./anneal` is a lid on the toolbox, not a replacement for it.

## What it runs on

**Apple silicon Macs, macOS, 16 GB of unified memory minimum.** That is not a
recommendation, it is the tested floor — and it is a real one. At 16 GB the music
model's ~21 GB footprint already exceeds physical RAM and pages continuously
throughout a generation. Below that, nothing about this design helps: FLUX alone
wants ~11 GB. More memory is strictly better and currently under-exploited,
because every model choice is hardcoded for this machine
([#33](https://github.com/frantinthe23rd/anneal/issues/33)).

**About 40 GB of free disk**, measured on this install: ~26 GB of weights that
are actually used, ~3 GB of virtualenvs, ~4 GB of wheel cache, and room for
`outputs/`, which grows without bound and nothing prunes yet.

Intel Macs are out, and so is everything else, for two separable reasons:

- **MLX.** Speech, image and text all run through MLX, which is Apple-silicon
  only. There is no fallback path in this repo. ACE-Step itself is the exception
  — it supports CUDA and XPU upstream — but the MLX DiT route and both local
  patches are Metal- and turbo-shaped.
- **Four macOS-only system calls.** The supervisor's whole lifecycle rests on
  `/usr/bin/footprint` (phys_footprint — the only accurate memory number for an MLX
  process), `vm_stat`, `sysctl kern.memorystatus_vm_pressure_level` and
  `sysctl vm.swapusage`. Eviction, admission control and the host-pressure
  warning are all downstream of those.

### Contributions

Welcome, including — especially — ports. It's MIT; extend it wherever you like.
The two boundaries above are where the work is, and they are more separable than
they look: the memory calls are four functions in `supervisor.py` behind an
obvious interface, and `services.py` is already generic enough that a CUDA
backend is a dictionary entry rather than a rewrite. A Linux/NVIDIA port would
also make [#33](https://github.com/frantinthe23rd/anneal/issues/33) real work
rather than a thought experiment, since on a 24 GB card the eviction logic that
exists purely because only one heavy model fits stops being necessary at all.

Open an issue before a large change so the reasoning gets recorded alongside it —
that is the convention throughout this repo, and most of the decisions here only
make sense with the measurement attached.

## Architecture

```
                              ┌─► ACE-Step :8011  (on demand, ~21 GB, heavy)
                              ├─► FLUX     :8013  (on demand, ~11 GB, heavy)
client ─► supervisor.py :8001 ┤
          (always on, ~25 MB) ├─► Kokoro / Qwen3-TTS :8012  (on demand, light)
                              └─► Gemma    :8014  (on demand, ~5 GB, light)
```

16 GB of unified memory can't hold these permanently, and none of the backends
offer idle-unload, so ending the process is the only way to reclaim the memory.
`supervisor.py` owns the public port and manages every backend's lifecycle:

- a request arrives → start the owning service, wait for it, forward the request
- starting a **heavy** service first evicts the other heavy service
- idle past the service's timeout, with nothing queued → stop it, releasing the RAM

Measured: loading music takes free RAM from ~11 GB to ~1.5 GB and drives swap
from 4 GB to 17 GB; stopping it hands all of that back. The cost is a ~3–4 minute
cold start for music (~30–60 s for images) after an idle period or an eviction.
Speech and chat are light enough to stay resident alongside either.

**On swap.** Under a music generation the working set exceeds RAM: measured
with `./monitor.py`, pagein peaks around 9,000/s while pageout stays near zero —
the model is being continuously re-read from swap, not written to it. The kernel
reports warning and occasionally critical. It works, and the headroom is real: a
second large model would not fit alongside it. Anneal reports `pressure_level`
and both paging rates, and raises the host chip only when the kernel does, since
a large swap file on its own is normal.

`ANNEAL_FREE_TORCH_DECODER=1` releases a duplicated copy of the DiT decoder
after MLX conversion, taking the peak footprint from 22 GB to 18 GB. It is off
by default: 18 GB still exceeds physical RAM so the paging is unchanged, and it
costs the PyTorch diffusion fallback plus Gradio's LRC and lyric-scoring
features. See [#32](https://github.com/frantinthe23rd/anneal/issues/32).

**Measure with `phys_footprint`, never RSS.** MLX allocates through Metal,
which `ps` does not attribute to the process — a backend holding 21 GB reports
an RSS of ~120 MB. The supervisor shells out to `footprint`, the same figure
Activity Monitor shows.

`/health`, `/supervisor/status`, `/docs`, `/openapi.json` and `/v1/audio` are all
answered by the supervisor itself, so health checks, docs and re-downloads never
wake a model.

Adding another modality means adding an entry to `services.py` — the supervisor
is generic.

**Press** (`builder.py`) chains them. Stage order is the design: doing
lyrics→music→art *per track* would evict and reload a multi-gigabyte model
between every step. Running every text stage, then every music stage, then the
cover means each heavy model loads exactly once whatever the track count:

```
plan (text) → lyrics ×N (text) → music ×N (music) → cover (image)
```

It calls the gateway's own endpoints on loopback rather than the services
directly, so it inherits tier switching, admission control, the durable job
queue and library persistence for free. State is in sqlite, so a press survives a
restart or a closed browser.

A press runs in a thread, so a gateway restart leaves a record claiming to be
working with nothing behind it. On startup anything non-terminal is marked
**interrupted**, and `POST /v1/press/resume` picks up from where it stopped —
finished tracks are kept, only the missing work is redone. `DELETE /v1/press?id=`
removes a record, with `&files=1` to take its audio and cover with it.

## Where things live

Everything bulky is under **`$AIMUSIC_ROOT`**; the repo holds only these scripts.
It is `~/anneal` by default, and on the machine this was built on it is an
external SSD because the internal disk is nearly full — `./anneal doctor` prints
which one this install resolved to, and why.

| Path | Contents |
| --- | --- |
| `$AIMUSIC_ROOT/ACE-Step-1.5` | upstream repo + `.venv` (2.7 GB) |
| `$AIMUSIC_ROOT/models` | ACE-Step weights (15 GB — turbo 4.5, sft 4.5, planning LMs 4.8, Qwen3 embedder 1.1, VAE 0.3) |
| `$AIMUSIC_ROOT/hf-cache` | FLUX 9.0 GB, Gemma 4.8 GB, Qwen3-TTS 2.2 GB, Kokoro 0.3 GB, and Kontext 9.0 GB if installed |
| `$AIMUSIC_ROOT/gen-venv` | venv for speech + image (mlx-audio, mflux) — 1.3 GB |
| `$AIMUSIC_ROOT/tools-venv` | rembg for sprite matting, Playwright for UI shots — optional |
| `$AIMUSIC_ROOT/uv-cache`, `uv-python` | wheel cache + Python 3.12 (4.3 GB) |
| `$AIMUSIC_ROOT/outputs/{music,speech,images,vectors,sprites}` | **everything generated**, prompt-named, with JSON sidecars |
| `$AIMUSIC_ROOT/supervisor.log` | supervisor lifecycle log |
| `$AIMUSIC_ROOT/api-server.log` | ACE-Step server log |
| `$AIMUSIC_ROOT/speech-server.log`, `image-server.log`, `text-server.log` | backend logs |

The root is resolved in one order by everything that needs it — `$AIMUSIC_ROOT`,
then `.anneal-root` (written by `setup.sh`, gitignored, per machine), then an
existing `/Volumes/Storage/AIMusic` if one is there, then `~/anneal`. `env.sh`
and `paths.aimusic_root()` implement it separately because one is bash and the
other is Python; `tests/unit/test_root_resolution.py` runs both and requires
them to agree, since a disagreement would put the databases somewhere the
launchers do not look and neither side would raise.

3.5 GB of that is `acestep-5Hz-lm-1.7B`, which arrives in ACE-Step's bundle and is
never loaded here — this hardware classifies as tier4, which permits only the
0.6B planning LM. It is dead weight on disk, kept because deleting part of a
pinned bundle would make `verify-models.py` unhappy for no benefit.

`ACE-Step-1.5/checkpoints` is a symlink to `models/` — the upstream server hardcodes
that path and ignores `ACESTEP_CHECKPOINTS_DIR`. `start-api.sh` recreates it if a repo
update clobbers it; without it the server silently re-downloads 9.4 GB.

## The UI

Open `/` in a browser. A front door, then four pages behind a subnav —
**Studio** (the application: five modes, output, library), **Guide**, **API**
and **About** — deep-linkable as `#guide`, `#api`, `#about`.

The page makes **no external requests**: `marked`, `DOMPurify` and KaTeX are
vendored into `assets/vendor/` with versions and hashes recorded, and the
backdrop was generated by Anneal's own image model.

The header strip shows every model `/health` reports as **cold**, **heating** or
**hot** with its true footprint, and a **host** chip appears when the machine is
short of memory. Cold-start warnings appear before you commit to a slow request;
a `409` offers to stop the other heavy model and retry rather than just failing.

Reaching the UI over a tailnet address needs **no key** — `tailscale serve`
stamps the caller's identity onto each request and Anneal trusts it. The
loopback address carries no identity, so it asks for the key.

## Usage

```bash
./start-api.sh          # supervisor + tailscale serve
./stop-api.sh           # stop everything, release memory
./verify-models.py      # check weights are complete
./download-models.sh    # (re)download weights
```

Generate:

```bash
source ./env.sh
./generate.py "warm lo-fi hip hop with rhodes piano" --instrumental --duration 30
./generate.py "anthemic indie rock chorus" --lyrics-file words.txt --duration 90
./generate.py "driving synthwave" --bpm 118 --key-scale "F# minor" --batch-size 2
```

Audio lands in `$AIMUSIC_ROOT/outputs` (`--out` to change).

**Everything generated is saved server-side regardless of client**, under
`outputs/{music,speech,images}/`, named from the prompt and paired with a JSON
sidecar holding the parameters that produced it:

```
outputs/music/2026-08-06T11-39-53_bright-acoustic-folk-with-mandolin_28db5d.mp3
outputs/music/2026-08-06T11-39-53_bright-acoustic-folk-with-mandolin_28db5d.mp3.json
```

Sidecars rather than a database, so the metadata travels with the file — copy
the directory anywhere and it stays self-describing. Music is *copied* out of
ACE-Step's temp cache, which the backend prunes, and the API's `file` URL is
rewritten to the durable copy.

Speech and images:

```bash
source ./env.sh
curl -X POST localhost:8001/v1/audio/speech -H "Authorization: Bearer $ACESTEP_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"input":"Build finished.","voice":"bf_emma","response_format":"mp3"}' -o out.mp3

curl -X POST localhost:8001/v1/images/generations -H "Authorization: Bearer $ACESTEP_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"a lighthouse in a storm, oil painting","response_format":"path"}'
```

Lifecycle control:

```bash
curl -s localhost:8001/health | python3 -m json.tool          # what's loaded
# pre-warm / unload a specific service: music | speech | image
curl -X POST -H "Authorization: Bearer $ACESTEP_API_KEY" -H 'Content-Type: application/json' \
  -d '{"service":"image"}' localhost:8001/supervisor/start
curl -X POST -H "Authorization: Bearer $ACESTEP_API_KEY" localhost:8001/supervisor/stop  # stop everything
```

## Surviving a reboot

Anneal does not come back after a restart unless you install the launchd job.
Tailscale's serve configuration *does* survive on its own, so without it the
tailnet URL stays up while proxying to a dead port.

```bash
./service.sh install     # start at login, restart if it dies
./service.sh status      # installed? loaded? answering?
./service.sh uninstall   # stop doing that
```

`install` stops a hand-started gateway, bootstraps the LaunchAgent, and verifies
it by seeing whether the job comes up — exiting non-zero with the launchd exit
code and the log tail if it does not.

**It needs Full Disk Access, and you should know where that grant goes.** macOS
blocks a LaunchAgent from touching an external volume entirely, so this is
unavoidable if the install root is on one. Granting it to `/bin/bash` would give
full disk access to every shell script anything on the machine runs. Instead,
`service.sh` places an ad-hoc signed private copy at
`~/Library/Anneal/anneal-bash` and the job runs that; macOS prompts on first run,
and the grant applies to that copy alone.

It starts at **login**, not at boot — a LaunchAgent, not a daemon, because
`tailscale serve` is configured per user. For an unattended machine, enable
automatic login rather than adding a second code path.

launchd's own log is in `~/Library/Logs/Anneal/`; it cannot live on the external
volume, because launchd opens it before any grant applies.

## Configuration

`env.sh` is tracked and holds all non-secret settings. The API key lives in
`env.local.sh`, which is gitignored and **generated automatically on first run**.

Useful knobs:

| Variable | Default | Meaning |
| --- | --- | --- |
| `AIMUSIC_ROOT` | `~/anneal`, or what `.anneal-root` records | Where models, venvs, logs and output live |
| `ACESTEP_IDLE_TIMEOUT` | `600` | Seconds idle before the model is unloaded |
| `ACESTEP_BACKEND_PORT` | `8011` | Where ACE-Step itself listens |
| `SUPERVISOR_PORT` | `8001` | Public port |
| `ACESTEP_LM_MODEL_PATH` | `acestep-5Hz-lm-0.6B` | Prompt/lyric planning LM |
| `ANNEAL_MIN_FREE_MB` | `1200` | Refuse to load a heavy model below this much free RAM |
| `SPEECH_QWEN_MODEL` | `mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-4bit` | The directed-speech model, loaded only when one of its voices is asked for |
| `ANNEAL_EXPOSE` | `loopback` | Set to `tailnet` to publish through `tailscale serve`. The gateway binds loopback either way |
| `ANNEAL_SPRITE_PYTHON` | `$AIMUSIC_ROOT/tools-venv/bin/python` | Interpreter used to cut and matte sprite sheets. Needs rembg, so it is kept out of the pinned environment that serves the models |
| `UV_BIN`, `TS_BIN`, `TAILNET_HOST` | auto-detected | Override only if detection picks wrong |

Tool paths and the tailnet hostname are resolved at startup rather than
hardcoded, and Tailscale is optional — without it Anneal serves on loopback
only. The OpenAPI `servers` block is generated per host, so `/openapi.json`
always advertises the machine actually serving it.

## Music quality tiers

Pick per request with `"quality": "draft" | "high"`, or in the UI's Model
selector. Measured on the same prompt and seed:

| Tier | Model | Steps | Generation | Notes |
| --- | --- | --- | --- | --- |
| `draft` | `acestep-v15-turbo` | 8 | ~68 s | Distilled for speed. |
| `high` | `acestep-v15-sft` | 50 | ~180 s | Non-turbo: CFG on, DCW off, PyTorch DiT. |

`high` only works because of two local patches in `patches/apply_patches.py`,
re-applied by `start-api.sh` on every start. Both exist because ACE-Step's
non-Gradio paths assume turbo:

1. **`dcw-off-for-non-turbo`** — DCW is turbo-only. `inference.py` hardcodes it
   on and the REST API cannot override it, so sft rendered pure noise. Now
   derived from the model's own `is_turbo` config.
2. **`no-mlx-dit-for-non-turbo`** — `models/mlx/dit_model.py` is, by its own
   docstring, a re-implementation of *`modeling_acestep_v15_turbo.py`*.
   Non-turbo checkpoints are dimensionally identical so they load through it
   without error and render smeared, low-contrast audio. Non-turbo models now
   use the PyTorch DiT, which costs ~50 s but restored dynamic range from
   8.4 dB to 14.6 dB.

Measured on the same prompt and seed, high is ~2× the generation time, 7.2 dB
more energy above 12 kHz and a 23% larger lossless file. **Whether that is
better is a judgement for your ears** — see the note on verification below.

### Determinism

**Generation is not reproducible.** A fixed seed is accepted and recorded, and
two identical requests still produce different audio.

`thinking: false` does not turn the planning LM off — upstream's own request
model says *"regardless of thinking, if some metas are missing, server may use
LM to fill them"*. It selects whether the LM generates audio *codes*; it will
still fill an unspecified `bpm` or `key_scale`, and it samples when it does, so
two identical requests can come back in different keys.

Pinning `bpm` and `key_scale` is not enough either. With both supplied the
reported `metas` are byte-identical across runs and the audio still differs, so
something below the plan is sampling too.

Consequences: byte-comparing two runs proves nothing, and "same seed, same
output" is not available by any route currently known.
[#35](https://github.com/frantinthe23rd/anneal/issues/35) covers what that means
for iterative refinement.

### `metas` is intent, not measurement

`bpm`, `keyscale` and `timesignature` in a result are what the planning LM asked
the DiT for — not an analysis of the audio. The LM can ask for something the
output plainly is not (300 bpm for an indie folk ballad, on a take that measures
nearer 100), and the audio can still be fine. Sidecars record these as
`bpm_planned` / `key_scale_planned` with a `metadata_source` note, and the UI
labels them "(planned)".

Set `bpm` and `key_scale` explicitly on the request if you want them to mean
something.

## Vector output

`POST /v1/vector {"prompt": "a compass rose", "style": "line", "size": 48}`
returns SVG source, saves it under `outputs/vectors/` with a sidecar, and lists
it in the Library under `kind=vectors`. Styles are `flat`, `line`, `duotone`
and `geometric`. `mode: "trace"` — vectorising the image model's output — is
specified in [#36](https://github.com/frantinthe23rd/anneal/issues/36) and
returns `501`: it needs `vtracer` or `potrace`, and neither is installed.

**Everything returned is sanitised.** The reply is parsed, must have a single
`<svg>` root, and is reduced to an allowlist of drawing elements and
attributes. Removed: `<script>`, `<style>`, `<foreignObject>`, every `on*`
handler, SMIL animation, and any `href` or `url()` that is not a local
`#fragment`. A `DOCTYPE` or `ENTITY` declaration is refused before parsing,
because ElementTree is documented as vulnerable to entity expansion. The
response reports what was taken in `sanitised_out`. On top of that, downloads
carry `Content-Disposition: attachment`, `X-Content-Type-Options: nosniff` and
`Content-Security-Policy: default-src 'none'; sandbox`, so three separate
things would have to fail for a generated file to execute anything.

### Well-formed is not good

Measured on `gemma-4-e4b-it-4bit`, across gear, heart, compass rose, health bar
and shield:

| | Well-formed | Recognisable as the subject |
| --- | --- | --- |
| temperature 0.9, plain instructions | 1/5 | — |
| temperature 0.2, rules naming the observed failures | **5/5** | **0/5** |
| the same, plus two worked examples | 5/5 | 1/5 at best |

Each result was rendered and looked at: the gear came back as a plain filled
circle, the compass rose as a single dot, the health bar as one solid rectangle.
Few-shot examples improved the *markup* and did not change whether the drawing
resembles its subject.

The plumbing works and the capability does not, which is why there is no Vector
tab. Larger text models were tried and produced better output, not good enough
to change the verdict, and the sizes that helped do not coexist with a heavy
model here — see [#36](https://github.com/frantinthe23rd/anneal/issues/36).

## Keeping it current

Models and dependencies are pinned: `models.lock.json` records an exact
revision per model, `gen-venv.lock.txt` pins the speech/image environment.
Without pins, a re-download silently pulls whatever upstream is current, so a
rebuilt machine can get different weights than the one that was tested.

```bash
./anneal update --check     # what has moved upstream (read-only, the default)
./anneal models list        # every model, its size, and whether it is optional
./anneal models all         # re-fetch at the pinned revisions, then verify
./anneal update --deps      # rebuild gen-venv from the lockfile
./anneal update --smoke     # generate on all three services and report
./anneal verify             # are the weights on disk actually complete
```

`verify` checks both places weights land — the ACE-Step checkpoints directory
and the Hub cache. It only ever checked the first, which left the larger half of
the install (FLUX at 9 GB, Gemma at 4.8) unverified against the sparse-file
failure it exists to catch. It also runs under `gen-venv` when that exists,
because `safetensors` is only importable there and without it the header check
silently does not run at all.

Updates are never automatic. To take a new revision, edit `models.lock.json`,
run `--models`, then `--smoke` — and treat a smoke failure as a reason to roll
the pin back.

## Working on it

```bash
tools/lint-ui.py      # before committing ui.html
tools/lint-prose.py   # self-characterising words in user-facing copy
./anneal test         # unit and acceptance suites
```

API endpoints are written test-first: the request shape, the
`{data, code, error}` envelope, the unauthenticated response and each failure
mode, before the handler exists — endpoints are the part other people build
against. `README.md`, `INTEGRATION.md` and `openapi.json` change in the same
commit as the code. `CLAUDE.md` has the rest of the conventions and the traps
that have already cost time.

## Security

The supervisor binds to `127.0.0.1` only. Tailnet reach comes from `tailscale serve`,
which terminates TLS and proxies to loopback — the API is never exposed to the LAN
or the internet.

Two ways to authenticate:

- **Tailnet identity.** `tailscale serve` adds `Tailscale-User-Login` to what it
  proxies. Since the only route to the port is that proxy, the header can't be
  forged from off-machine, so it's accepted as proof. This is what lets the UI
  work without a token. Set `ANNEAL_ALLOWED_LOGINS` to restrict which logins
  count; the default accepts any tailnet member, which is right for a personal
  tailnet where reaching the port at all requires being on it.
- **Bearer key** — `Authorization: Bearer $ACESTEP_API_KEY`, for programmatic
  clients and for loopback, which carries no identity.

The identity headers are trusted **only while the listener is on loopback**. Bind
anywhere else and they're ignored outright, since they could then be forged.

### Limits

Everything a caller can ask for is bounded, so one request cannot exhaust the
machine. They are generous — no ordinary request meets one.

| Variable | Default | What it bounds |
| --- | --- | --- |
| `ANNEAL_MAX_REQUEST_BYTES` | 2 MB | Any request body. Refused on the declared `Content-Length`, before a byte is read, then the connection is closed |
| `ANNEAL_MAX_PROMPT_CHARS` | 8000 | A press brief |
| `ANNEAL_MAX_PRESS_SECONDS` | 1800 | `tracks × duration` for one press. Eight ten-minute tracks was previously accepted and is hours of generation holding the heavy slot |
| `ANNEAL_MAX_ZIP_BYTES` | 4 GB | The album zip, which is built on disk before any of it is sent |
| `ANNEAL_JOB_RETENTION_SECONDS` | 7 days | Finished rows in `jobs.db`, pruned hourly. Pending rows are never pruned — that is the replay queue |

Files are served off disk only from under `outputs/` and the backend's own
cache, only if the resolved path is genuinely inside one of them, and only if
the extension is a media type Anneal produces. `paths.py` is the single
containment check; `tests/test_paths.py` covers the traversal, symlink and
shared-prefix cases that a hand-rolled `startswith` gets wrong.

**`outputs/` has no automatic retention policy** and grows without bound.
`./anneal prune` removes old output when you ask it to; nothing removes anything
on its own, because generation is not reproducible and a wrongly deleted take
cannot be regenerated.

Public without auth: `/health`, `/supervisor/status`, `/supervisor/auth`,
`/supervisor/whoami`, `/v1/music/tiers`, `/docs`, `/openapi.json`, the UI and
its assets. Everything else requires one of the two methods above.

## Notes on this hardware

- 16 GB unified memory; ACE-Step sees ~11.8 GB and classifies it as tier4 — MLX
  backend, no quantization, no CPU offload.
- Default DiT is `acestep-v15-turbo` (8 inference steps). The XL models want
  ≥20 GB and aren't worth attempting here.
- tier4 permits only the 0.6B planning LM; requesting the bundled 1.7B is
  silently overridden.
- `HF_HUB_DISABLE_XET=1` is mandatory. The Xet download backend silently wrote
  *sparse* weight files to this external APFS volume — correct logical size,
  zero-filled interiors — which fail deep inside model loading with a confusing
  "invalid JSON in header" error. `verify-models.py` detects exactly this.
- One job at a time: single worker. The backend's queue is in memory, but the
  gateway records every job and replays anything outstanding after a restart.

## Gradio UI

If you'd rather click than curl (bypasses the supervisor and holds memory while running):

```bash
source ./env.sh && cd "$ACESTEP_DIR" && ./start_gradio_ui_macos.sh
```

## MCP server

`mcp_server.py` exposes the services as MCP tools over stdio — stdlib only, so
it needs no environment of its own.

```json
{
  "mcpServers": {
    "anneal": {
      "command": "/path/to/anneal/mcp_server.py",
      "env": {
        "ANNEAL_URL": "http://127.0.0.1:8001",
        "ANNEAL_KEY": "sk-aimusic-..."
      }
    }
  }
}
```

Tools: `service_status`, `prewarm`, `unload_models`, `generate_music`,
`check_music_job`, `generate_speech`, `generate_image`.

Two deliberate choices. **Music submits and returns a job id** rather than
blocking for minutes — `check_music_job` collects, and the descriptions tell the
agent not to resubmit a slow job. **Binary comes back as a file path**, not
base64: a 1.3 MB PNG is ~1.7 MB of text in a tool result, which is a poor use of
anyone's context.

## Credits

Concept, product direction and design: **Jon Moseley**. Built with **Claude
Code**. The hard parts are other
people's work, running locally and unmodified apart from two documented patches
to ACE-Step's non-turbo paths (`patches/apply_patches.py`).

| Model | For | Licence |
| --- | --- | --- |
| [ACE-Step 1.5](https://github.com/ace-step/ACE-Step-1.5) — DiT, 5 Hz planning LM, audio VAE, bundling Qwen3-Embedding-0.6B (Apache-2.0) | Music | MIT |
| [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) by hexgrad, MLX conversion by Prince Canuma | Speech | Apache-2.0 |
| [Qwen3-TTS CustomVoice](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice) by Alibaba, 4-bit MLX conversion by mlx-community | Directed speech — nine named voices that take a written performance direction | Apache-2.0 |
| [FLUX.1-schnell](https://huggingface.co/black-forest-labs/FLUX.1-schnell) by Black Forest Labs, run as a 4-bit mflux quantisation | Images | Apache-2.0 |
| [Gemma 4 E4B](https://huggingface.co/google/gemma-4-E4B-it) by Google DeepMind, 4-bit MLX conversion by mlx-community | Text, lyrics | Gemma Terms of Use |

| Component | For | Licence |
| --- | --- | --- |
| [MLX](https://github.com/ml-explore/mlx) · [mlx-lm](https://github.com/ml-explore/mlx-lm) | Everything but ACE-Step runs on it | MIT |
| [mlx-audio](https://github.com/Blaizzy/mlx-audio) (with [misaki](https://github.com/hexgrad/misaki), Apache-2.0) | Kokoro on MLX | MIT |
| [mflux](https://github.com/filipstrand/mflux) | FLUX on MLX | MIT |
| [PyTorch](https://pytorch.org) | ACE-Step's own runtime, including the non-turbo DiT path | BSD-3-Clause |
| [FFmpeg](https://ffmpeg.org) | Transcoding for downloads and speech formats | LGPL / GPL |
| [Tailscale](https://tailscale.com) | Reach, TLS and caller identity without exposing a port | BSD-3-Clause |
| [marked](https://github.com/markedjs/marked) · [DOMPurify](https://github.com/cure53/DOMPurify) · [KaTeX](https://katex.org) | Chat Markdown, sanitising, and maths — vendored, not CDN-loaded | MIT · Apache-2.0/MPL-2.0 · MIT |
| [rembg](https://github.com/danielgatis/rembg) with u2net | Background removal for sprite frames | MIT · Apache-2.0 |
| [Swagger UI](https://github.com/swagger-api/swagger-ui) | `/docs` — the one page that loads from a CDN | Apache-2.0 |
| [uv](https://github.com/astral-sh/uv) · [Hugging Face Hub](https://huggingface.co) | Environments and weights, pinned to exact revisions | Apache-2.0 / MIT |

The gateway itself is Python's standard library — no web framework. Licences are
as published upstream at the time of writing; confirm them before shipping
generated output commercially.

## Licence

Anneal's own code is **MIT** — see [LICENSE](LICENSE). © 2026 Jon Moseley.

Three things that licence does *not* cover, because they aren't Anneal's to
license:

- **The models.** They keep their own terms (see Credits above). Gemma's weights
  in particular are under Google's Gemma Terms of Use, not an OSI licence.
- **Generated output.** Yours, subject to those upstream model terms. The music
  model will imitate a described style, so do not name a specific living artist
  in a prompt and then publish the result commercially.
- **`/v1/sprites` with `method: "kontext"`.** That method uses FLUX.1 Kontext
  [dev], which is **non-commercial** — the weights may not be used commercially
  without a licence from Black Forest Labs, though the output is yours. The
  default `sheet` method is Apache-2.0 and unaffected.
- **The upstream ACE-Step checkout**, which lives outside this repo under its own
  MIT licence.

MIT asks one thing in return: keep the copyright and permission notice in copies
and substantial portions. That is the only attribution it can require — it does
not oblige anyone to credit Anneal in a UI, a README or a product page.

If you build something with this, an issue saying what you made is welcome.

Built for **local, personal use on a private network**. It binds to loopback,
and reaching it from elsewhere is opt-in through `tailscale serve`. It has
request and resource limits, path containment and two authentication paths, but
it has not been through an audit and is not intended to face the open internet.
