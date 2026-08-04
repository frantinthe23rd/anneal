#!/usr/bin/env python3
"""Text-to-speech backend (Kokoro-82M via MLX).

Runs behind supervisor.py, which starts it on demand and stops it when idle.
Kokoro is small (~350 MB) and fast — a sentence takes a couple of seconds — so
unlike the music and image models it can sit alongside a heavy service.

Endpoints (OpenAI-compatible where it costs nothing to be):

    POST /v1/audio/speech   {"input": "...", "voice": "af_heart", "speed": 1.0,
                             "response_format": "wav"|"mp3"|"flac"}
                            -> raw audio bytes
    GET  /v1/voices         -> available voice ids
    GET  /health            -> readiness

Run it directly for a quick check:
    gen-venv/bin/python speech_server.py --port 8012
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_REPO = os.environ.get("SPEECH_MODEL", "prince-canuma/Kokoro-82M")
DEFAULT_VOICE = os.environ.get("SPEECH_VOICE", "af_heart")

# Kokoro voice ids encode language + gender: a=American, b=British, f=female, m=male.
VOICES = [
    "af_heart", "af_alloy", "af_aoede", "af_bella", "af_jessica", "af_kore",
    "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
    "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael",
    "am_onyx", "am_puck", "am_santa",
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
]

CONTENT_TYPES = {
    "wav": "audio/wav", "mp3": "audio/mpeg", "flac": "audio/flac",
    "opus": "audio/opus", "aac": "audio/aac",
}

_model = None
_model_lock = threading.Lock()
# Kokoro's pipeline isn't safe to drive from several threads at once.
_generate_lock = threading.Lock()


def log(msg: str) -> None:
    print("[speech] %s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def get_model():
    global _model
    with _model_lock:
        if _model is None:
            from mlx_audio.tts.utils import load_model
            from mlx_audio.utils import get_model_path

            log("loading %s ..." % MODEL_REPO)
            t0 = time.time()
            _model = load_model(get_model_path(MODEL_REPO))
            log("loaded in %.1fs" % (time.time() - t0))
        return _model


def synthesize(text, voice, speed, fmt):
    """Return audio bytes for `text`."""
    from mlx_audio.tts.generate import generate_audio

    model = get_model()
    # mlx-audio only writes to disk, so stage in a temp dir and read it back.
    workdir = tempfile.mkdtemp(prefix="tts-")
    try:
        # Kokoro takes a one-letter lang code; derive it from the voice prefix.
        lang_code = voice[0] if voice and voice[0] in "abefhijpz" else "a"
        native = fmt if fmt in ("wav", "flac") else "wav"
        with _generate_lock:
            generate_audio(
                text=text, model=model, voice=voice, speed=speed,
                lang_code=lang_code, output_path=workdir, file_prefix="out",
                audio_format=native, save=True, play=False, verbose=False,
                join_audio=True,
            )
        produced = sorted(glob.glob(os.path.join(workdir, "out*.%s" % native)))
        if not produced:
            raise RuntimeError("model produced no audio")
        path = produced[0]

        if fmt != native:
            converted = os.path.join(workdir, "converted.%s" % fmt)
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", path, converted],
                check=True,
            )
            path = converted

        with open(path, "rb") as fh:
            return fh.read()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "KokoroSpeech/1.0"

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
            self._json({"status": "ok", "service": "speech",
                        "model": MODEL_REPO, "loaded": _model is not None})
        elif self.path.startswith("/v1/voices"):
            self._json({"data": {"voices": VOICES, "default": DEFAULT_VOICE}, "code": 200})
        else:
            self._json({"code": 404, "error": "not found"}, 404)

    def do_POST(self):
        if not (self.path.startswith("/v1/audio/speech") or self.path.startswith("/v1/speech")):
            self._json({"code": 404, "error": "not found"}, 404)
            return

        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._json({"code": 400, "error": "invalid JSON body"}, 400)
            return

        text = payload.get("input") or payload.get("text") or ""
        if not text.strip():
            self._json({"code": 400, "error": "'input' is required"}, 400)
            return

        voice = payload.get("voice") or DEFAULT_VOICE
        if voice not in VOICES:
            self._json({"code": 400, "error": "unknown voice %r; see GET /v1/voices" % voice}, 400)
            return

        fmt = (payload.get("response_format") or payload.get("format") or "wav").lower()
        if fmt not in CONTENT_TYPES:
            self._json({"code": 400, "error": "unsupported format %r" % fmt}, 400)
            return

        try:
            speed = float(payload.get("speed", 1.0))
        except (TypeError, ValueError):
            speed = 1.0
        speed = min(max(speed, 0.5), 2.0)

        try:
            t0 = time.time()
            audio = synthesize(text, voice, speed, fmt)
            log("%d chars -> %d bytes %s in %.2fs (%s)"
                % (len(text), len(audio), fmt, time.time() - t0, voice))
        except Exception as exc:
            log("synthesis failed: %r" % exc)
            self._json({"code": 500, "error": str(exc)}, 500)
            return

        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES[fmt])
        self.send_header("Content-Length", str(len(audio)))
        self.end_headers()
        self.wfile.write(audio)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("SPEECH_PORT", "8012")))
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
