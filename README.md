# AIMusic — local, on-demand music generation API

[ACE-Step 1.5](https://github.com/ace-step/ACE-Step-1.5) (MIT) running natively on a
Mac mini M4, exposed as an HTTP API to the host and to the tailnet.

Text-to-music with vocals and lyrics, instrumentals, style transfer, covers,
repainting and continuation — tracks up to 10 minutes, generated entirely locally.

**Integrating it into a project? Read [INTEGRATION.md](INTEGRATION.md).**

## Architecture

```
tailnet ──TLS──► tailscale serve ──► supervisor.py :8001 ──► ACE-Step :8011
                                     (always on, ~25 MB)      (on demand, ~7 GB)
```

The model needs ~7 GB of the machine's 16 GB, which is too much to pin
permanently, and ACE-Step has no idle-unload of its own. So `supervisor.py` owns
the public port and manages the model's lifecycle:

- a request arrives → start ACE-Step, wait for it, forward the request
- 10 minutes with no requests *and* no queued/running jobs → stop it, releasing the RAM

Measured: stopping the backend returns ~6.5 GB to the system. The cost is a
~3–4 minute cold start on the first request after an idle period.

`GET /health` and `GET /v1/audio` are served by the supervisor itself, so health
checks and re-downloads never wake the model.

## Where things live

Everything bulky is on the **Storage SSD**; the internal disk holds only these scripts.

| Path | Contents |
| --- | --- |
| `/Volumes/Storage/AIMusic/ACE-Step-1.5` | upstream repo + `.venv` (~1.2 GB) |
| `/Volumes/Storage/AIMusic/models` | model weights (~9.4 GB) |
| `/Volumes/Storage/AIMusic/uv-cache`, `uv-python` | wheel cache + Python 3.12 (~3.2 GB) |
| `/Volumes/Storage/AIMusic/outputs` | generated audio |
| `/Volumes/Storage/AIMusic/supervisor.log` | supervisor lifecycle log |
| `/Volumes/Storage/AIMusic/api-server.log` | ACE-Step server log |

`ACE-Step-1.5/checkpoints` is a symlink to `models/` — the upstream server hardcodes
that path and ignores `ACESTEP_CHECKPOINTS_DIR`. `start-api.sh` recreates it if a repo
update clobbers it; without it the server silently re-downloads 9.4 GB.

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

Lifecycle control:

```bash
curl -s localhost:8001/supervisor/status | python3 -m json.tool
curl -X POST -H "Authorization: Bearer $ACESTEP_API_KEY" localhost:8001/supervisor/start  # pre-warm
curl -X POST -H "Authorization: Bearer $ACESTEP_API_KEY" localhost:8001/supervisor/stop   # free RAM now
```

## Configuration

`env.sh` is tracked and holds all non-secret settings. The API key lives in
`env.local.sh`, which is gitignored and **generated automatically on first run**.

Useful knobs:

| Variable | Default | Meaning |
| --- | --- | --- |
| `ACESTEP_IDLE_TIMEOUT` | `600` | Seconds idle before the model is unloaded |
| `ACESTEP_BACKEND_PORT` | `8011` | Where ACE-Step itself listens |
| `SUPERVISOR_PORT` | `8001` | Public port |
| `ACESTEP_LM_MODEL_PATH` | `acestep-5Hz-lm-0.6B` | Prompt/lyric planning LM |

## Security

The supervisor binds to `127.0.0.1` only. Tailnet reach comes from `tailscale serve`,
which terminates TLS and proxies to loopback — the API is never exposed to the LAN
or the internet. Every endpoint except `/health` and `/supervisor/status` requires
`Authorization: Bearer $ACESTEP_API_KEY`.

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
