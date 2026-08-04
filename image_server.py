#!/usr/bin/env python3
"""Image generation backend (FLUX.1-schnell, 4-bit, via mflux/MLX).

Runs behind supervisor.py, which starts it on demand and stops it when idle.
This one is heavy (~7 GB resident), so the supervisor will not let it and the
music model be loaded at the same time.

Weights come from an ungated, pre-quantized mflux mirror — black-forest-labs'
own repo is gated and would need an accepted licence plus an HF token.

Endpoints:

    POST /v1/images/generations  {"prompt": "...", "size": "1024x1024",
                                  "steps": 4, "seed": 42, "n": 1,
                                  "response_format": "b64_json"|"path"}
    GET  /v1/images/file?path=   -> raw PNG bytes
    GET  /health                 -> readiness
    GET  /busy                   -> whether a generation is in flight

Run it directly for a quick check:
    gen-venv/bin/python image_server.py --port 8013
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_REPO = os.environ.get("IMAGE_MODEL", "dhairyashil/FLUX.1-schnell-mflux-4bit")
OUTPUT_DIR = os.environ.get("IMAGE_OUTPUT_DIR", "/Volumes/Storage/AIMusic/outputs/images")

# schnell is a distilled 4-step model; more steps mostly just costs time.
DEFAULT_STEPS = int(os.environ.get("IMAGE_STEPS", "4"))
MAX_STEPS = 20
MAX_PIXELS = 1536 * 1536

_model = None
_model_lock = threading.Lock()
# One image at a time — concurrent MLX generations would thrash 16 GB of memory.
_generate_lock = threading.Lock()
_in_flight = 0
_flight_lock = threading.Lock()


def log(msg: str) -> None:
    print("[image] %s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def get_model():
    global _model
    with _model_lock:
        if _model is None:
            from huggingface_hub import snapshot_download
            from mflux.models.common.config.model_config import ModelConfig
            from mflux.models.flux.variants.txt2img.flux import Flux1

            log("resolving %s ..." % MODEL_REPO)
            local = snapshot_download(MODEL_REPO, max_workers=4)
            log("loading FLUX from %s ..." % local)
            t0 = time.time()
            _model = Flux1(model_path=local, model_config=ModelConfig.schnell())
            log("loaded in %.1fs" % (time.time() - t0))
        return _model


def parse_size(raw):
    """'1024x1024' -> (1024, 1024), snapped to the multiple of 16 FLUX needs."""
    match = re.match(r"^\s*(\d{2,5})\s*[xX*]\s*(\d{2,5})\s*$", raw or "")
    if not match:
        raise ValueError("size must look like '1024x1024'")
    width, height = int(match.group(1)), int(match.group(2))
    width = max(256, (width // 16) * 16)
    height = max(256, (height // 16) * 16)
    if width * height > MAX_PIXELS:
        raise ValueError("size too large for this machine (max ~1536x1536)")
    return width, height


def generate(prompt, width, height, steps, seed):
    model = get_model()
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9]+", "_", prompt)[:48].strip("_") or "image"
    path = os.path.join(OUTPUT_DIR, "%s-%d.png" % (stem, seed))

    with _generate_lock:
        t0 = time.time()
        image = model.generate_image(
            seed=seed, prompt=prompt, num_inference_steps=steps,
            width=width, height=height,
        )
        image.save(path, overwrite=True)
        elapsed = time.time() - t0

    log("%dx%d, %d steps, seed %d -> %s (%.1fs)" % (width, height, steps, seed, path, elapsed))
    return path, elapsed


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FluxImage/1.0"

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            self._json({"status": "ok", "service": "image",
                        "model": MODEL_REPO, "loaded": _model is not None})
            return
        if self.path.startswith("/busy"):
            self._json({"data": {"in_flight": _in_flight}, "code": 200})
            return
        if self.path.startswith("/v1/images/file"):
            import urllib.parse
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            raw = (params.get("path") or [""])[0]
            path = os.path.realpath(raw)
            root = os.path.realpath(OUTPUT_DIR)
            if not path.startswith(root + os.sep) or not os.path.isfile(path):
                self._json({"code": 404, "error": "not found"}, 404)
                return
            with open(path, "rb") as fh:
                blob = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)
            return
        self._json({"code": 404, "error": "not found"}, 404)

    def do_POST(self):
        global _in_flight
        if not self.path.startswith("/v1/images"):
            self._json({"code": 404, "error": "not found"}, 404)
            return

        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._json({"code": 400, "error": "invalid JSON body"}, 400)
            return

        prompt = (payload.get("prompt") or "").strip()
        if not prompt:
            self._json({"code": 400, "error": "'prompt' is required"}, 400)
            return

        try:
            width, height = parse_size(payload.get("size") or "1024x1024")
        except ValueError as exc:
            self._json({"code": 400, "error": str(exc)}, 400)
            return

        steps = min(max(int(payload.get("steps") or DEFAULT_STEPS), 1), MAX_STEPS)
        count = min(max(int(payload.get("n") or 1), 1), 4)
        base_seed = payload.get("seed")
        fmt = (payload.get("response_format") or "b64_json").lower()

        with _flight_lock:
            _in_flight += 1
        try:
            results = []
            for index in range(count):
                seed = int(base_seed) + index if base_seed is not None else random.randint(0, 2**31 - 1)
                path, elapsed = generate(prompt, width, height, steps, seed)
                entry = {"path": path, "seed": seed, "seconds": round(elapsed, 1),
                         "url": "/v1/images/file?path=" + path}
                if fmt == "b64_json":
                    with open(path, "rb") as fh:
                        entry["b64_json"] = base64.b64encode(fh.read()).decode()
                results.append(entry)
        except Exception as exc:
            log("generation failed: %r" % exc)
            self._json({"code": 500, "error": str(exc)}, 500)
            return
        finally:
            with _flight_lock:
                _in_flight -= 1

        self._json({"created": int(time.time()), "data": results, "code": 200, "error": None})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("IMAGE_PORT", "8013")))
    ap.add_argument("--preload", action="store_true", help="load the model before serving")
    args = ap.parse_args()

    if args.preload:
        get_model()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    log("listening on %s:%d (model %s)" % (args.host, args.port, MODEL_REPO))
    server.serve_forever()


if __name__ == "__main__":
    main()
