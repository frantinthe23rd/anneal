#!/usr/bin/env python3
"""Minimal client for the local ACE-Step 1.5 music API.

Stdlib only, so it can be copied to any machine on the tailnet.

    ./generate.py "dreamy lo-fi hip hop with rhodes piano, 80 bpm"
    ./generate.py "driving synthwave" --duration 60 --instrumental
    ./generate.py "indie folk ballad" --lyrics-file verse.txt --out ~/Music

Point it elsewhere with --base-url, e.g. from another tailnet machine:
    ./generate.py "..." --base-url https://jons-mac-mini.pangolin-darter.ts.net
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = os.environ.get("ACESTEP_BASE_URL", "http://127.0.0.1:8001")
DEFAULT_API_KEY = os.environ.get("ACESTEP_API_KEY", "")

# On the Mac mini the internal disk is nearly full, so default to the SSD.
# Elsewhere (this script is copyable to any tailnet machine) fall back to ~/Music.
_SSD_OUT = "/Volumes/Storage/AIMusic/outputs"
DEFAULT_OUT = _SSD_OUT if os.path.isdir(_SSD_OUT) else os.path.expanduser("~/Music/AIMusic")


def _post(base_url: str, path: str, payload: dict, api_key: str) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=data,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    # The first request after a restart blocks while ~9.4 GB of weights load.
    with urllib.request.urlopen(req, timeout=1200) as resp:
        return json.load(resp)


def submit(base_url: str, api_key: str, args) -> str:
    payload = {
        "prompt": args.prompt,
        "lyrics": args.lyrics,
        "audio_duration": args.duration,
        "audio_format": args.format,
        "batch_size": args.batch_size,
        "inference_steps": args.steps,
        "thinking": not args.no_thinking,
        "vocal_language": args.language,
    }
    if args.bpm:
        payload["bpm"] = args.bpm
    if args.key_scale:
        payload["key_scale"] = args.key_scale
    if args.seed is not None:
        payload["use_random_seed"] = False
        payload["seed"] = args.seed

    try:
        body = _post(base_url, "/release_task", payload, api_key)
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            sys.exit(f"busy: {json.load(exc).get('error')}")
        raise
    if body.get("code") != 200:
        sys.exit(f"submit failed: {body.get('error')}")
    data = body.get("data") or {}
    task_id = data.get("task_id") or data.get("taskId")
    if not task_id:
        sys.exit(f"no task_id in response: {json.dumps(body)[:500]}")
    return task_id


def poll(base_url: str, api_key: str, task_id: str, timeout: int) -> list[dict]:
    deadline = time.time() + timeout
    transient = 0
    while time.time() < deadline:
        try:
            body = _post(base_url, "/query_result", {"task_id_list": [task_id]}, api_key)
        except Exception as exc:
            # A failed poll is not a failed job — the backend may just be
            # restarting. Keep polling; never resubmit, or you queue duplicate
            # work behind the original.
            transient += 1
            if transient > 20:
                sys.exit(f"\ngave up after {transient} consecutive poll failures: {exc}")
            print("?", end="", flush=True)
            time.sleep(5)
            continue
        transient = 0

        for entry in body.get("data") or []:
            if entry.get("task_id") != task_id:
                continue
            status = entry.get("status")
            if status == 1:
                # `result` is a JSON-encoded string
                result = entry.get("result")
                return json.loads(result) if isinstance(result, str) else (result or [])
            if status == 2:
                if entry.get("orphaned"):
                    sys.exit(f"\njob {task_id} was orphaned by a backend restart "
                             f"and is not running. Resubmit it.")
                sys.exit(f"\ngeneration failed: {entry.get('result')}")
        print(".", end="", flush=True)
        time.sleep(3)
    sys.exit(f"\ntimed out after {timeout}s (task {task_id} may still be running)")


def download(base_url: str, api_key: str, file_url: str, out_dir: str, stem: str) -> str:
    url = file_url if file_url.startswith("http") else f"{base_url.rstrip('/')}{file_url}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        blob = resp.read()

    ext = os.path.splitext(urllib.parse.unquote(urllib.parse.urlparse(url).query))[-1]
    ext = ext if ext and len(ext) <= 6 else ".mp3"
    os.makedirs(out_dir, exist_ok=True)
    dest = os.path.join(out_dir, stem + ext)
    with open(dest, "wb") as fh:
        fh.write(blob)
    return dest


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("prompt", help="music description, e.g. 'melancholic piano ballad'")
    p.add_argument("--lyrics", default="", help="lyrics text")
    p.add_argument("--lyrics-file", help="read lyrics from a file")
    p.add_argument("--instrumental", action="store_true", help="no vocals")
    p.add_argument("--duration", type=float, default=60.0, help="seconds, 10-600 (default 60)")
    p.add_argument("--bpm", type=int, help="tempo, 30-300")
    p.add_argument("--key-scale", default="", help="e.g. 'C Major', 'Am'")
    p.add_argument("--language", default="en", help="vocal language (default en)")
    p.add_argument("--steps", type=int, default=8, help="inference steps, turbo wants 8")
    p.add_argument("--batch-size", type=int, default=1, help="how many takes to generate")
    p.add_argument("--format", default="flac", choices=["mp3", "flac", "wav", "wav32", "opus", "aac"],
                   help="flac/wav are lossless. mp3 is capped at 128 kbps by the backend and is audibly lossy.")
    p.add_argument("--seed", type=int, help="fixed seed for reproducible output")
    p.add_argument("--no-thinking", action="store_true", help="skip the 5Hz LM planning pass (faster, lower quality)")
    p.add_argument("--out", default=DEFAULT_OUT, help=f"output directory (default {DEFAULT_OUT})")
    p.add_argument("--timeout", type=int, default=1800, help="seconds to wait for generation")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--api-key", default=DEFAULT_API_KEY)
    args = p.parse_args()

    if args.lyrics_file:
        with open(args.lyrics_file) as fh:
            args.lyrics = fh.read()
    if args.instrumental:
        args.lyrics = "[instrumental]"

    task_id = submit(args.base_url, args.api_key, args)
    print(f"task {task_id}\ngenerating", end="", flush=True)
    results = poll(args.base_url, args.api_key, task_id, args.timeout)
    print(" done")

    stem_base = "".join(c if c.isalnum() or c in "-_ " else "" for c in args.prompt)[:48].strip().replace(" ", "_")
    for i, item in enumerate(results):
        file_url = item.get("file")
        if not file_url:
            continue
        stem = stem_base if len(results) == 1 else f"{stem_base}_{i + 1}"
        print(download(args.base_url, args.api_key, file_url, args.out, stem))


if __name__ == "__main__":
    main()
