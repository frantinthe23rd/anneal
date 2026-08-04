# Integrating the local music generation API

A self-hosted music generation service running on the Mac mini (`jons-mac-mini`).
It turns a text description — plus optional lyrics — into finished audio.

This document is for developers and agents wiring the service into a project.
You do not need to understand the model; treat it as an async HTTP job API.

---

## 1. Connection

| | |
| --- | --- |
| **Tailnet** | `https://jons-mac-mini.pangolin-darter.ts.net` |
| **On the host itself** | `http://127.0.0.1:8001` |
| **Auth** | `Authorization: Bearer <API_KEY>` on every request |

The service is reachable **only over Tailscale** — it is not on the public
internet and not on the LAN. Your machine must be on the tailnet. TLS on the
tailnet URL is terminated by Tailscale; the certificate is valid, so no
`--insecure` or cert pinning is needed.

Get the API key from the host at `~/dev/AIMusic/env.local.sh`. **Do not commit
it.** Read it from an environment variable in your project:

```bash
export MUSIC_API_URL=https://jons-mac-mini.pangolin-darter.ts.net
export MUSIC_API_KEY=sk-aimusic-...
```

Requests without a valid key get `401`.

---

## 2. The one thing that will surprise you: cold starts

The machine has 16 GB of RAM, and the model needs ~7 GB of it. Keeping it
loaded permanently starved everything else, so the model is **loaded on demand
and unloaded after 10 minutes of inactivity**.

Practical consequences:

| Situation | First-response latency |
| --- | --- |
| Model already warm | generation time only (~1.5–3 min) |
| Model cold (idle >10 min) | **+3–4 minutes** while weights load |

So a single request can legitimately take **up to ~8 minutes** end to end.

**Design for this:**

- Set HTTP client timeouts to **at least 900s** on `POST /release_task`, or
  pre-warm first (below). A 30s default timeout will fail every cold request.
- Never treat a slow first request as an error. Do not auto-retry it — a retry
  queues a *second* generation behind the first and makes things worse.
- Put generation behind a job/queue in your own app. Do not block a web request
  on it.

**Pre-warming** — if you know work is coming (user opened the editor, a batch is
scheduled), fire this first and the model loads while the user is still typing:

```bash
curl -X POST -H "Authorization: Bearer $MUSIC_API_KEY" \
  "$MUSIC_API_URL/supervisor/start"
```

It blocks until the model is ready, then returns. Any subsequent request within
the idle window is warm. Each request resets the idle timer.

`GET /health` and `GET /v1/audio` are answered without waking the model, so
health checks and re-downloads are free and instant. Poll `/health` as often as
you like.

---

## 3. The flow

Generation is asynchronous — three steps:

```
POST /release_task   -> task_id
POST /query_result   -> poll until status is 1 (done) or 2 (failed)
GET  /v1/audio?path= -> download the file
```

### Response envelope

**Every** endpoint wraps its payload identically:

```json
{ "data": ..., "code": 200, "error": null, "timestamp": 1785881567698, "extra": null }
```

Check `code == 200` and `error == null` before touching `data`. Note that HTTP
200 with `code: 500` in the body is possible — check the body, not just the
status line.

### 3.1 Submit

```bash
curl -X POST "$MUSIC_API_URL/release_task" \
  -H "Authorization: Bearer $MUSIC_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
        "prompt": "warm indie pop ballad, female vocals, brushed drums",
        "lyrics": "[verse]\nStreetlights blur into the rain\n\n[chorus]\nHold on, hold on",
        "audio_duration": 45,
        "audio_format": "mp3"
      }'
```

→ `{"data": {"task_id": "42f580bc-..."}, "code": 200, ...}`

### 3.2 Poll

```bash
curl -X POST "$MUSIC_API_URL/query_result" \
  -H "Authorization: Bearer $MUSIC_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"task_id_list": ["42f580bc-..."]}'
```

→

```json
{"data": [{"task_id": "42f580bc-...", "status": 1, "result": "[{\"file\": \"/v1/audio?path=...\", ...}]"}]}
```

| `status` | meaning |
| --- | --- |
| `0` | queued or running |
| `1` | succeeded |
| `2` | failed (`result` carries the error) |

