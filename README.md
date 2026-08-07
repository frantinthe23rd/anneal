# Anneal

**Local, on-demand generation — music, speech and images behind one API.**

Three models on a Mac mini M4, behind a single HTTP gateway, reachable from the
host and over the tailnet. Nothing leaves the machine.

Named for what it does to its models: heat one up on demand, let it cool and
release the memory when idle. On 16 GB that isn't an optimisation, it's the only
way they all fit.

Concept, product direction and design by **Jon Moseley**; built with **Claude
Code**, which is also what it was built to serve. It began as an API with no
interface at all — somewhere a build script or a coding agent could ask for the
assets code can't produce: a soundtrack, a voiceover, a title card. The music
turned out to be the part that worked best, which is where **Press** came from.
The UI came last, because judging a generated take by reading JSON is miserable.

**Everything runs locally.** No inference request leaves the machine. `env.sh`
sets `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` at run time, so the model
libraries resolve from the local cache and *raise* rather than quietly fetching
anything — a missing or mis-pinned model fails loudly instead of being
downloaded mid-request. `download-models.sh` and `update.sh` clear those flags
deliberately; they are the only places a download happens. No hosted-model
client (OpenAI, Anthropic, and so on) is installed in either virtualenv.

The one external fetch is `/docs`, which pulls Swagger UI's CSS and JS from a
CDN for the browser to render. No prompt or generated content is involved, and
the spec itself is served locally. Say so if you want it vendored. The UI at `/`
fetches nothing externally: its backdrop, favicon and the two browser libraries
it uses are all served from `assets/`.

