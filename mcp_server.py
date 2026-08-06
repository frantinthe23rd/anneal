#!/usr/bin/env python3
"""MCP server exposing Anneal's generation services as tools.

Speaks JSON-RPC 2.0 over stdio. Stdlib only, so it runs under any Python 3.9+
without an environment of its own.

Design notes, because they are not obvious:

* **Music does not block.** A music job takes minutes and can take eight from
  cold. A tool call that blocks that long is hostile to every client, so
  `generate_music` submits and returns a job id, and `check_music_job` collects.
  The agent can do other work in between.
* **Binary comes back as a path, not base64.** A 1.3 MB PNG becomes ~1.7 MB of
  text in a tool result, which is a poor use of anyone's context. Callers get a
  filesystem path and read it if they actually need the bytes.
* **Cold-start cost is in the tool descriptions**, because otherwise an agent
  sees a slow call, assumes failure, and retries — queueing duplicate work.

Configure with ANNEAL_URL and ANNEAL_KEY (falls back to ACESTEP_API_KEY).

    ANNEAL_URL=http://127.0.0.1:8001 ANNEAL_KEY=sk-... ./mcp_server.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("ANNEAL_URL", "http://127.0.0.1:8001").rstrip("/")
KEY = os.environ.get("ANNEAL_KEY") or os.environ.get("ACESTEP_API_KEY", "")
PROTOCOL_VERSION = "2025-06-18"


# --------------------------------------------------------------- transport
def call_api(path, payload=None, method=None, timeout=1200):
    method = method or ("POST" if payload is not None else "GET")
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={
        "Authorization": "Bearer %s" % KEY,
        **({"Content-Type": "application/json"} if data else {}),
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        ctype = resp.headers.get("Content-Type", "")
    if "application/json" in ctype:
        return json.loads(raw.decode("utf-8"))
    return raw


def envelope_data(body):
    """Unwrap Anneal's {data, code, error} envelope, raising on a carried error."""
    if isinstance(body, dict) and "code" in body:
        if body.get("code") != 200:
            raise RuntimeError(body.get("error") or "request failed")
        return body.get("data")
    return body


# ------------------------------------------------------------------ tools
def tool_service_status(_args):
    data = envelope_data(call_api("/health", timeout=15))
    services = data.get("services", {})
    lines = ["%-7s %-8s %s" % (
        name, s.get("state", "?"),
        ("%.1f GB" % (s["memory_mb"] / 1024)) if s.get("memory_mb") else "",
    ) for name, s in services.items()]
    system = data.get("system", {})
    if system.get("pressure"):
        lines.append("HOST UNDER MEMORY PRESSURE — generation will be slow and may fail.")
    lines.append("free %s MB, swap %s MB" % (system.get("free_mb"), system.get("swap_used_mb")))
    return "\n".join(lines)


def tool_prewarm(args):
    service = args.get("service", "music")
    envelope_data(call_api("/supervisor/start", {"service": service}, timeout=1200))
    return "%s is loaded and ready." % service


def tool_unload(args):
    payload = {"service": args["service"]} if args.get("service") else {}
    envelope_data(call_api("/supervisor/stop", payload, timeout=120))
    return "Unloaded %s; memory released." % (args.get("service") or "all services")


def tool_generate_music(args):
    body = {
        "prompt": args["prompt"],
        "lyrics": args.get("lyrics") or "[instrumental]",
        "audio_duration": args.get("duration_seconds", 30),
        "batch_size": args.get("takes", 1),
        "audio_format": args.get("format", "flac"),
    }
    for key in ("bpm", "key_scale", "seed"):
        if args.get(key) is not None:
            body[key] = args[key]
    if args.get("seed") is not None:
        body["use_random_seed"] = False

    data = envelope_data(call_api("/release_task", body, timeout=1200))
    task_id = data.get("task_id")
    return ("Submitted music job %s.\n"
            "This takes 1.5-3 minutes warm, or up to 8 from cold. "
            "Call check_music_job with this id — do NOT resubmit." % task_id)


