#!/usr/bin/env python3
"""Image generation backend (4-bit, via mflux/MLX).

Runs behind supervisor.py, which starts it on demand and stops it when idle.
This one is heavy, so the supervisor will not let it and the music model be
loaded at the same time.

Which model it serves is chosen at startup from `services.IMAGE_MODELS` and
held for the life of the process — mflux loads weights into one object and
there is no room for a second. Switching therefore costs a restart, which the
gateway does for you; this process only has to refuse to answer as a model it
is not, so a switch that failed shows up as an error rather than as an image
quietly made by the wrong model.

Weights come from ungated, pre-quantized mflux mirrors — black-forest-labs'
own repo is gated and would need an accepted licence plus an HF token.

Endpoints:

    POST /v1/images/generations  {"prompt": "...", "size": "1024x1024",
                                  "model": "schnell", "steps": 4, "seed": 42,
                                  "n": 1,
                                  "init_image": "<path from a previous result>",
                                  "retention": 0.7,
                                  "response_format": "b64_json"|"path"}
    GET  /v1/images/file?path=   -> raw PNG bytes
    GET  /health                 -> readiness, and which model is loaded
    GET  /busy                   -> whether a generation is in flight

Run it directly for a quick check:
    gen-venv/bin/python image_server.py --port 8013 --model unfiltered
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import re
import concurrent.futures
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402  — stdlib only, so it imports fine under gen-venv
import services  # noqa: E402  — likewise; the registry rather than a copy of it

OUTPUT_DIR = os.environ.get("IMAGE_OUTPUT_DIR") or paths.under_root("outputs", "images")

MAX_STEPS = 20
MAX_PIXELS = 1536 * 1536

# Set by select_model() below, which runs at import and again for --model.
MODEL_NAME = None
MODEL_SPEC = None
MODEL_REPO = None
# Each model is distilled for a different budget: four steps for schnell, nine
# for Z-Image Turbo. One constant would be wrong for one of them, so the
# default follows the model and a caller who sends `steps` still wins.
DEFAULT_STEPS = 4

_model = None
_model_lock = threading.Lock()
# One image at a time — concurrent MLX generations would thrash 16 GB of memory.
_generate_lock = threading.Lock()
# And always on the *same* thread. MLX binds a stream to the thread that made
# it, and ThreadingHTTPServer serves every request on a new one: with Z-Image
# the second request died at step zero with "There is no Stream(gpu, 1) in
# current thread", the weights having been built on the thread that served the
# first. FLUX happened not to trip it, which is why this went unnoticed.
# Generation is already serialised by _generate_lock, so pinning it costs
# nothing — it only makes explicit that one thread owns the model.
_worker = concurrent.futures.ThreadPoolExecutor(max_workers=1,
                                                thread_name_prefix="mflux")
_in_flight = 0
_flight_lock = threading.Lock()


def log(msg: str) -> None:
    print("[image] %s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def select_model(name=None, repo=None):
    """Choose which model this process serves. Loads nothing.

    `name` is a key of services.IMAGE_MODELS. `repo` overrides where the
    weights come from without changing the loader — how IMAGE_MODEL behaved
    when it was the only setting, and still the way to point this at a mirror
    or a finetune that is not in the table.
    """
    global MODEL_NAME, MODEL_SPEC, MODEL_REPO, DEFAULT_STEPS, _model
    name = name or services.DEFAULT_IMAGE_MODEL
    if name not in services.IMAGE_MODELS:
        raise ValueError("unknown image model %r — one of: %s"
                         % (name, ", ".join(services.IMAGE_MODELS)))
    with _model_lock:
        MODEL_NAME = name
        MODEL_SPEC = services.IMAGE_MODELS[name]
        MODEL_REPO = repo or MODEL_SPEC["repo"]
        DEFAULT_STEPS = int(os.environ.get("IMAGE_STEPS") or MODEL_SPEC["steps"])
        _model = None            # a different model means the old one is stale
    return name


def _model_from_env():
    """(name, repo override) from the environment.

    IMAGE_MODEL used to be the repo id and nothing else, and env.sh and older
    installs still set it. A repo that is in the registry selects that entry;
    one that is not is loaded with the default entry's loader.
    """
    name = os.environ.get("ANNEAL_IMAGE_MODEL")
    if name not in services.IMAGE_MODELS:
        name = services.DEFAULT_IMAGE_MODEL
    repo = os.environ.get("IMAGE_MODEL")
    if repo:
        for known, spec in services.IMAGE_MODELS.items():
            if spec["repo"] == repo:
                return known, None
        return name, repo
    return name, None


def build_model(loader, local):
    """The mflux object for a loader, imported only when it is wanted.

    These are not one class with a flag: FLUX and Z-Image are different
    architectures with different weight layouts, and mflux ships a variant for
    each. The imports are inside because they pull in MLX and Metal, which the
    test suite and every other caller of this module must not need.
    """
    if loader == "flux-schnell":
        from mflux.models.common.config.model_config import ModelConfig
        from mflux.models.flux.variants.txt2img.flux import Flux1
        return Flux1(model_path=local, model_config=ModelConfig.schnell())
    if loader == "z-image-turbo":
        from mflux.models.z_image.variants import ZImageTurbo
        return ZImageTurbo(model_path=local)
    raise ValueError("no loader named %r" % loader)


def weights_dir():
    """Where this model's weights are, without asking the Hub anything.

    The pinned snapshot first, which is the same lookup the gateway's
    availability check makes — so "installed" and "loads" cannot disagree, and
    a cold start costs no network. `snapshot_download` is the fallback for a
    repo that is not in the lockfile, reached only through the IMAGE_MODEL
    escape hatch.

    Not an optimisation. The downloader fetches an exact revision and writes no
    `refs/main`, so an offline `snapshot_download(repo)` with no revision fails
    to resolve a model that is sitting complete on disk — which is how this was
    found, on the second model, with 5.9 GB already downloaded.
    """
    local = services.image_model_path(MODEL_REPO)
    if local:
        return local
    from huggingface_hub import snapshot_download
    return snapshot_download(MODEL_REPO, max_workers=4)


def get_model():
    global _model
    with _model_lock:
        if _model is None:
            log("resolving %s ..." % MODEL_REPO)
            local = weights_dir()
            log("loading %s (%s) from %s ..."
                % (MODEL_NAME, MODEL_SPEC["loader"], local))
            t0 = time.time()
            _model = build_model(MODEL_SPEC["loader"], local)
            log("loaded in %.1fs" % (time.time() - t0))
        return _model


select_model(*_model_from_env())


def on_model_thread(fn):
    """Run `fn` on the one thread that is allowed to touch MLX.

    Exceptions surface at the call site rather than in the worker, so the 500
    the caller gets still names what went wrong.
    """
    return _worker.submit(fn).result()


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

    def draw():
        model = get_model()          # loaded on this thread, and used on it
        t0 = time.time()
        image = model.generate_image(
            seed=seed, prompt=prompt, num_inference_steps=steps,
            width=width, height=height, **extra,
        )
        image.save(path, overwrite=False)   # unique_path already guaranteed it
        return time.time() - t0

    with _generate_lock:
        elapsed = on_model_thread(draw)

    log("%s: %dx%d, %d steps, seed %d%s -> %s (%.1fs)" % (
        MODEL_NAME, width, height, steps, seed,
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
                        "model": MODEL_REPO, "model_name": MODEL_NAME,
                        "loader": MODEL_SPEC["loader"],
                        "default_steps": DEFAULT_STEPS,
                        "loaded": _model is not None})
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

        # One process holds one model. The gateway restarts this backend when a
        # request names a different one, so reaching here with a mismatch means
        # that restart did not happen — and generating anyway would answer with
        # a model nobody asked for, which is the whole failure this refuses.
        asked = (payload.get("model") or "").strip()
        if asked and asked not in (MODEL_NAME, MODEL_REPO):
            self._json({"code": 409,
                        "error": "this backend is running %r (%s); it cannot "
                                 "switch. Ask the gateway, which restarts it."
                                 % (MODEL_NAME, MODEL_REPO),
                        "model": MODEL_NAME}, 409)
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
                         "model": MODEL_NAME, "steps": steps,
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
    ap.add_argument("--model", default=None,
                    help="which of %s to serve" % ", ".join(services.IMAGE_MODELS))
    ap.add_argument("--preload", action="store_true", help="load the model before serving")
    args = ap.parse_args()

    # On the command line rather than only in the environment, so `ps` answers
    # which model a running backend holds. The supervisor reads it back that
    # way, because a backend can outlive the supervisor and then its own
    # bookkeeping is fiction.
    if args.model:
        try:
            select_model(args.model)
        except ValueError as exc:
            ap.error(str(exc))

    if args.preload:
        on_model_thread(get_model)   # the same thread every request will use

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    log("listening on %s:%d (model %s / %s)"
        % (args.host, args.port, MODEL_NAME, MODEL_REPO))
    server.serve_forever()


if __name__ == "__main__":
    main()
