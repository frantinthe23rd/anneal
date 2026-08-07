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

import paths

MODEL_REPO = os.environ.get("SPEECH_MODEL", "prince-canuma/Kokoro-82M")
# Named speakers plus a written direction. The VoiceDesign variant of the same
# family was tried first and rejected: it designs a voice from the description
# on every call, so the same character came back as a different person on the
# next line of dialogue. Identity and performance have to be separate knobs,
# which is exactly what CustomVoice separates.
QWEN_REPO = os.environ.get("SPEECH_QWEN_MODEL",
                           "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-4bit")
DEFAULT_VOICE = os.environ.get("SPEECH_VOICE", "af_heart")

CONTENT_TYPES = {
    "wav": "audio/wav", "mp3": "audio/mpeg", "flac": "audio/flac",
    "opus": "audio/opus", "aac": "audio/aac",
}

# Kokoro voice ids encode language + gender: a=American, b=British, f=female, m=male.
KOKORO_VOICES = [
    "af_heart", "af_alloy", "af_aoede", "af_bella", "af_jessica", "af_kore",
    "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
    "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael",
    "am_onyx", "am_puck", "am_santa",
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
]
QWEN_VOICES = ["serena", "vivian", "uncle_fu", "ryan", "aiden", "ono_anna",
               "sohee", "eric", "dylan"]

# One registry, because the *voice* chooses the model. The two name sets do not
# collide, so an existing caller keeps working untouched and there is no way to
# ask for a voice and a model that disagree with each other.
VOICE_REGISTRY = {}
for _v in KOKORO_VOICES:
    VOICE_REGISTRY[_v] = {"backend": "kokoro", "supports_instruct": False}
for _v in QWEN_VOICES:
    VOICE_REGISTRY[_v] = {"backend": "qwen", "supports_instruct": True}

# Kept for anything still importing it. The registry is the source of truth.
VOICES = list(VOICE_REGISTRY)


def backend_for(voice):
    """Which model makes this voice, or None if nothing does."""
    spec = VOICE_REGISTRY.get(voice)
    return spec["backend"] if spec else None


def voices_payload():
    """What /v1/voices returns. Answered from the registry, so listing voices
    never loads 2.3 GB of weights to tell you something already known."""
    return {
        "default": DEFAULT_VOICE,
        "voices": [dict(spec, name=name) for name, spec in VOICE_REGISTRY.items()],
    }


def speech_problem(payload):
    """Why this request cannot be served, or None."""
    text = payload.get("input") or payload.get("text") or ""
    if not text.strip():
        return "'input' is required"
    voice = payload.get("voice") or DEFAULT_VOICE
    spec = VOICE_REGISTRY.get(voice)
    if not spec:
        return "unknown voice %r; see GET /v1/voices" % voice
    if (payload.get("instruct") or "").strip() and not spec["supports_instruct"]:
        # Silently ignoring it is the failure worth designing against: flat
        # delivery is indistinguishable from a model that tried and failed.
        return ("voice %r cannot take 'instruct' — Kokoro has no expressive "
                "control at all. Use one of: %s"
                % (voice, ", ".join(QWEN_VOICES)))
    fmt = (payload.get("response_format") or payload.get("format") or "wav").lower()
    if fmt not in CONTENT_TYPES:
        return "unsupported format %r" % fmt
    return None

_model = None            # Kokoro
_qwen = None             # Qwen3-TTS CustomVoice
_model_lock = threading.Lock()
# Neither pipeline is safe to drive from several threads at once, and the two
# together are ~2.7 GB — one at a time keeps that predictable.
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


def get_qwen():
    """Qwen3-TTS CustomVoice, loaded only when a voice that needs it is asked
    for. 2.3 GB against Kokoro's 350 MB, so most requests should never pay it."""
    global _qwen
    with _model_lock:
        if _qwen is None:
            from mlx_audio.tts.utils import load_model
            from mlx_audio.utils import get_model_path

            log("loading %s ..." % QWEN_REPO)
            t0 = time.time()
            _qwen = load_model(get_model_path(QWEN_REPO))
            log("loaded in %.1fs" % (time.time() - t0))
        return _qwen


def _encode(path, fmt, workdir):
    if fmt in ("wav", "flac") and path.endswith("." + fmt):
        return path
    converted = os.path.join(workdir, "converted.%s" % fmt)
    subprocess.run([paths.ffmpeg_bin(), "-y", "-loglevel", "error", "-i", path, converted],
                   check=True)
    return converted


def synthesize_qwen(text, voice, instruct, fmt, temperature=0.7):
    """Named speaker, plus a written direction for the performance.

    Written out rather than routed through mlx-audio's generate_audio, which
    does not expose generate_custom_voice's `speaker`/`instruct` pair.
    """
    import wave
    import numpy as np
    import mlx.core as mx

    model = get_qwen()
    workdir = tempfile.mkdtemp(prefix="tts-q-")
    try:
        with _generate_lock:
            chunks = list(model.generate_custom_voice(
                text=text, speaker=voice, language="English",
                instruct=(instruct or None), temperature=temperature))
        if not chunks:
            raise RuntimeError("model produced no audio")
        audio = mx.concatenate([c.audio for c in chunks]) if len(chunks) > 1 else chunks[0].audio
        rate = chunks[0].sample_rate
        samples = np.array(audio, copy=False).astype(np.float32).ravel()
        raw = os.path.join(workdir, "out.wav")
        with wave.open(raw, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
        with open(_encode(raw, fmt, workdir), "rb") as fh:
            return fh.read()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def synthesize(text, voice, speed, fmt, instruct=None):
    """Return audio bytes for `text`, from whichever model owns the voice."""
    if backend_for(voice) == "qwen":
        return synthesize_qwen(text, voice, instruct, fmt)

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
                [paths.ffmpeg_bin(), "-y", "-loglevel", "error", "-i", path, converted],
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
                        "model": MODEL_REPO, "loaded": _model is not None,
                        "expressive_model": QWEN_REPO,
                        "expressive_loaded": _qwen is not None})
        elif self.path.startswith("/v1/voices"):
            self._json({"data": voices_payload(), "code": 200})
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

        problem = speech_problem(payload)
        if problem:
            self._json({"code": 400, "error": problem}, 400)
            return

        text = payload.get("input") or payload.get("text") or ""
        voice = payload.get("voice") or DEFAULT_VOICE
        instruct = (payload.get("instruct") or "").strip()
        fmt = (payload.get("response_format") or payload.get("format") or "wav").lower()

        try:
            speed = float(payload.get("speed", 1.0))
        except (TypeError, ValueError):
            speed = 1.0
        speed = min(max(speed, 0.5), 2.0)

        try:
            t0 = time.time()
            audio = synthesize(text, voice, speed, fmt, instruct)
            log("%d chars -> %d bytes %s in %.2fs (%s/%s%s)"
                % (len(text), len(audio), fmt, time.time() - t0,
                   backend_for(voice), voice, ", directed" if instruct else ""))
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