| Service | Model | Licence | Output |
| --- | --- | --- | --- |
| Music | [ACE-Step 1.5](https://github.com/ace-step/ACE-Step-1.5) | MIT | Songs with vocals, instrumentals, covers, continuation — up to 10 min. Two quality tiers. |
| Speech | [Kokoro-82M](https://huggingface.co/prince-canuma/Kokoro-82M) via [mlx-audio](https://github.com/Blaizzy/mlx-audio) | Apache-2.0 | 28 voices, en/uk |
| Image | FLUX.1-schnell 4-bit via [mflux](https://github.com/filipstrand/mflux) | Apache-2.0 | Up to ~1536px, plus variations of an earlier image |
| Text | Gemma 4 e4b 4-bit via [mlx-lm](https://github.com/ml-explore/mlx-lm) | Gemma Terms of Use | Chat completions, streaming |

(The Gemma row previously said Apache-2.0 here. That was wrong: the *tooling* is
Apache/MIT, but Google's weights are under the Gemma Terms of Use. Full
attribution for every model and library is in [Credits](#credits) and on the
UI's About page.)

**Press** chains all four: one brief becomes a title, an artist name, a
tracklist with varied lengths, lyrics, the music and a cover.
`POST /v1/press {"prompt": "...", "tracks": 4}`, then poll `/v1/press?id=`.
Results appear in the Library as a record you can play through, and
`GET /v1/press/download?id=…&format=mp3&bitrate=320k` returns the whole thing as
a zip — audio, cover and tracklist. Masters are FLAC, so lossy formats are
transcoded from the original rather than from another lossy copy.

**Using it by hand?** Open the web UI at **`/`** — prompt window, output view,
and a forge strip showing which models are hot.

**Integrating it? Live API docs at `/docs`, spec at `/openapi.json`, plus
[INTEGRATION.md](INTEGRATION.md) for the things a spec can't tell you.**

| Path | |
| --- | --- |
| `/` | Web UI |
| `/docs` | Swagger UI |
| `/openapi.json` | Raw spec |

## What it runs on

**Apple silicon Macs, macOS, 16 GB of unified memory minimum.** That is not a
recommendation, it is the tested floor — and it is a real one. At 16 GB the music
model's ~21 GB footprint already exceeds physical RAM and pages continuously
throughout a generation. Below that, nothing about this design helps: FLUX alone
wants ~11 GB. More memory is strictly better and currently under-exploited,
because every model choice is hardcoded for this machine
([#9](https://github.com/frantinthe23rd/anneal/issues/9)).

**About 40 GB of free disk**, measured on this install: ~26 GB of weights that
are actually used, ~3 GB of virtualenvs, ~4 GB of wheel cache, and room for
`outputs/`, which grows without bound and nothing prunes yet.

Intel Macs are out, and so is everything else, for two separable reasons:

- **MLX.** Speech, image and text all run through MLX, which is Apple-silicon
  only. There is no fallback path in this repo. ACE-Step itself is the exception
  — it supports CUDA and XPU upstream — but the MLX DiT route and both local
  patches are Metal- and turbo-shaped.
- **Four macOS-only system calls.** The supervisor's whole lifecycle rests on
  `/usr/bin/footprint` (phys_footprint — the only honest memory number for an MLX
  process), `vm_stat`, `sysctl kern.memorystatus_vm_pressure_level` and
  `sysctl vm.swapusage`. Eviction, admission control and the host-pressure
  warning are all downstream of those.

### Contributions

Welcome, including — especially — ports. It's MIT; extend it wherever you like.
The two boundaries above are where the work is, and they are more separable than
they look: the memory calls are four functions in `supervisor.py` behind an
obvious interface, and `services.py` is already generic enough that a CUDA
backend is a dictionary entry rather than a rewrite. A Linux/NVIDIA port would
also make [#9](https://github.com/frantinthe23rd/anneal/issues/9) real work
rather than a thought experiment, since on a 24 GB card the eviction logic that
exists purely because only one heavy model fits stops being necessary at all.

Open an issue before a large change so the reasoning gets recorded alongside it —
that is the convention throughout this repo, and most of the decisions here only
make sense with the measurement attached.

## Architecture

```
                                        ┌─► ACE-Step  :8011  (on demand, ~21 GB, heavy)
tailnet ─TLS─► tailscale serve ─► supervisor.py :8001 ─┼─► Kokoro    :8012  (on demand, ~400 MB, light)
                                  (always on, ~25 MB)  └─► FLUX      :8013  (on demand, ~11 GB, heavy)
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

**On swap.** Idle, a large swap file costs nothing: pageouts sit near one per
second and the kernel reports *normal*. Under an actual music generation it is
different — measured with `./monitor.py` across a 5-minute run:

```
peak pagein/s 9025   peak pageout/s 32   min free 1421M   worst pressure warning
```

Pageout stays near zero while **pagein** hits thousands per second: the working
set is larger than RAM and is being continuously re-read from swap, not newly
written to it. That is ~140 MB/s, which an Apple NVMe absorbs without the
machine feeling any different — which is exactly why it *seems* free. But the
kernel does report warning and occasionally critical, and the headroom is real:
a second large model would not fit alongside it.

So both things are true. It works fine, and it is genuinely tight. Anneal
reports `pressure_level` from the kernel plus both paging rates, and only raises
the host chip when the kernel says so — not merely because swap is large.

Run `./monitor.py` during a generation to see it for yourself.

`ANNEAL_FREE_TORCH_DECODER=1` releases a duplicated copy of the DiT decoder
after MLX conversion, taking the peak footprint from 22 GB to 18 GB. It is off
by default: 18 GB still exceeds physical RAM so the paging is unchanged, and it
costs the PyTorch diffusion fallback plus Gradio's LRC and lyric-scoring
features. See [#7](https://github.com/frantinthe23rd/anneal/issues/7).

**Measure with `phys_footprint`, never RSS.** MLX allocates through Metal, which
`ps` does not attribute to the process — a backend genuinely holding 21 GB
reports an RSS of ~120 MB that jitters as ordinary heap moves around. The
supervisor shells out to `footprint`, the same figure Activity Monitor shows.
Every memory number here was wrong until that was fixed.

`/health`, `/supervisor/status`, `/docs`, `/openapi.json` and `/v1/audio` are all
answered by the supervisor itself, so health checks, docs and re-downloads never
wake a model.

Adding a fourth modality means adding an entry to `services.py` — the supervisor
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
queue and library persistence for free. State is in sqlite, because a five-track
album is twenty minutes of work that must survive a restart or a closed browser.

A press runs in a thread, so a gateway restart leaves a record claiming to be
working with nothing behind it. On startup anything non-terminal is marked
**interrupted**, and `POST /v1/press/resume` picks up from where it stopped —
finished tracks are kept, only the missing work is redone. `DELETE /v1/press?id=`
removes a record, with `&files=1` to take its audio and cover with it.

## Where things live

Everything bulky is on the **Storage SSD**; the internal disk holds only these scripts.

| Path | Contents |
| --- | --- |
| `/Volumes/Storage/AIMusic/ACE-Step-1.5` | upstream repo + `.venv` (1.6 GB) |
| `/Volumes/Storage/AIMusic/models` | ACE-Step weights (15 GB — turbo 4.5, sft 4.5, planning LMs 4.8, Qwen3 embedder 1.1, VAE 0.3) |
| `/Volumes/Storage/AIMusic/hf-cache` | FLUX 9.0 GB, Gemma 4.8 GB, Kokoro 0.3 GB |
| `/Volumes/Storage/AIMusic/gen-venv` | venv for speech + image (mlx-audio, mflux) — 1.3 GB |
| `/Volumes/Storage/AIMusic/uv-cache`, `uv-python` | wheel cache + Python 3.12 (4.3 GB) |
| `/Volumes/Storage/AIMusic/outputs/{music,speech,images}` | **everything generated**, prompt-named, with JSON sidecars |
| `/Volumes/Storage/AIMusic/supervisor.log` | supervisor lifecycle log |
| `/Volumes/Storage/AIMusic/api-server.log` | ACE-Step server log |
| `/Volumes/Storage/AIMusic/speech-server.log`, `image-server.log` | backend logs |

3.5 GB of that is `acestep-5Hz-lm-1.7B`, which arrives in ACE-Step's bundle and is
never loaded here — this hardware classifies as tier4, which permits only the
0.6B planning LM. It is dead weight on disk, kept because deleting part of a
pinned bundle would make `verify-models.py` unhappy for no benefit.

`ACE-Step-1.5/checkpoints` is a symlink to `models/` — the upstream server hardcodes
that path and ignores `ACESTEP_CHECKPOINTS_DIR`. `start-api.sh` recreates it if a repo
update clobbers it; without it the server silently re-downloads 9.4 GB.

## The UI

Arrives at a **front door**: what Anneal is, which models are warm, a card per
capability, and — with equal weight — the fact that everything here is also an
API a build script or coding agent can call. The backdrop was generated by
Anneal's own image model and lives in `assets/`, served by the gateway. Click
the wordmark to return to it.

Four pages behind a subnav, deep-linkable as `#guide`, `#api`, `#about`:

| Page | |
| --- | --- |
| **Studio** | The application — five modes, output, library. |
| **Guide** | How to get a good result out of each mode, and the cold-start arithmetic behind all of them. |
| **API** | Why it is a service first, auth, the job protocol, the endpoint table, and the MCP setup. Code samples quote the address you are actually on. |
| **About** | What it is, why it exists, why it is called Anneal — and full attribution for every model and library. |

The page still makes **no external requests**. `marked` and `DOMPurify` (chat
Markdown, and sanitising it) are vendored into `assets/vendor/` with their
versions and hashes recorded in `assets/vendor/README.md`, rather than pulled
from a CDN. The favicon is an SVG of the same anvil the empty state uses.

Motion runs through three CSS variables, so `prefers-reduced-motion` switches the
whole interface to still in one place. Transitions are used where they carry
meaning — the cold → heating → hot lifecycle, drawers opening — not as
decoration.

**Settings** (header, or from the front door) holds client-side defaults for
new work, a read-only view of the server's state with the environment variable
that changes each one, and the API key. A web page should not be rewriting the
gateway's environment, so it shows rather than edits.

Open `https://<tailnet-host>/` from anywhere on the tailnet — **no key, no
login**. `tailscale serve` stamps the caller's identity onto every request it
proxies, and Anneal trusts that, so the browser is already authenticated.

The `http://127.0.0.1:8001/` loopback address carries no such identity, so it
still asks for the key. Using the tailnet address on the host itself avoids
that.

- **Music / Speech / Image** tabs, prompt box, and the options that matter per mode.
- **Forge strip** in the header shows all four models — music, speech, chat and
  image — as **cold**, **heating** or **hot**, with the true footprint once
  loaded. Updated from `/health`, which never wakes anything. "Heating" matters:
  the process answers long before the weights are in, and reporting that as
  ready was actively misleading. The pill pulses for whichever service is
  actually working, which is not always the tab you're on — the lyric writer
  runs on the chat model from the Music tab.
- A **host** chip appears when the machine is short on memory or swapping hard,
  which on 16 GB is the usual reason a job crawls or dies.
- Cold-start warnings appear *before* you commit to a slow request, so a 4-minute
  music generation isn't a surprise.
- A `409` (the other heavy model is mid-job) offers to stop it and retry rather
  than just failing.
- Results stack newest-first with inline playback or preview and a download link.
- **Vary this** on any generated image feeds it back as an init latent, keeping
  the composition and re-rendering the detail. Worth knowing what it is not: it
  does **not** apply prompt changes to an existing image. Measured — asking a
  brass watch to become silver returns the same brass watch. Distillation is
  why: keeping the image spends the steps that would redraw it. Use it for
  variants of a shot, not to change what is in one. See
  [#19](https://github.com/frantinthe23rd/anneal/issues/19).
- **Chat** is a plain conversation with the local Gemma model, streamed, in the
  shape every chat interface has: transcript above, composer below, **Enter to
  send** and Shift+Enter for a newline. Replies render as **Markdown** — the model
  writes it whether or not you render it, and a wall of literal `**` and
  backticks was the worst thing in the interface. Reasoning is off by default —
  Gemma 4 will otherwise spend a short budget thinking before answering — with a
  checkbox to show it. The transcript stays in the browser; the server keeps no
  conversation state.
- **Write for me** in the lyrics block drafts lyrics from the style prompt,
  streaming into the box. Click again to stop; your text is restored on failure.
- **Library** switches to everything the server has kept — filter by kind, play
  or preview, download, delete, **Details** and **Reuse**. Details shows exactly
  what produced a file: every submitted parameter, the full prompt and lyrics,
  which DiT and planning model ran, and the planned bpm/key labelled as such.
  Reuse loads those settings straight back into the form. Served off disk, so
  browsing wakes no model.

  Worth knowing when comparing takes: **generation is not deterministic**, even
  with `use_random_seed: false`. Two identical requests produce different audio,
  because the planning LM samples at temperature. Details tells you what was
  asked for; it cannot tell you why two takes of the same request differ.
- `Cmd/Ctrl+Enter` submits.

Dark by design — the accent colour is reserved for things that are genuinely hot
or genuinely in progress.

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

Audio lands in `/Volumes/Storage/AIMusic/outputs` (`--out` to change).

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

## Configuration

`env.sh` is tracked and holds all non-secret settings. The API key lives in
`env.local.sh`, which is gitignored and **generated automatically on first run**.

Useful knobs:

| Variable | Default | Meaning |
| --- | --- | --- |
| `AIMUSIC_ROOT` | `/Volumes/Storage/AIMusic` | Where models, venvs, logs and output live |
| `ACESTEP_IDLE_TIMEOUT` | `600` | Seconds idle before the model is unloaded |
| `ACESTEP_BACKEND_PORT` | `8011` | Where ACE-Step itself listens |
| `SUPERVISOR_PORT` | `8001` | Public port |
| `ACESTEP_LM_MODEL_PATH` | `acestep-5Hz-lm-0.6B` | Prompt/lyric planning LM |
| `ANNEAL_MIN_FREE_MB` | `1200` | Refuse to load a heavy model below this much free RAM |
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

### `metas` is intent, not measurement

`bpm`, `keyscale` and `timesignature` in a result are what the planning LM asked
the DiT for — not an analysis of the audio. The LM can ask for something the
output plainly is not (300 bpm for an indie folk ballad, on a take that measures
nearer 100), and the audio can still be fine. Sidecars record these as
`bpm_planned` / `key_scale_planned` with a `metadata_source` note, and the UI
labels them "(planned)".

Set `bpm` and `key_scale` explicitly on the request if you want them to mean
something.

### Verifying generated audio

An earlier "improvement" here was garbled noise shipped on the strength of a
spectral measurement that moved in the expected direction. Noise is broadband,
so it *raises* high-frequency energy. Numbers alone cannot tell good audio from
bad.

What does work, short of listening:

- **Spectrogram** — `ffmpeg -i x.flac -lavfi showspectrumpic=s=900x360 out.png`.
  Music shows harmonic bands, note onsets, section changes and silence. Noise is
  a uniform wash. The difference is unmistakable.
- **Near-silent frame fraction** — music breathes; the garbled take had *zero*
  quiet frames against 22.8% for a good one. This was the sharpest single
  discriminator.
- **Spectral flatness** — the garbled take measured 5× higher.

Use those to catch catastrophic failure. Use your ears for everything else.

ACE-Step fixes its DiT at startup and can only route between models already
resident, which on 16 GB is one. **Switching tiers restarts the backend**, so
the first request after a switch pays a cold load. `GET /v1/music/tiers` reports
which is loaded, so a client can warn before committing.

Output is **FLAC by default**. The backend's MP3 encoder is fixed at 128 kbps
and does not expose a bitrate through the API — a null test puts its discarded
residual at −42 dB, around 24 dB below programme level, which is audible.

## Keeping it current

Models and dependencies are pinned: `models.lock.json` records an exact
revision per model, `gen-venv.lock.txt` pins the speech/image environment.
Without pins, a re-download silently pulls whatever upstream is current, so a
rebuilt machine can get different weights than the one that was tested.

```bash
./update.sh --check     # what has moved upstream (read-only, the default)
./update.sh --models    # re-fetch at the pinned revisions, then verify
./update.sh --deps      # rebuild gen-venv from the lockfile
./update.sh --smoke     # generate on all three services and report
```

Updates are never automatic. To take a new revision, edit `models.lock.json`,
run `--models`, then `--smoke` — and treat a smoke failure as a reason to roll
the pin back.

## Working on it

`CLAUDE.md` has the full working notes — layout, the traps, and what has already
cost time. Three rules are worth stating here, where someone reading about the
API will meet them.

**API endpoints are written test-first.** The test comes before the handler:
request shape, the `{data, code, error}` envelope, what an unauthenticated call
gets, and each failure mode with its status. It is a rule about endpoints
specifically, because they are the part other people build against — and three
got out without one. `/v1/press/cancel` shipped documented nowhere: not the
spec, not the guide, not the endpoint tables. `init_image` and `retention`
shipped and, outside the server, existed only in the UI's own JavaScript.
`JobStore.prune()` exists and nothing calls it. All three were found by hand, in
an audit prompted by someone asking whether the docs were still true — which is
not a mechanism.

**An endpoint change updates `openapi.json` and `INTEGRATION.md` in the same
commit.** `/openapi.json` is what integrators point their tooling at, so a spec
that lags the server is worse than no spec: it is confidently wrong.

**`ui.html` is linted and photographed before it is committed.** `tools/lint-ui.py`
needs no Node — standard library plus the JavaScriptCore shell macOS already
ships — and checks the faults that raise nothing in a browser: `getElementById`
targets that no longer exist, unresolved `var(--x)`, colour tokens missing from
the light theme, duplicate ids, a syntax error in the inline script, and any
`http(s)` subresource, which would quietly break the guarantee above that the
page fetches nothing externally. Then take a screenshot — `tools/README.md`
explains how, and lists three faults that only a picture caught.

```bash
tools/lint-ui.py     # before committing ui.html
tools/test.sh        # the suite in tests/
```

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

Public without auth: `/health`, `/supervisor/status`, `/supervisor/auth`,
`/supervisor/whoami`, `/docs`, `/openapi.json` and the UI itself. Everything
else requires one of the two methods above.

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
- One job at a time: single worker, in-memory queue, lost on restart.

## Gradio UI

If you'd rather click than curl (bypasses the supervisor and holds memory while running):

```bash
cd /Volumes/Storage/AIMusic/ACE-Step-1.5 && source ~/dev/AIMusic/env.sh && ./start_gradio_ui_macos.sh
```

## MCP server

`mcp_server.py` exposes the services as MCP tools over stdio — stdlib only, so
it needs no environment of its own.

```json
{
  "mcpServers": {
    "anneal": {
      "command": "/Users/jon/dev/AIMusic/mcp_server.py",
      "env": {
        "ANNEAL_URL": "https://jons-mac-mini.pangolin-darter.ts.net",
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
Code** — the tools that made it are the tools it feeds. The hard parts are other
people's work, running locally and unmodified apart from two documented patches
to ACE-Step's non-turbo paths (`patches/apply_patches.py`).

| Model | For | Licence |
| --- | --- | --- |
| [ACE-Step 1.5](https://github.com/ace-step/ACE-Step-1.5) — DiT, 5 Hz planning LM, audio VAE, bundling Qwen3-Embedding-0.6B (Apache-2.0) | Music | MIT |
| [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) by hexgrad, MLX conversion by Prince Canuma | Speech | Apache-2.0 |
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
| [marked](https://github.com/markedjs/marked) · [DOMPurify](https://github.com/cure53/DOMPurify) | Chat Markdown, rendered and sanitised — vendored, not CDN-loaded | MIT · Apache-2.0/MPL-2.0 |
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
- **Generated output.** Yours, subject to those upstream model terms.
- **The upstream ACE-Step checkout**, which lives outside this repo under its own
  MIT licence.

MIT asks one thing in return: keep the copyright and permission notice in copies
and substantial portions. That is the only attribution it can require — it does
not oblige anyone to credit Anneal in a UI, a README or a product page. If that
matters to you downstream, ask rather than assume.

Built for **local, personal use on a private network**. It binds to loopback and
reaches the tailnet through `tailscale serve`; it is not hardened for public
exposure, and [#13](https://github.com/frantinthe23rd/anneal/issues/13) tracks
what would need doing first.
