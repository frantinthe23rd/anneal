#!/usr/bin/env python3
"""Video generation backend — Wan 2.1 via mlx-video, and whatever replaces it.

Runs behind supervisor.py, which starts it on demand and stops it when idle.
Heavy: the supervisor will not let this and the music or image model be resident
at the same time.

#20 recorded video as out of reach and wrote down what would change that: a
4-bit MLX port of a competent model with a peak footprint under ~10 GB. Wan 2.1
T2V-1.3B through mlx-video meets that, so this exists. Two things about it are
worth knowing before using it:

  * **It is the slowest thing here by a wide margin.** Reported Apple-silicon
    figures are around nine to ten minutes for nine frames at 768x768 — longer
    than a whole Press. Nothing about that is a bug.
  * **1.3B is the small variant.** Short, low resolution, and not close to the
    14B model or to a hosted service. What it buys is that it runs at all.

**The model is pluggable, and that is the design rather than a nicety.** Wan
1.3B is what fits 16 GB; Wan 14B and LTX-2 fit a 32 GB machine, and swapping
should be an environment variable, not a rewrite. It also moves the licence
question from a design decision to a per-model property — the reason MiniMax H3
was passed over is that its licence is use-restricted, and a table makes that a
visible attribute of a row instead of something buried in a commit message.

The two families do not share a command line: Wan wants a pre-converted
`--model-dir`, LTX-2 resolves `--model-repo` itself. That is why BACKENDS holds
an argv builder per family rather than a flag mapping.

Generation runs as a subprocess of this server rather than in-process. Two
reasons, both measured elsewhere in this repo: MLX allocates through Metal and
only a process exit reliably returns it, and a nine-minute generation that
wedges should not take the HTTP server with it. The weight load is paid per
request, which on a ten-minute generation is noise.

Endpoints:

    POST /v1/videos/generations  {"prompt": "...", "frames": 17, "width": 832,
                                  "height": 480, "steps": 30, "seed": 42}
    GET  /health                 -> readiness, and whether the model is present
    GET  /busy                   -> whether a generation is in flight

Run it directly for a quick check:
    /Volumes/Storage/AIMusic/video-venv/bin/python video_server.py --port 8015
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AIMUSIC_ROOT = os.environ.get("AIMUSIC_ROOT", "/Volumes/Storage/AIMusic")
OUTPUT_DIR = os.environ.get("VIDEO_OUTPUT_DIR", os.path.join(AIMUSIC_ROOT, "outputs", "video"))
PYTHON = os.environ.get("ANNEAL_VIDEO_PYTHON", sys.executable)

# Where the converted MLX weights live. Conversion is a separate step — see
# README — because the published checkpoint is PyTorch and roughly 17 GB.
MODEL_DIR = os.environ.get(
    "ANNEAL_VIDEO_MODEL_DIR", os.path.join(AIMUSIC_ROOT, "models", "Wan2.1-T2V-1.3B-mlx"))

# 9 frames took ~10 minutes on comparable hardware. 33 is already the far side
# of half an hour; past that a request is indistinguishable from a hang.
MAX_FRAMES = 33
MIN_FRAMES = 5
MAX_PIXELS = 1280 * 720
DEFAULT_FRAMES = int(os.environ.get("VIDEO_FRAMES", "17"))

_generate_lock = threading.Lock()
_in_flight = 0
_flight_lock = threading.Lock()


def log(msg: str) -> None:
    print("[video] %s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def snap_frames(n):
    """Wan requires 4n+1 frames. Snap up rather than fail nine minutes in.

    The CLI enforces this itself, but it does so after loading the weights,
    which is several minutes of nothing followed by an argument error.
    """
    n = max(int(n or 0), MIN_FRAMES)
    if (n - 1) % 4:
        n += 4 - ((n - 1) % 4)
    return n


# --------------------------------------------------------------- backends
#
# One row per model family. `argv` takes the request and the output path and
# returns the command line; `licence` is stated because it is the attribute that
# decides whether a model may be the default (see the module docstring).

def _wan_argv(req, out_path, model_dir):
    argv = ["--model-dir", model_dir, "--prompt", req["prompt"],
            "--num-frames", str(snap_frames(req.get("frames", DEFAULT_FRAMES))),
            "--width", str(int(req.get("width", 832))),
            "--height", str(int(req.get("height", 480))),
            "--output-path", out_path,
            # VAE decoding is the memory peak, not denoising. Tiling is what
            # keeps that under the ceiling on 16 GB.
            "--tiling", os.environ.get("VIDEO_TILING", "auto")]
    if req.get("steps"):
        argv += ["--steps", str(int(req["steps"]))]
    if req.get("seed") is not None:
        argv += ["--seed", str(int(req["seed"]))]
    if req.get("negative_prompt"):
        argv += ["--negative-prompt", str(req["negative_prompt"])]
    return argv


def _ltx_argv(req, out_path, model_dir):
    argv = ["--prompt", req["prompt"],
            "--num-frames", str(int(req.get("frames", DEFAULT_FRAMES))),
            "--width", str(int(req.get("width", 832))),
            "--height", str(int(req.get("height", 480))),
            "--output-path", out_path,
            "--tiling", os.environ.get("VIDEO_TILING", "auto"),
            "--model-repo", os.environ.get("ANNEAL_VIDEO_REPO",
                                           "Lightricks/LTX-Video")]
    if req.get("steps"):
        argv += ["--steps", str(int(req["steps"]))]
    if req.get("seed") is not None:
        argv += ["--seed", str(int(req["seed"]))]
    return argv


BACKENDS = {
    "wan": {
        "module": "mlx_video.models.wan_2.generate",
        "argv": _wan_argv,
        # The reason this is the default. Apache-2.0 with no commercial
        # restriction, revenue threshold or territorial limit — unlike MiniMax
        # H3, and unlike LTX-2, which is widely described as Apache but ships
        # under a community licence with a revenue threshold. "Apache" in a
        # blog post is not the same as Apache in the repository.
        "licence": "Apache-2.0",
        "needs_local_model": True,
        "label": "Wan 2.1 T2V-1.3B",
    },
    "ltx": {
        "module": "mlx_video.models.ltx_2.generate",
        "argv": _ltx_argv,
        "licence": "LTX-Video community licence (revenue-threshold restricted)",
        "needs_local_model": False,
        "label": "LTX-2",
    },
}
DEFAULT_BACKEND = os.environ.get("ANNEAL_VIDEO_BACKEND", "wan")


def build_argv(backend, req, out_path, model_dir=None):
    spec = BACKENDS.get(backend)
    if not spec:
        raise ValueError("unknown video backend %r — known: %s"
                         % (backend, ", ".join(sorted(BACKENDS))))
    return [PYTHON, "-m", spec["module"]] + spec["argv"](
        req, out_path, model_dir or MODEL_DIR)


def model_problem(backend, model_dir=None):
    """Why this host cannot generate yet, or None.

    Worth its own answer rather than a stack trace: the weights are a separate
    multi-gigabyte download *and* a conversion step, and "no such file" from
    inside MLX does not tell anyone which of the two they skipped.
    """
    spec = BACKENDS.get(backend)
    if not spec:
        return "unknown backend %r" % backend
    if not spec.get("needs_local_model"):
        return None
    path = model_dir or MODEL_DIR
    if not os.path.isdir(path):
        return ("no converted model at %s. Download the checkpoint and convert "
                "it to MLX — see 'Video' in README.md; the published weights are "
                "PyTorch and must be converted before they can be used." % path)
    return None


def limits(payload):
    """Reject what cannot work before spending ten minutes finding out."""
    if not (payload.get("prompt") or "").strip():
        return "'prompt' is required"
    try:
        frames = int(payload.get("frames", DEFAULT_FRAMES))
        width = int(payload.get("width", 832))
        height = int(payload.get("height", 480))
    except (TypeError, ValueError):
        return "'frames', 'width' and 'height' must be numbers"
    if frames > MAX_FRAMES:
        return ("'frames' must be at most %d — nine frames takes about ten "
                "minutes here, so longer requests are indistinguishable from a "
                "hang" % MAX_FRAMES)
    if width * height > MAX_PIXELS:
        return "'width' x 'height' must be at most %d pixels" % MAX_PIXELS
    return None


def unique_path(prompt, seed):
    stem = re.sub(r"[^a-zA-Z0-9]+", "_", prompt.strip())[:48].strip("_") or "video"
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    base = os.path.join(OUTPUT_DIR, "%s-%s" % (stem, seed))
    path, n = base + ".mp4", 1
    while os.path.exists(path):
        path, n = "%s-%d.mp4" % (base, n), n + 1
    return path


def generate(req, backend=None, timeout=None):
    """Run one generation. Returns (path, seconds, seed)."""
    backend = backend or DEFAULT_BACKEND
    seed = req.get("seed")
    if seed is None:
        seed = random.randint(0, 2 ** 31 - 1)
    req = dict(req, seed=seed)
    out_path = unique_path(req["prompt"], seed)
    argv = build_argv(backend, req, out_path)
    log("generating with %s: %s" % (backend, " ".join(argv[3:])))
    t0 = time.time()
    done = subprocess.run(argv, capture_output=True, text=True,
                          timeout=timeout or int(os.environ.get("VIDEO_TIMEOUT", "5400")))
    took = time.time() - t0
    if done.returncode or not os.path.exists(out_path):
        tail = (done.stderr or done.stdout or "").strip().splitlines()[-6:]
        raise RuntimeError("generation failed (%s): %s"
                           % (done.returncode, " / ".join(tail) or "no output"))
    log("wrote %s in %.1fs" % (out_path, took))
    return out_path, took, seed


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

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
            problem = model_problem(DEFAULT_BACKEND)
            self._json({"status": "ok", "service": "video",
                        "backend": DEFAULT_BACKEND,
                        "model": BACKENDS[DEFAULT_BACKEND]["label"],
                        "licence": BACKENDS[DEFAULT_BACKEND]["licence"],
                        "model_ready": problem is None,
                        "model_problem": problem})
            return
        if self.path.startswith("/busy"):
            self._json({"data": {"in_flight": _in_flight}, "code": 200})
            return
        self._json({"code": 404, "error": "not found"}, 404)

    def do_POST(self):
        global _in_flight
        if not self.path.startswith("/v1/videos"):
            self._json({"code": 404, "error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            self._json({"code": 400, "error": "invalid JSON body"}, 400)
            return

        problem = limits(payload)
        if problem:
            self._json({"code": 400, "error": problem}, 400)
            return
        problem = model_problem(DEFAULT_BACKEND)
        if problem:
            self._json({"code": 503, "error": problem}, 503)
            return

        with _flight_lock:
            _in_flight += 1
        try:
            # One at a time. Two concurrent MLX video generations do not fit,
            # and the second would not fail cleanly — it would page.
            with _generate_lock:
                path, took, seed = generate(payload)
        except subprocess.TimeoutExpired:
            self._json({"code": 504, "error": "generation exceeded its time limit"}, 504)
            return
        except Exception as exc:
            log("generation failed: %r" % (exc,))
            self._json({"code": 500, "error": str(exc)[:500]}, 500)
            return
        finally:
            with _flight_lock:
                _in_flight -= 1

        self._json({"created": int(time.time()), "code": 200, "error": None,
                    "data": [{"path": path, "seconds": round(took, 1), "seed": seed,
                              "backend": DEFAULT_BACKEND,
                              "frames": snap_frames(payload.get("frames", DEFAULT_FRAMES))}]})


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=int(os.environ.get("VIDEO_PORT", "8015")))
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    problem = model_problem(DEFAULT_BACKEND)
    log("backend %s (%s, %s); model %s" % (
        DEFAULT_BACKEND, BACKENDS[DEFAULT_BACKEND]["label"],
        BACKENDS[DEFAULT_BACKEND]["licence"], problem or "ready"))
    log("listening on %s:%d" % (args.host, args.port))
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
