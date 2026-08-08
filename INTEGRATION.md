# Integrating Anneal

**Anneal** is self-hosted **music, speech, image and text** generation running on an Apple silicon Mac, behind one HTTP gateway.

It's named for what it does to its models: heat one up on demand, let it cool
and release the memory when idle.

| Service | Model | Shape | Typical time |
| --- | --- | --- | --- |
| Music | ACE-Step 1.5 | async: submit → poll → download | 1.5–3 min |
| Speech | Kokoro-82M, or Qwen3-TTS CustomVoice for directed delivery | synchronous, returns bytes | 1–2 s, or a few |
| Image | FLUX.1-schnell 4-bit | synchronous, returns bytes | ~2 min |
| Text | Gemma 4 E4B 4-bit | synchronous or streamed | seconds |
| **Press** | all four, in one call | async: submit → poll → download zip | 4 min – 1 hour |
| Sprites | FLUX.1-schnell + rembg | synchronous, returns paths | 2–3 min |

**Interactive API docs are hosted at
<https://your-machine.your-tailnet.ts.net/docs>**, with the raw spec at
`/openapi.json`. Point your tooling there — it is generated from the same
contract described below and is the authoritative reference.

This document covers the things a spec can't tell you: the memory model, the
double-encoded result field, and how to set timeouts.

---

## 1. Connection

| | |
| --- | --- |
| **Tailnet** | `https://your-machine.your-tailnet.ts.net` |
| **On the host itself** | `http://127.0.0.1:8001` |
| **Auth** | `Authorization: Bearer <API_KEY>` on every request |

Anneal binds to loopback only. Reaching it from another machine is opt-in and
goes through `tailscale serve` — set `ANNEAL_EXPOSE=tailnet` on the host. Then
it is on the tailnet and nowhere else: not the public internet, not the LAN.
TLS is terminated by Tailscale with a valid certificate, so no `--insecure` or
cert pinning is needed.

Your API key is generated on first run into `env.local.sh`, which is gitignored.
**Do not commit it.** Read it from an environment variable:

```bash
export ANNEAL_URL=https://your-machine.your-tailnet.ts.net
export ANNEAL_KEY=sk-aimusic-...
```

Requests without a valid key get `401`.

---

## 2. The one thing that will surprise you: cold starts

The machine has 16 GB of RAM. Measured properly, the music model holds ~21 GB
and the image model ~11 GB. Keeping either loaded permanently starved everything else, so
models are **loaded on demand and unloaded after an idle period** — and the two
heavy models are **never resident together**. Asking for an image evicts the
music model, and vice versa. Speech is small enough to coexist with either.

Practical consequences:

| Situation | First-response latency |
| --- | --- |
| Model already warm | generation time only |
| Music cold, or evicted by an image request | **+3–4 minutes** while weights load |
| Image cold | **+30–60 s** |

So a single music request can legitimately take **up to ~8 minutes** end to end.

If your app interleaves music and image work, expect a cold start on *every*
switch. Batch same-modality work together rather than alternating.

**A busy heavy model is never evicted.** If you ask for an image while music is
mid-generation you get a **409** with a `Retry-After` header, not a cold start —
the in-flight job is protected. Handle 409 by waiting and retrying, or by
explicitly stopping the other service if your work is more important:

```bash
curl -X POST -H "Authorization: Bearer $ANNEAL_KEY" \
  -H 'Content-Type: application/json' -d '{"service":"music"}' \
  "$ANNEAL_URL/supervisor/stop"    # abandons its work, frees the slot
```

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
curl -X POST -H "Authorization: Bearer $ANNEAL_KEY" \
  -H 'Content-Type: application/json' -d '{"service":"music"}' \
  "$ANNEAL_URL/supervisor/start"
