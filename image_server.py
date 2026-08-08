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
                                  "init_image": "<path from a previous result>",
                                  "retention": 0.7,
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

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402  — stdlib only, so it imports fine under gen-venv

MODEL_REPO = os.environ.get("IMAGE_MODEL", "dhairyashil/FLUX.1-schnell-mflux-4bit")
OUTPUT_DIR = os.environ.get("IMAGE_OUTPUT_DIR") or paths.under_root("outputs", "images")

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


def resolve_init_image(raw):
    """Validate an init image path for img2img, or raise.

    Only files this server produced are accepted. The alternative — taking any
    path the caller names — hands a network client the ability to read anything
    the process can, and the value of accepting arbitrary paths is nil when
    every image Anneal makes is already in here.
    """
    if not raw:
        return None
    path = os.path.realpath(str(raw))
    root = os.path.realpath(OUTPUT_DIR)
    if not (path.startswith(root + os.sep) and os.path.isfile(path)):
        raise ValueError("init image must be a file previously generated here")
    return path


def unique_path(stem, seed):
    """A path that does not already exist.

    The name is prompt-slug plus seed, and neither is unique: the slug is
    truncated at 48 characters and the seed is often deliberately reused. Two
    requests could therefore land on the same filename, and the second silently
    destroyed the first along with its sidecar. A variation makes that routine
    rather than rare — it shares both the prompt prefix and the seed with the
    image it derives from, so it overwrote its own source.
    """
    base = os.path.join(OUTPUT_DIR, "%s-%d" % (stem, seed))
    if not os.path.exists(base + ".png"):
        return base + ".png"
    n = 2
    while os.path.exists("%s-%d.png" % (base, n)):
        n += 1
    return "%s-%d.png" % (base, n)


def generate(prompt, width, height, steps, seed, init_path=None, retention=None):
    model = get_model()
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9]+", "_", prompt)[:48].strip("_") or "image"
    if init_path:
        stem = (stem + "_var")[:52]
    path = unique_path(stem, seed)

    # mflux spends `int(steps * strength)` of the budget reproducing the init
    # image, leaving the remainder to act on the prompt. schnell is distilled to
    # four steps, so at high retention there is only one step left — which is
    # the point for variations, and why this cannot restructure a subject. See
    # Measured, not assumed.
    extra = {}
    if init_path:
        extra = {"image_path": init_path, "image_strength": retention}

    with _generate_lock:
        t0 = time.time()
        image = model.generate_image(
            seed=seed, prompt=prompt, num_inference_steps=steps,
            width=width, height=height, **extra,
        )
        image.save(path, overwrite=False)   # unique_path already guaranteed it
        elapsed = time.time() - t0

    log("%dx%d, %d steps, seed %d%s -> %s (%.1fs)" % (
        width, height, steps, seed,
        ", from %s @ %.2f" % (os.path.basename(init_path), retention) if init_path else "",
        path, elapsed))
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

        try:
            init_path = resolve_init_image(payload.get("init_image"))
        except ValueError as exc:
            self._json({"code": 400, "error": str(exc)}, 400)
            return
        # Retention is how much of the original survives. Below ~0.5 on a
        # four-step model the result drifts into a different subject rather
        # than a variation of this one, so the floor is deliberate.
        retention = float(payload.get("retention") or 0.7)
        if init_path and not 0.3 <= retention <= 0.95:
            self._json({"code": 400,
                        "error": "retention must be between 0.3 and 0.95"}, 400)
            return
        # Retention is quantised by the step count: mflux spends
        # `int(steps * retention)` steps reproducing the init image, so at the
        # default four there are only three distinct settings and 0.7 and 0.55
        # produce byte-identical output. Eight steps gives the three offered
        # levels genuinely different remaining budgets (2, 3 and 4), at a few
        # seconds' cost. An explicit `steps` from the caller still wins.
        if init_path and payload.get("steps") is None:
            steps = 8

        with _flight_lock:
            _in_flight += 1
        try:
            results = []
            for index in range(count):
                seed = int(base_seed) + index if base_seed is not None else random.randint(0, 2**31 - 1)
                path, elapsed = generate(prompt, width, height, steps, seed,
                                         init_path, retention)
                entry = {"path": path, "seed": seed, "seconds": round(elapsed, 1),
                         "url": "/v1/images/file?path=" + path}
                if init_path:
                    entry["derived_from"] = init_path
                    entry["retention"] = retention
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
