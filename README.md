# Anneal

**Local, on-demand generation — music, speech and images behind one API.**

Three models on a Mac mini M4, behind a single HTTP gateway, reachable from the
host and over the tailnet. Nothing leaves the machine.

Named for what it does to its models: heat one up on demand, let it cool and
release the memory when idle. On 16 GB that isn't an optimisation, it's the only
way all three fit.

| Service | Model | Licence | Output |
| --- | --- | --- | --- |
| Music | [ACE-Step 1.5](https://github.com/ace-step/ACE-Step-1.5) | MIT | Songs with vocals, instrumentals, covers, continuation — up to 10 min |
| Speech | [Kokoro-82M](https://huggingface.co/prince-canuma/Kokoro-82M) via [mlx-audio](https://github.com/Blaizzy/mlx-audio) | Apache-2.0 | 28 voices, en/uk |
| Image | FLUX.1-schnell 4-bit via [mflux](https://github.com/filipstrand/mflux) | Apache-2.0 | Up to ~1536px |

**Using it by hand?** Open the web UI at **`/`** — prompt window, output view,
and a forge strip showing which models are hot.

**Integrating it? Live API docs at `/docs`, spec at `/openapi.json`, plus
[INTEGRATION.md](INTEGRATION.md) for the things a spec can't tell you.**

| Path | |
| --- | --- |
| `/` | Web UI |
| `/docs` | Swagger UI |
| `/openapi.json` | Raw spec |

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
Speech is light enough to stay resident alongside either.

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

## Where things live

Everything bulky is on the **Storage SSD**; the internal disk holds only these scripts.

| Path | Contents |
| --- | --- |
| `/Volumes/Storage/AIMusic/ACE-Step-1.5` | upstream repo + `.venv` (~1.2 GB) |
| `/Volumes/Storage/AIMusic/models` | ACE-Step weights (~9.4 GB) |
| `/Volumes/Storage/AIMusic/hf-cache` | Kokoro + FLUX weights (~9.4 GB) |
| `/Volumes/Storage/AIMusic/gen-venv` | venv for speech + image (mlx-audio, mflux) |
| `/Volumes/Storage/AIMusic/uv-cache`, `uv-python` | wheel cache + Python 3.12 (~3.2 GB) |
| `/Volumes/Storage/AIMusic/outputs` | generated audio and `images/` |
| `/Volumes/Storage/AIMusic/supervisor.log` | supervisor lifecycle log |
| `/Volumes/Storage/AIMusic/api-server.log` | ACE-Step server log |
| `/Volumes/Storage/AIMusic/speech-server.log`, `image-server.log` | backend logs |

`ACE-Step-1.5/checkpoints` is a symlink to `models/` — the upstream server hardcodes
that path and ignores `ACESTEP_CHECKPOINTS_DIR`. `start-api.sh` recreates it if a repo
update clobbers it; without it the server silently re-downloads 9.4 GB.

## The UI

Open `https://<tailnet-host>/` from anywhere on the tailnet — **no key, no
login**. `tailscale serve` stamps the caller's identity onto every request it
proxies, and Anneal trusts that, so the browser is already authenticated.

The `http://127.0.0.1:8001/` loopback address carries no such identity, so it
still asks for the key. Using the tailnet address on the host itself avoids
that.

- **Music / Speech / Image** tabs, prompt box, and the options that matter per mode.
- **Forge strip** in the header shows each model as **cold**, **heating** or
  **hot**, with its true footprint once loaded — updated from `/health`, which
  never wakes anything. "Heating" matters: the process answers long before the
  weights are in, and reporting that as ready was actively misleading.
- A **host** chip appears when the machine is short on memory or swapping hard,
  which on 16 GB is the usual reason a job crawls or dies.
- Cold-start warnings appear *before* you commit to a slow request, so a 4-minute
  music generation isn't a surprise.
- A `409` (the other heavy model is mid-job) offers to stop it and retry rather
  than just failing.
- Results stack newest-first with inline playback or preview and a download link.
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