```

`service` is one of `music`, `speech`, `image`.

It blocks until the model is ready, then returns. Any subsequent request within
the idle window is warm. Each request resets the idle timer.

`GET /health` and `GET /v1/audio` are answered without waking the model, so
health checks and re-downloads are free and instant. Poll `/health` as often as
you like.

---

## 3. Music: the flow

Music generation is asynchronous — three steps. (Speech and image are plain
synchronous calls; see sections 6a and 6b.)

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
curl -X POST "$ANNEAL_URL/release_task" \
  -H "Authorization: Bearer $ANNEAL_KEY" \
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
curl -X POST "$ANNEAL_URL/query_result" \
  -H "Authorization: Bearer $ANNEAL_KEY" \
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

**Jobs now survive a restart.** The queue inside the model backend still lives
in memory, but the gateway records every job it hands out and **replays anything
outstanding when the backend comes back**. Keep polling the id you were given —
the replayed job gets a new id internally, and the gateway translates in both
directions, so the indirection is invisible.

You will therefore see `status: 0` across a restart rather than a failure, and
the result arrives once the replay completes.

**Orphaned jobs** are now the residual case only: a job that can't be replayed
(too old, or it has already failed repeatedly) comes back as `status: 2` with
`"orphaned": true`. Treat that as final and resubmit.

**Transient 502s.** A `502` while polling means the backend was restarting — the
poll failed, not the job. Retry the poll. **Never resubmit on a 502**, or you
queue duplicate work behind the original.

**`result` is a JSON-encoded string, not an object** — parse it a second time.
It decodes to a *list*, one entry per take (see `batch_size`). Each entry has:

| field | notes |
| --- | --- |
| `file` | relative URL, pass to `/v1/audio` |
| `prompt`, `lyrics` | what was actually used (the model may rewrite the prompt) |
| `metas` | `{bpm, duration, keyscale, timesignature, genres}` |
| `seed_value` | comma-separated. Recorded, but it does not make a take reproducible |
| `dit_model`, `lm_model` | which checkpoints ran |

Poll every **3–5 seconds**. Polling is cheap but there is no push/webhook.

### 3.3 Download

`file` is **already a complete request path, including its query string** —
something like `/v1/audio?path=%2FVolumes%2FStorage%2F...`. It is not a bare
filesystem path. Append it to the base URL exactly as given:

```python
url = base_url + take["file"]          # correct
```

```python
url = f"{base_url}/v1/audio?path={quote(take['file'])}"   # WRONG — double-encoded
```

Double-encoding used to fail with `403 Access denied: path outside allowed
directory`, which reads like a permissions problem and sends you hunting in the
wrong place. It now returns a `400` that names the mistake.

Files persist on the host and can be re-downloaded later without waking the
model — but they live in a temp cache the server prunes. **Download and store
the bytes in your own storage.** Do not treat the URL as durable.

---

## 3a. Press: a whole record in one call

Press chains every service — plan, lyrics, music, cover — behind a single
request. It is the highest-value endpoint here and the one most worth wrapping,
because it does in one call what would otherwise be a dozen and gets the model
ordering right.

```bash
curl -X POST "$ANNEAL_URL/v1/press" \
  -H "Authorization: Bearer $ANNEAL_KEY" -H 'Content-Type: application/json' \
  -d '{"prompt": "a short winter album about leaving a coastal town, folk with strings",
       "tracks": 4, "duration": 120, "quality": "draft", "art": true}'
# -> {"data": {"id": "0f2c…", "state": "planning"}}

curl "$ANNEAL_URL/v1/press?id=0f2c…" -H "Authorization: Bearer $ANNEAL_KEY"
```

Poll the same way you poll music — every 3–5 seconds. `state` runs
`planning → writing → recording → artwork → done`, with `stage_note` carrying
human-readable progress and `tracks[]` filling in as each one lands.

| Parameter | Default | Notes |
| --- | --- | --- |
| `prompt` | required | The brief for the whole record. |
| `tracks` | `1` | 1–8. One is a single. |
| `duration` | `90` | Target seconds per track; the planner varies around it. |
| `duration_min` / `duration_max` | 60% / 150% of `duration` | Bounds for that variation. |
| `quality` | `draft` | `draft` or `high`, as for music. |
| `instrumental` | `false` | Skips the lyric stage, and no vocal clause is added. |

**Say who is singing in the brief.** The planner names the lead vocalist once for
the whole record and every track's music prompt carries it. That matters because
each track is its own generation, and a per-track style line describes genre,
instruments and mood rather than a performer — a brief asking for a British
female lead previously came back with male vocals on three tracks of four.

It makes the request consistent, not the performance: nothing in this path
conditions on a speaker, so expect the same *described* singer rather than an
identical voice across tracks.
| `art` | `true` | Generate a cover. This is the only stage needing the image model. |
| `art_size` | `1024x1024` | Cover dimensions. |
| `audio_format` | `flac` | Master format. Downloads transcode from it. |

**Budget for it properly.** A single is a few minutes; eight tracks is most of
an hour. The stage order — every text stage, then every music stage, then the
cover — is deliberate: doing lyrics→music→art per track would evict and reload a
multi-gigabyte model between every step.

**Lyric density follows the genre.** An electronic record came back with full
verse-chorus-verse on every track, because one lyric instruction went to every
genre alike — "two verses and a chorus is plenty". Club music does not work that
way: the vocal is a hook and a handful of lines, and a wall of text sung over it
sounds wrong however well it is written. The planner is now asked how wordy each
track should be; where it does not answer — the 0.6B planner drops fields
regularly — the density is derived from the track's style line, and failing that
from the brief. An unrecognised style falls back to `moderate`, deliberately, not
to `full`: writing the most words possible is the failure being corrected. Send
`lyric_density` (`sparse` | `moderate` | `full`) to decide for the whole record
instead. As with the voice, this constrains the *request*; it does not guarantee
the model obeys.

**Review before the expensive stage.** Press is one-shot by default: a brief in,
twenty minutes later a record out. If the tracklist is wrong or a lyric is weak
the whole run is wasted, and on this hardware that is a real cost.

Send `review: true` and it stops between lyrics and music — after the two cheap
stages that decide what the record will be, before the one that makes it.

```bash
# 1. Submit, asking to review.
curl -X POST "$ANNEAL_URL/v1/press" -H "Authorization: Bearer $ANNEAL_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"a winter album about leaving a coastal town","tracks":4,"review":true}'