**`result` is a JSON-encoded string, not an object** — parse it a second time.
It decodes to a *list*, one entry per take (see `batch_size`). Each entry has:

| field | notes |
| --- | --- |
| `file` | relative URL, pass to `/v1/audio` |
| `prompt`, `lyrics` | what was actually used (the model may rewrite the prompt) |
| `metas` | `{bpm, duration, keyscale, timesignature, genres}` |
| `seed_value` | comma-separated; reuse to reproduce a take |
| `dit_model`, `lm_model` | which checkpoints ran |

Poll every **3–5 seconds**. Polling is cheap but there is no push/webhook.

### 3.3 Download

`file` comes back as `/v1/audio?path=<url-encoded absolute path>`. Prefix it
with the base URL and send the auth header:

```bash
curl -o track.mp3 -H "Authorization: Bearer $MUSIC_API_KEY" \
  "$MUSIC_API_URL/v1/audio?path=%2FVolumes%2FStorage%2F..."
```

Files persist on the host and can be re-downloaded later without waking the
model — but they live in a temp cache the server prunes. **Download and store
the bytes in your own storage.** Do not treat the URL as durable.

---

## 4. Request parameters

Only `prompt` is really required. Everything else has a sane default.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `prompt` | string | — | Style description. Genre, instruments, mood, production. |
| `lyrics` | string | `""` | Use `[verse]` / `[chorus]` / `[bridge]` tags. `"[instrumental]"` for no vocals. |
| `audio_duration` | float | model picks | Seconds, 10–600. |
| `audio_format` | string | `mp3` | `mp3`, `flac`, `wav`, `wav32`, `opus`, `aac`. |
| `batch_size` | int | 2 | Takes per request, max 8. Each costs roughly full generation time. |
| `bpm` | int | LM fills | 30–300. |
| `key_scale` | string | LM fills | e.g. `"C Major"`, `"Am"`. |
| `time_signature` | string | LM fills | `"3"`, `"4"`, `"6"`. |
| `vocal_language` | string | `en` | `en`, `zh`, `ja`, … |
| `seed` | int | random | Set with `"use_random_seed": false` to reproduce. |
| `inference_steps` | int | 8 | The turbo model wants 8. Raising it mostly costs time. |
| `thinking` | bool | false | Runs an LM planning pass. Better structure, slower. |
| `task_type` | string | `text2music` | Also `cover`, `repaint`, `lego`, `extract`, `complete`. |

camelCase aliases work too (`audioDuration`, `keyScale`), as do `caption` for
`prompt` and `duration` for `audio_duration`.

Anything not specified gets filled in by a small language model from your
prompt, so a bare `{"prompt": "..."}` produces a complete, coherent track.

---

## 5. Concurrency

**One job runs at a time.** The server has a single worker and an in-memory
queue (max 200). Concurrent submissions are accepted immediately and then run
sequentially — a second caller waits for the first to finish.

- Don't fan out parallel requests expecting parallel speed. You'll just fill the queue.
- Use `batch_size` for variations of one idea; it's cheaper than N separate jobs.
- `GET /v1/stats` returns `{jobs: {queued, running, ...}, queue_size}` if you
  want to show queue depth or apply backpressure.
- The queue is **in memory**. If the host restarts, queued and running jobs are
  lost. Your app must own durable job state and be able to resubmit.

---

## 6. Reference client (Python)

Stdlib only, handles cold starts and the double-encoded `result`.

```python
import json, time, urllib.parse, urllib.request

BASE = os.environ["MUSIC_API_URL"]
KEY = os.environ["MUSIC_API_KEY"]

def _post(path, payload, timeout=1200):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {KEY}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.load(r)
    if body.get("code") != 200:
        raise RuntimeError(body.get("error") or "request failed")
    return body["data"]

def generate(prompt, lyrics="[instrumental]", duration=60, poll=4, timeout=1800):
    task_id = _post("/release_task", {
        "prompt": prompt, "lyrics": lyrics,
        "audio_duration": duration, "audio_format": "mp3",
        "batch_size": 1,
    })["task_id"]

    deadline = time.time() + timeout
    while time.time() < deadline:
        for entry in _post("/query_result", {"task_id_list": [task_id]}):
            if entry["task_id"] != task_id:
                continue
            if entry["status"] == 1:
                takes = json.loads(entry["result"])      # note: double-encoded
                return [_download(t["file"]) for t in takes if t.get("file")]
            if entry["status"] == 2:
                raise RuntimeError(f"generation failed: {entry.get('result')}")
        time.sleep(poll)
    raise TimeoutError(f"task {task_id} still running after {timeout}s")

def _download(file_url):
    url = file_url if file_url.startswith("http") else BASE + file_url
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {KEY}"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()          # bytes — persist these yourself
```