def tool_check_music_job(args):
    task_id = args["task_id"]
    rows = envelope_data(call_api("/query_result", {"task_id_list": [task_id]}, timeout=120)) or []
    row = next((r for r in rows if r.get("task_id") == task_id), None)
    if row is None:
        return "No such job %s." % task_id

    status = row.get("status")
    if status == 0:
        return "Job %s is still running. Check again in 30 seconds." % task_id
    if status == 2:
        if row.get("orphaned"):
            return ("Job %s was lost and could not be replayed. It is not running — "
                    "submit it again." % task_id)
        return "Job %s failed: %s" % (task_id, str(row.get("result"))[:400])

    takes = []
    try:
        takes = json.loads(row["result"])
    except Exception:
        pass

    out = ["Job %s finished with %d take(s):" % (task_id, len(takes))]
    for take in takes:
        if not take.get("file"):
            continue
        # `file` is a complete request path already — appending is correct,
        # re-encoding it is the classic mistake here.
        saved = _save_binary(take["file"], args.get("save_dir"),
                             os.path.splitext(urllib.parse.unquote(take["file"]))[1] or ".flac")
        meta = take.get("metas") or {}
        out.append("  %s  (%ss, %s bpm, %s)" % (
            saved, meta.get("duration"), meta.get("bpm"), meta.get("keyscale")))
    return "\n".join(out)


def tool_generate_speech(args):
    blob = call_api("/v1/audio/speech", {
        "input": args["text"],
        "voice": args.get("voice", "af_heart"),
        "speed": args.get("speed", 1.0),
        "response_format": "mp3",
    }, timeout=600)
    path = _write_bytes(blob, args.get("save_dir"), "speech", ".mp3")
    return "Wrote speech to %s" % path


def tool_generate_image(args):
    data = call_api("/v1/images/generations", {
        "prompt": args["prompt"],
        "size": args.get("size", "1024x1024"),
        "steps": args.get("steps", 4),
        "n": 1,
        "response_format": "path",
    }, timeout=1800)
    entries = (data or {}).get("data") or []
    if not entries:
        return "No image was produced."
    first = entries[0]
    return "Wrote image to %s (seed %s, %.0fs)" % (
        first.get("path"), first.get("seed"), first.get("seconds", 0))


# ------------------------------------------------------------------ files
def _write_bytes(blob, save_dir, stem, suffix):
    directory = save_dir or os.path.join(os.path.expanduser("~"), "anneal-output")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "%s-%d%s" % (stem, abs(hash(blob)) % 10**8, suffix))
    with open(path, "wb") as fh:
        fh.write(blob)
    return path


def _save_binary(file_path, save_dir, suffix):
    blob = call_api(file_path, timeout=600)
    if isinstance(blob, (dict, list)):
        return "(unexpected response for %s)" % file_path
    return _write_bytes(blob, save_dir, "music", suffix)