# 2. Poll until state is "awaiting-review", then read plan and tracks.
curl "$ANNEAL_URL/v1/press?id=0f2c…" -H "Authorization: Bearer $ANNEAL_KEY"

# 3. Fix what is wrong and continue. Amend and approve in one call.
curl -X POST "$ANNEAL_URL/v1/press/review" -H "Authorization: Bearer $ANNEAL_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"id":"0f2c…",
       "plan":{"artist":"The Salt Line"},
       "tracks":[{"n":3,"title":"Low Tide","lyrics":"[verse]\nBetter words here"}],
       "approve":true}'
```

**Amendments are patches.** Fields you do not send are left alone — changing a
title cannot blank the `voice` that pins the singer across the record. Tracks
match on `n`, and an unrecognised number is ignored rather than appended: a typo
should not add a track the music stage then records.

**A press waiting for review releases the queue.** It holds no model and no
slot, so someone taking an hour over the lyrics blocks nothing behind them. That
also means approving puts it back in the *queue* rather than straight into
running — something else may hold the slot by then. It resumes into the music
stage with your edits and does not re-plan.

Omit `approve` to save edits and keep waiting. Anything not awaiting review —
already recording, finished, never paused — is a 409.

**You can name the record, or be offered names.** Send `title` and `artist` on
`/v1/press` and they are used instead of invented ones; name only one and the
planner still supplies the other. An empty string is not an instruction to
forget a name.

To choose before committing, `POST /v1/press/names {"prompt": "…", "count": 5}`
returns title/artist pairs for the brief. It is one call on the text model —
light, coexists with a heavy one, and finishes in seconds — so it fits before
the twenty minutes rather than after.

```bash
curl -X POST "$ANNEAL_URL/v1/press/names" -H "Authorization: Bearer $ANNEAL_KEY" \
  -H 'Content-Type: application/json' -d '{"prompt":"a winter album","count":5}'
# -> {"data": {"names": [{"title": "Winter Roads", "artist": "The Salt Line"}, …]}}
```

**Another record from the same artist.** Press invents an artist, a singer and a
register, and they used to die with the record. `GET /v1/artists` lists everyone
with a finished record — newest first, with the voice and lyric density to reuse
and a cover to show — and `adopt_artist` on a new press fills them in.

```bash
curl "$ANNEAL_URL/v1/artists" -H "Authorization: Bearer $ANNEAL_KEY"
# -> {"data": {"artists": [{"name": "The Salt Line", "records": 2,
#      "voice": "a British woman, warm alto", "lyric_density": "full", …}]}}