A ready-to-use CLI version lives on the host at `~/dev/AIMusic/generate.py` and
is copyable to any tailnet machine (`--base-url` to point it here).

### TypeScript

```ts
const BASE = process.env.MUSIC_API_URL!;
const KEY = process.env.MUSIC_API_KEY!;

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(BASE + path, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${KEY}` },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(1_200_000), // cold start can take minutes
  });
  const env = await res.json();
  if (env.code !== 200) throw new Error(env.error ?? "request failed");
  return env.data as T;
}

export async function generate(prompt: string, durationSec = 60) {
  const { task_id } = await post<{ task_id: string }>("/release_task", {
    prompt, lyrics: "[instrumental]", audio_duration: durationSec, batch_size: 1,
  });

  for (;;) {
    const rows = await post<any[]>("/query_result", { task_id_list: [task_id] });
    const row = rows.find(r => r.task_id === task_id);
    if (row?.status === 1) return JSON.parse(row.result);  // double-encoded
    if (row?.status === 2) throw new Error(`generation failed: ${row.result}`);
    await new Promise(r => setTimeout(r, 4000));
  }
}
```

---

## 7. Endpoint summary

| Method | Path | Wakes model? | Purpose |
| --- | --- | --- | --- |
| POST | `/release_task` | yes | Submit a job |
| POST | `/query_result` | yes | Poll job status |
| GET | `/v1/audio?path=` | no | Download output |
| GET | `/v1/stats` | yes | Queue depth |
| GET | `/health` | **no** | Liveness + whether the model is loaded |
| GET | `/supervisor/status` | **no** | Idle seconds, memory, in-flight count |
| POST | `/supervisor/start` | yes | Pre-warm; blocks until ready |
| POST | `/supervisor/stop` | no | Unload now, free the RAM |
| GET | `/docs` | yes | Interactive OpenAPI docs |

---

## 8. Expected timings

Measured on this machine (M4, 16 GB), for reference when setting timeouts and
writing progress copy:

| Job | Warm | Cold |
| --- | --- | --- |
| 20 s instrumental | ~1.5 min | ~5 min |
| 30 s instrumental | ~2.5 min | ~6 min |
| 45 s with vocals | ~2.7 min | ~6 min |

Roughly 9 s per diffusion step, 8 steps, plus VAE decode. Longer durations cost
proportionally more. Tell your users it takes minutes, not seconds.

---

## 9. Failure modes worth handling

| Symptom | Cause | What to do |
| --- | --- | --- |
| Connection refused / DNS fails | Not on the tailnet, or host asleep | Check `tailscale status` |
| `401` | Missing or wrong `Authorization` header | — |
| Client timeout on first request | Cold start | Raise timeout to 900s+, or pre-warm |
| `503 backend start failed` | Model couldn't load — usually the SSD is unmounted | Check `/Volumes/Storage` on the host |
| `status: 2` | Generation failed | `result` has the traceback; surface it and let the user retry |
| Job vanished after restart | Queue is in memory | Own your job state; resubmit |

Ask the host owner to run `./verify-models.py` if generation fails repeatedly —
incomplete weight files produce confusing load errors.

---

## 10. Content and licensing

ACE-Step 1.5 is MIT-licensed and runs entirely locally — no prompts, lyrics, or
audio leave the machine. Output rights follow the model licence; if you ship
generated audio in a product, confirm the current upstream terms at
<https://github.com/ace-step/ACE-Step-1.5>.

The model will happily imitate a described style. Don't pass prompts naming a
specific living artist and then publish the result commercially.