# ------------------------------------------------------------------ schema
TOOLS = [
    {
        "name": "service_status",
        "description": ("Which Anneal models are loaded (cold/heating/hot), how much memory each "
                        "holds, and whether the host is under memory pressure. Cheap and instant — "
                        "never wakes a model. Call this before a big job to decide whether to "
                        "pre-warm."),
        "inputSchema": {"type": "object", "properties": {}},
        "handler": tool_service_status,
    },
    {
        "name": "prewarm",
        "description": ("Load a model ahead of time. BLOCKS until ready: about 3-4 minutes for "
                        "music, 30-60 seconds for image. Use when you know work is coming so the "
                        "user doesn't absorb the cold start."),
        "inputSchema": {
            "type": "object",
            "properties": {"service": {"type": "string", "enum": ["music", "speech", "image"]}},
            "required": ["service"],
        },
        "handler": tool_prewarm,
    },
    {
        "name": "unload_models",
        "description": "Unload a model (or all of them) to free memory immediately.",
        "inputSchema": {
            "type": "object",
            "properties": {"service": {"type": "string", "enum": ["music", "speech", "image"]}},
        },
        "handler": tool_unload,
    },
    {
        "name": "generate_music",
        "description": ("Start generating a music track from a description. Returns a job id "
                        "IMMEDIATELY — generation takes 1.5-3 minutes warm and up to 8 from cold. "
                        "Poll with check_music_job. NEVER resubmit a job that seems slow; that "
                        "queues duplicate work behind the original."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Genre, instruments, mood, production."},
                "lyrics": {"type": "string", "description": "Use [verse]/[chorus] tags. Omit for instrumental."},
                "duration_seconds": {"type": "number", "default": 30, "minimum": 10, "maximum": 600},
                "takes": {"type": "integer", "default": 1, "minimum": 1, "maximum": 4},
                "bpm": {"type": "integer", "minimum": 30, "maximum": 300},
                "key_scale": {"type": "string", "description": "e.g. 'F# minor'"},
                "seed": {"type": "integer", "description": "Set to reproduce a previous take."},
                "format": {"type": "string", "enum": ["flac", "wav", "mp3"], "default": "flac",
                           "description": "flac/wav are lossless. mp3 is capped at 128 kbps and audibly lossy."},
            },
            "required": ["prompt"],
        },
        "handler": tool_generate_music,
    },
    {
        "name": "check_music_job",
        "description": ("Check a music job. Returns still-running, failed, or the paths of the "
                        "finished audio. Poll roughly every 30 seconds."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "save_dir": {"type": "string", "description": "Where to write the audio. Defaults to ~/anneal-output."},
            },
            "required": ["task_id"],
        },
        "handler": tool_check_music_job,
    },
    {
        "name": "generate_speech",
        "description": ("Speak text aloud and write it to an audio file. Fast — about a second "
                        "per sentence. Returns the file path."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "voice": {"type": "string", "default": "af_heart",
                          "description": "a=American b=British, f=female m=male. e.g. bf_emma, am_michael."},
                "speed": {"type": "number", "default": 1.0, "minimum": 0.5, "maximum": 2.0},
                "save_dir": {"type": "string"},
            },
            "required": ["text"],
        },
        "handler": tool_generate_speech,
    },
    {
        "name": "generate_image",
        "description": ("Generate an image and write it to a PNG. BLOCKS for roughly two minutes "
                        "per 1024px image, plus cold start. Note this EVICTS the music model — "
                        "only one heavy model fits in memory. Returns the file path."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "size": {"type": "string", "default": "1024x1024", "description": "WxH, max ~1536x1536. Smaller is faster."},
                "steps": {"type": "integer", "default": 4, "minimum": 1, "maximum": 20},
            },
            "required": ["prompt"],
        },
        "handler": tool_generate_image,
    },
]
HANDLERS = {t["name"]: t["handler"] for t in TOOLS}
TOOL_SCHEMA = [{k: v for k, v in t.items() if k != "handler"} for t in TOOLS]


# ------------------------------------------------------------------- loop
def respond(msg_id, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def handle(msg):
    method, msg_id = msg.get("method"), msg.get("id")

    if method == "initialize":
        respond(msg_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "anneal", "version": "1.0.0"},
        })
    elif method == "tools/list":
        respond(msg_id, {"tools": TOOL_SCHEMA})
    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        handler = HANDLERS.get(name)
        if handler is None:
            respond(msg_id, error={"code": -32602, "message": "unknown tool %r" % name})
            return
        try:
            text = handler(params.get("arguments") or {})
            respond(msg_id, {"content": [{"type": "text", "text": text}]})
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            # Surface 409 and 503 as tool errors with their explanation intact —
            # they are actionable, not mysteries.
            respond(msg_id, {"content": [{"type": "text", "text": "Anneal returned %d: %s" % (exc.code, detail)}],
                             "isError": True})
        except Exception as exc:
            respond(msg_id, {"content": [{"type": "text", "text": "%s: %s" % (type(exc).__name__, exc)}],
                             "isError": True})
    elif method in ("notifications/initialized", "notifications/cancelled"):
        pass                                  # notifications take no reply
    elif msg_id is not None:
        respond(msg_id, error={"code": -32601, "message": "method not found: %s" % method})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        try:
            handle(msg)
        except Exception as exc:                       # never die on one bad message
            sys.stderr.write("[anneal-mcp] %r\n" % exc)
            sys.stderr.flush()


if __name__ == "__main__":
    main()