curl -X POST "$ANNEAL_URL/v1/press" -H "Authorization: Bearer $ANNEAL_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"a second record, harder and faster","adopt_artist":"The Salt Line"}'
```

Precedence is explicit field, then artist, then planner — so adopting gives you
the singer and the register back while leaving you free to change either. The
**title is never inherited**: a new record needs its own name. An unknown artist
is a 400 rather than quietly becoming a new one.

The list is derived from the presses rather than stored separately, so it needs
no maintenance and covers records made before this existed. It reads sqlite and
wakes no model.

**Tracks are asked to end properly.** `outro` defaults to true. Before it
existed, five of eight measured tracks played at full level to the last bar,
stopped dead, and were padded with two to six seconds of silence to reach the
requested duration — nothing in the prompt ever said the piece had to finish.
Set `outro: false` for loops or background beds, where a resolved ending is
exactly wrong.

This is **Press-only**. `POST /release_task` never adds it: a builder asking
that endpoint for a two-bar loop wants it to loop, and an outro welded onto
every music request would ruin the use the API exists for.

**Trailing silence is trimmed** from every saved take, Press or not. That
padding is not music — it reads as part of the track in a player and breaks a
loop outright. Only the tail is touched; a rest inside the arrangement and any
lead-in are left alone, and a FLAC master stays lossless. Set
`ANNEAL_TRIM_SILENCE=0` on the server to keep the raw output.

**Downloading.** `GET /v1/press/download?id=…&format=mp3&bitrate=320k` returns
the whole record as a zip: audio, cover and tracklist. Masters are FLAC, so a
lossy format is transcoded from the original rather than from another lossy copy.
Omit `format` for the masters as they are.

**Interruption and cancellation.** A press outlives the client, so closing your
connection does not stop it. If the gateway restarts mid-run the record is marked
`interrupted` rather than left claiming to work, and `POST /v1/press/resume`
`{"id": …}` finishes it — finished tracks are kept, only the missing work is
redone. `POST /v1/press/cancel` `{"id": …}` stops one deliberately and frees the
heavy slot; a track already in flight may still land, and a cancelled press
cannot be resumed. `DELETE /v1/press?id=…` removes the record, with `&files=1`
to take its audio and cover with it.

**One at a time, and the gateway enforces it.** Press assumes it owns the model
ordering for its whole run, so a second submission is **queued** rather than
started alongside — and rather than refused, which would lose the brief you just
typed.

A queued press returns `state: "queued"` with a 1-based `queue_position` and a
rough `estimated_start_seconds`, and carries both on `GET /v1/press?id=` until it
starts. Treat the estimate as an order of magnitude: it is built from what each
waiting request asked for, not from history.

You do not need to serialise anything your side. Submit, poll, and it will run
when it reaches the front. The queue survives a gateway restart — anything
waiting is still waiting, the press that was mid-run is marked `interrupted` and
can be resumed, and the next one starts on its own.

---

## 4. Request parameters

Only `prompt` is really required. Everything else has a sane default.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `prompt` | string | — | Style description. Genre, instruments, mood, production. |
| `lyrics` | string | `""` | Use `[verse]` / `[chorus]` / `[bridge]` tags. `"[instrumental]"` for no vocals. |
| `audio_duration` | float | model picks | Seconds, 10–600. |
| `audio_format` | string | **`flac`** | Lossless by default. The backend's `mp3` is fixed at 128 kbps with no bitrate control — audibly lossy. |
| `batch_size` | int | 2 | Takes per request, max 8. Each costs roughly full generation time. |
| `bpm` | int | LM fills | 30–300. |
| `key_scale` | string | LM fills | e.g. `"C Major"`, `"Am"`. |
| `time_signature` | string | LM fills | `"3"`, `"4"`, `"6"`. |
| `vocal_language` | string | `en` | `en`, `zh`, `ja`, … |
| `seed` | int | random | Recorded in the result. Two identical seeded requests still differ — see README → Determinism. |
| `inference_steps` | int | 8 | The turbo model wants 8. Raising it mostly costs time. |
| `thinking` | bool | **true** | Planning pass — sections, key, arrangement. ~30 s. Anneal defaults this on; the backend does not. |
| `quality` | string | `draft` | `draft` (turbo, ~90 s) or `high` (sft, ~180 s, better detail). Switching restarts the model once. |
| `task_type` | string | `text2music` | Also `cover`, `repaint`, `lego`, `extract`, `complete`. |

camelCase aliases work too (`audioDuration`, `keyScale`), as do `caption` for
`prompt` and `duration` for `audio_duration`.

Anything not specified gets filled in by a small language model from your
prompt, so a bare `{"prompt": "..."}` produces a complete, coherent track.

---

### Defaults

Anneal overrides two upstream defaults because they materially change output,
and leaving them would make every integrator rediscover the same two things:
`thinking` is **on**, and `audio_format` is **flac**. Send either field to
override.

`metas` in a result (`bpm`, `keyscale`) is what the planning LM *asked for*, not
an analysis of the audio. It can be plainly wrong while the audio is fine. Set
`bpm`/`key_scale` on the request if you need them to mean something.

## 5. Concurrency

**One job runs at a time.** The server has a single worker and an in-memory
queue (max 200). Concurrent submissions are accepted immediately and then run
sequentially — a second caller waits for the first to finish.

- Don't fan out parallel requests expecting parallel speed. You'll just fill the queue.
- Use `batch_size` for variations of one idea; it's cheaper than N separate jobs.
- `GET /v1/stats` returns `{jobs: {queued, running, ...}, queue_size}` if you
  want to show queue depth or apply backpressure.
- The backend's queue is **in memory**, but the gateway records every job and
  replays anything outstanding after a restart — keep polling the id you were
  given. Only a job that comes back `status: 2` with `orphaned: true` needs
  resubmitting.

---

## 6. Reference client (Python)

Stdlib only, handles cold starts and the double-encoded `result`.

```python
import json, os, time, urllib.parse, urllib.request

BASE = os.environ["ANNEAL_URL"]
KEY = os.environ["ANNEAL_KEY"]

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

`generate.py` at the repo root is a ready-made stdlib version of this. It has
no imports from the rest of the repo, so it copies to any machine that can reach
the gateway — point it with `--base-url`.

### TypeScript

```ts
const BASE = process.env.ANNEAL_URL!;
const KEY = process.env.ANNEAL_KEY!;

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

## 6a. Speech

Synchronous — post text, get audio bytes back. No polling.

```bash
curl -X POST "$ANNEAL_URL/v1/audio/speech" \
  -H "Authorization: Bearer $ANNEAL_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"input":"Your build finished successfully.","voice":"bf_emma","response_format":"mp3"}' \
  -o narration.mp3
```

| Field | Default | Notes |
| --- | --- | --- |
| `input` | — | Required. Text to speak. |
| `voice` | `af_heart` | `GET /v1/voices` lists all 37. Prefix encodes language and gender: `a`=American, `b`=British, `f`=female, `m`=male. |
| `response_format` | `wav` | `wav`, `mp3`, `flac`, `opus`, `aac`. |
| `speed` | `1.0` | 0.5–2.0. |

Roughly 1–2 s per sentence once warm; the model is only ~350 MB so it loads in
about a second and can stay resident alongside music or image work. Long inputs
scale linearly — chunk anything book-length and concatenate client-side.

This is the OpenAI `/v1/audio/speech` shape, so most OpenAI TTS client code
works by changing the base URL, key, and voice name.

### Two speech models, chosen by the voice

There is no model parameter. The two voice sets do not overlap, so naming a
voice picks the model — and an existing caller keeps working untouched.

| | voices | size | per line | direction |
| --- | --- | --- | --- | --- |
| **Kokoro** (default) | 28, `af_*` `am_*` `bf_*` `bm_*` | 350 MB | 1–2 s | none |
| **Qwen3-TTS CustomVoice** | `serena` `vivian` `uncle_fu` `ryan` `aiden` `ono_anna` `sohee` `eric` `dylan` | 2.3 GB | a few seconds | `instruct` |

```bash
curl -X POST "$ANNEAL_URL/v1/audio/speech" \
  -H "Authorization: Bearer $ANNEAL_KEY" -H 'Content-Type: application/json' \
  -d '{"input":"Get back! The whole tunnel is coming down on top of us!",
       "voice":"ryan",
       "instruct":"Shouting a desperate warning, panicked and breathless."}' \
  --output line.wav
```

**`instruct` directs the performance, not the person.** The speaker is fixed by
`voice`, so the same character can be panicked in one line and quietly furious
in the next. Measured on one voice across three directions: RMS 0.080 panicked,
0.054 calm, **0.021** for "quietly furious" — it goes *quieter* for quiet fury
rather than simply adding energy.

**Sending `instruct` with a Kokoro voice is a 400, deliberately.** Kokoro has no
expressive control at all — verified against the installed package, its
`generate()` takes text, voice, speed and a language code and nothing else. A
caller who sent a direction and got flat delivery would have no way to tell
whether the model tried and failed or the field was dropped, so the error says
so and names the voices that can.

**Why not a written voice description.** The VoiceDesign variant of the same
family takes a description instead of a speaker name, which sounds better until
you use it: it designs a fresh voice on every call, so a character comes back as
a different person on the next line. A same-seed checksum test said it was
stable — it was, but only for identical text, which is not the case that
matters. Identity and performance have to be separate knobs.

`GET /v1/voices` returns every voice with its `backend` and `supports_instruct`.
Build the control from that rather than a hardcoded list.

## 6b. Images

Also synchronous, but slow — budget ~2 minutes per 1024x1024 image.

```bash
curl -X POST "$ANNEAL_URL/v1/images/generations" \
  -H "Authorization: Bearer $ANNEAL_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"a lighthouse in a storm, oil painting","size":"1024x1024","steps":4}'
```

→ `{"created": ..., "data": [{"b64_json": "...", "path": "...", "seed": 7, "seconds": 122.4}]}`

| Field | Default | Notes |
| --- | --- | --- |
| `prompt` | — | Required. |
| `size` | `1024x1024` | Snapped to a multiple of 16, max ~1536x1536. Smaller is proportionally faster. |
| `steps` | `4` | schnell is distilled for 4 steps; more mostly costs time. |
| `n` | `1` | Max 4. Each costs a full generation. |
| `seed` | random | With `n>1` seeds increment from this. |
| `response_format` | `b64_json` | Use `path` to skip base64 and fetch via `/v1/images/file?path=`. |
| `init_image` | — | Absolute `path` from a previous result. Makes this a **variation** of that image. Paths outside the server's output directory are rejected with 400. |
| `retention` | `0.7` | 0.3–0.95. How much of `init_image` survives. Ignored without it. |

### Variations are another take, not an edit

`init_image` keeps the composition, subject and lighting of an earlier image and
re-renders its detail. It does **not** apply prompt changes to that image.
Measured on schnell: asking a brass watch to become silver returns the same
brass watch, and asking black velvet to become red returns black velvet, at
every retention down to 0.5 with six free steps. The init latent dominates.

The reason is arithmetic. The server spends `int(steps * retention)` steps
reproducing the original, and schnell is distilled to four — so keeping the
image spends the very steps that would redraw it. Requests carrying
`init_image` therefore default to **8** steps rather than 4, so that 0.85,
0.7 and 0.55 have genuinely different budgets; at 4 steps two of those three
produce byte-identical files. An explicit `steps` still wins.

Responses for a variation carry `derived_from` and `retention` alongside the
usual fields, and the saved sidecar records both.

Use it for: colour and texture variants, damage states, tile variations, another
take of the same shot. Not for: changing what is in the picture.

`b64_json` inflates a ~1.3 MB PNG to ~1.7 MB of JSON. For anything
latency-sensitive use `"response_format": "path"` and fetch the bytes separately.

Remember this evicts the music model.

## 6c. Sprites: an animation set that stays the same character

`POST /v1/sprites` takes a brief and returns a set of transparent PNGs, one per
frame, plus an atlas describing where each came from.

```bash
curl -X POST "$ANNEAL_URL/v1/sprites" \
  -H "Authorization: Bearer $ANNEAL_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"a small round green slime with big eyes",
       "poses":["idle","crouched to jump","stretched mid-air","landing splat"],
       "style":"flat pixel art"}'
```

```json
{"data": {
  "subject": "a small round green slime with big eyes",
  "requested_frames": 4,
  "source_size": [1344, 768],
  "frame_dir": "…/outputs/sprites/20260807-153815-a-small-round-green-slime",
  "frames": [
    {"index": 0, "x": 112, "y": 126, "width": 218, "height": 230,
     "file": "…-00.png", "url": "/v1/outputs/file?path=…"},
    {"index": 1, "x": 388, "y": 126, "width": 218, "height": 230,
     "file": "…-01.png", "url": "/v1/outputs/file?path=…"}
  ],
  "sheet": "…/outputs/images/…png",
  "sheet_url": "/v1/images/file?path=…"
}, "code": 200, "error": null}
```

| Field | Default | Notes |
| --- | --- | --- |
| `prompt` | — | Required. Describe the character as you would to an artist; "a small round green slime with big eyes" works, "slime" does not. |
| `poses` | — | One description per frame, in order. Sets the frame count. This is the field that produces motion — see below. |
| `frames` | `4` | 2–8. A hint, not a contract. Ignored when `poses` is given. |
| `style` | `flat pixel art` | Art direction, e.g. `hand-drawn ink`, `3D clay render`. |
| `size` | `1344x768` | The sheet canvas. |
| `wait` | `300` | Seconds to wait for the heavy slot if music is mid-generation. |

**Every frame comes out of one generation, and that is the point.** Generating
four sprites separately gives you four different characters — the model has no
memory between samples, so armour, palette and proportions all drift. That was
measured on both plain prompting and `init_image`; a variation keeps the
composition, which is exactly the wrong thing when you want the pose to change
and nothing else. Asking for a single picture containing every pose is what
makes them the same character, and it works: five slimes came back with
identical eyes, palette and proportions.

**Count what you got, don't assume it.** Two runs asking for four frames each
returned five, arranged 3+2 across two rows. Diffusion does not count, and no
amount of prompt wording makes it. The response's `frames` array is the truth;
`requested_frames` is only what was asked for. Any caller that indexes 0..3
because it asked for 4 will be wrong.

**Name the poses if you want animation.** This is the real limit and it is worth
being plain about. Asked for "the same character with only the pose changing",
the model returns identity and essentially no motion — the measured run was five
near-identical standing slimes, useful as a character set and useless as a walk
cycle. Passing `poses` fixed that: idle, crouch, mid-air and splat came back
visibly different. The cost is that the design drifts more between frames —
arms and a mouth appeared in some poses and not others. At four schnell steps
the model cannot fully serve both constraints, so choose which one the asset
needs. This is the temporal-coherence problem video models exist to solve, met
halfway rather than solved.

**The sheet is not a grid.** Poses are spaced irregularly, at different sizes,
across however many rows the model chose, so frames are located by content
rather than by dividing the canvas — reading order is left to right, then top to
bottom. The soft drop shadow under each sprite is excluded rather than cut as a
frame of its own. Cell-slicing a real sheet cuts characters in half.

**Two methods, and one of them changes what you may do with Anneal.** `method`
defaults to `sheet`, which is what everything above describes: one
FLUX.1-schnell generation containing every pose, cut up. Apache-2.0.

`method: "kontext"` generates one base sprite and then edits it once per pose,
making the pose an instruction rather than something you hope falls out of a
single sample. It is **declared but not implemented** and returns 501 today.

When it is wired, note the licence: it uses **FLUX.1 Kontext [dev], which is
non-commercial.** The model may not be used commercially without a licence from
Black Forest Labs, although outputs are your own. Everything else Anneal runs is
Apache-2.0 or MIT except Gemma, so this is the one component that constrains
what you can do with the whole system — which is why it is opt-in, never the
default, and stated here rather than only in the code. It also needs a separate
~9 GB download.

**Backgrounds are removed with a segmentation model.** Colour keying is the
fallback and its limit is why: a white robot on a white sheet came out
see-through, its background readable through its head. Pale characters are
ordinary, not an edge case. The cut runs in a separate interpreter, set by
`ANNEAL_SPRITE_PYTHON`, because it needs rembg and the environment that serves
the models is version-pinned; a host without one gets a 503 saying so rather
than a silent fallback.

Synchronous, measured at **2 min 8 s** end to end at the default size on both
runs — one image generation plus a few seconds to cut. It evicts music.

**A set is one asset, and it comes with a preview.** The frames land in their
own directory alongside an `atlas.json` and an animated `preview.gif` built from
them. The library lists the set as a **single row** represented by that preview,
rather than one row per frame sorted among unrelated output — a four-pose walk
cycle arriving as four unconnected entries was the previous behaviour and it
made a set impossible to find again.

The response carries `preview` and `preview_url`. `fps` (default 8) sets the
preview's frame rate and changes nothing about the frames, which are what a game
loads. Frames are padded onto a common canvas and bottom-aligned rather than
resized, so the character stands on a consistent floor; where the source poses
genuinely differ in scale, the preview shows that rather than hiding it.

**When the sheet can't be cut** you get a 502 that still carries `sheet` and
`sheet_url`. The model occasionally returns one scene rather than separate
poses; the image cost minutes and is in the library either way, so it is handed
back rather than thrown away.

## 7. Endpoint summary

| Method | Path | Wakes model? | Purpose |
| --- | --- | --- | --- |
| POST | `/release_task` | music | Submit a music job |
| POST | `/query_result` | music | Poll job status |
| GET | `/v1/audio?path=` | **no** | Download generated music |
| GET | `/v1/stats` | music | Queue depth |
| POST | `/v1/audio/speech` | speech | Synthesize speech, returns bytes |
| GET | `/v1/voices` | speech | List voices |
| POST | `/v1/images/generations` | image | Generate images, or a variation of one |
| GET | `/v1/images/file?path=` | image | Download a generated image |
| POST | `/v1/chat/completions` | text | OpenAI-shaped chat, streaming supported |
| POST | `/v1/text` | text | One-shot prompt in, text out |
| POST | `/v1/vector` | text (light) | Draw an SVG icon. **Experimental** — see below |
| POST | `/v1/sprites` | image | An animation set as separate transparent PNGs |
| POST | `/v1/press` | in stages | Start a record: plan, lyrics, music, cover |
| GET | `/v1/press?id=` | **no** | Poll a press, or list recent ones without `id` |
| GET | `/v1/press/download?id=` | **no** | The whole record as a zip, transcoded on request |
| POST | `/v1/press/resume` | in stages | Finish a press left `interrupted` by a restart |
| POST | `/v1/press/review` | **no** | Amend and/or approve a press paused for review |
| POST | `/v1/press/names` | text (light) | Suggest titles and artist names for a brief |
| GET | `/v1/artists` | **no** | Artists you have made records with, and their voices |
| POST | `/v1/press/cancel` | **no** | Stop a press deliberately; keeps finished tracks |
| DELETE | `/v1/press?id=` | **no** | Remove a record; `&files=1` takes its audio too |
| GET | `/v1/outputs` | **no** | The library, filterable by `kind` |
| GET | `/v1/outputs/file?path=` | **no** | Download any saved output |
| DELETE | `/v1/outputs?path=` | **no** | Delete a saved output and its sidecar |
| GET | `/v1/music/tiers` | **no** | Which music model is loaded, and what else exists |
| GET | `/health` | **no** | Per-service state |
| GET | `/supervisor/status` | **no** | Idle seconds, memory, in-flight count |
| GET | `/supervisor/auth` | **no** | Whether you are already authenticated, and how |
| POST | `/supervisor/start` | yes | Pre-warm; blocks until ready |
| POST | `/supervisor/stop` | **no** | Unload now, free the RAM |
| GET | `/docs`, `/openapi.json` | **no** | Interactive docs and raw spec |

Anything marked "wakes model: no" is answered by the gateway itself off disk, so
polling, browsing the library and downloading finished work never cost a cold
start.

### 7a. Vector — the one fast endpoint, and its limits

`POST /v1/vector {"prompt": "a compass rose", "style": "line", "size": 48}`
returns SVG source in **2–7 seconds**. It runs on the text model, which is
light and coexists with a heavy one, so unlike every other generative endpoint
here it evicts nothing and cold-starts nothing (unless chat itself is cold).
That makes it the only endpoint an agent can reasonably call in a loop.

Everything returned has been through a sanitiser: single `<svg>` root, allowlist
of drawing elements, no `<script>`, no `<style>`, no `<foreignObject>`, no `on*`
handlers, no SMIL, and no `href`/`url()` outside a local `#fragment`.
`sanitised_out` in the response lists anything that was taken — if it is
non-empty, the model emitted something it should not have, which is worth
logging on your side too.

**Do not ship this into a product yet.** Measured on the model that fits this
hardware, output is well-formed 5 times in 5 and a recognisable icon 0 times in
5: a gear rendered as a plain circle, a compass rose as a single dot, a health
bar as one solid rectangle. The README has the full table. Two attempts are made
per request; `422` means both failed to parse, which is a generation failure and
not something to retry indefinitely.

---

## 8. Expected timings

Measured on this machine (M4, 16 GB), for reference when setting timeouts and
writing progress copy:

| Job | Warm | Cold |
| --- | --- | --- |
| Speech, one sentence | ~1.2 s | ~3 s |
| 20 s instrumental | ~1.5 min | ~5 min |
| 30 s instrumental | ~2.5 min | ~6 min |
| 45 s with vocals | ~2.7 min | ~6 min |
| 1024x1024 image, 4 steps | ~2 min | ~2.5 min |

Music runs roughly 9 s per diffusion step across 8 steps, plus VAE decode;
longer durations cost proportionally more. Tell your users music and images take
minutes, not seconds — speech is the only one that feels instant.

---

## 9. Failure modes worth handling

| Symptom | Cause | What to do |
| --- | --- | --- |
| Connection refused / DNS fails | Not on the tailnet, or host asleep | Check `tailscale status` |
| `401` | Missing or wrong `Authorization` header | — |
| Client timeout on first request | Cold start | Raise timeout to 900s+, or pre-warm |
| `503 backend start failed` | Model couldn't load — often the install root is missing, e.g. an unmounted external disk | Run `./anneal doctor` on the host |
| `503` with `reason: host_memory_exhausted` | The machine is out of RAM; Anneal refused to start a model rather than thrash | Close other apps, or `POST /supervisor/stop`. The body carries the memory figures |
| `409` on an image or music request | The other heavy model is mid-job | Wait and retry, or stop it explicitly |
| `502` while polling | Backend restarting | Retry the poll. **Never resubmit** |
| `400 ... looks double-encoded` | You re-encoded the `file` field | Append `file` to the base URL as-is |
| `413` on any POST | Request body over `ANNEAL_MAX_REQUEST_BYTES` (2 MB) | Shorten it. The connection is closed, so reconnect |
| `400 ... over the … second limit` on `/v1/press` | `tracks` × `duration_max` exceeds `ANNEAL_MAX_PRESS_SECONDS` (1800 s) | Fewer or shorter tracks. Checked at submit, so you find out immediately rather than an hour in |
| `413` on `/v1/press/download` | The FLAC masters exceed the zip ceiling | Fetch tracks individually from `/v1/outputs/file` |
| `status: 2`, `orphaned: true` | Backend restarted; queue was lost | Resubmit — it is not coming back |
| `status: 2` otherwise | Generation failed | `result` has the traceback; surface it and let the user retry |
| Polls forever at `status: 0` | Should no longer happen — report it | Cross-check `GET /v1/stats` for `queued`/`running` |

Run `./anneal verify` if generation fails repeatedly — incomplete weight files
produce confusing load errors.

---

## 10. Content and licensing

Every model and library, with its licence and who wrote it, is listed in the
README's [Credits](README.md#credits) section and on the UI's About page. One to
know before you redistribute anything: the text model's weights (Gemma) are
under Google's **Gemma Terms of Use**, not an OSI licence — the MLX conversion
tooling around them is MIT.

ACE-Step 1.5 is MIT-licensed and runs entirely locally — no prompts, lyrics, or
audio leave the machine. Output rights follow the model licence; if you ship
generated audio in a product, confirm the current upstream terms at
<https://github.com/ace-step/ACE-Step-1.5>.

The model will happily imitate a described style. Don't pass prompts naming a
specific living artist and then publish the result commercially.
