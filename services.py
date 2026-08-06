#!/usr/bin/env python3
"""Service registry for the on-demand generation gateway.

Each entry describes one model backend the supervisor can start and stop.
Adding a modality means adding a dict here — the supervisor itself is generic.

Fields:
    routes        Path prefixes this service owns. Longest prefix wins, so
                  "/v1/audio/speech" (speech) correctly beats "/v1/audio" (music).
    port          Loopback port the backend listens on.
    cmd           argv to launch it.
    cwd           Working directory for the process.
    heavy         True if it holds multiple GB. Starting a heavy service stops
                  any other heavy service — 16 GB only fits one at a time.
    ready_timeout Seconds to wait for /health after launch (weights load lazily
                  for music, eagerly for speech/image).
    idle_timeout  Seconds with no traffic before the service is stopped.
    busy_path     Optional JSON endpoint used to check for in-progress work
                  before an idle stop, so a queued job is never killed.
"""

from __future__ import annotations

import os

AIMUSIC_ROOT = os.environ.get("AIMUSIC_ROOT", "/Volumes/Storage/AIMusic")
ACESTEP_DIR = os.environ.get("ACESTEP_DIR", os.path.join(AIMUSIC_ROOT, "ACE-Step-1.5"))
UV_BIN = os.environ.get("UV_BIN", "/opt/homebrew/bin/uv")
GEN_PYTHON = os.path.join(AIMUSIC_ROOT, "gen-venv", "bin", "python")
HERE = os.path.dirname(os.path.abspath(__file__))


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# Music quality tiers. ACE-Step picks its DiT at startup via ACESTEP_CONFIG_PATH
# and can only route between models it already has resident — which on 16 GB is
# exactly one. So a tier change means restarting the backend on the other model,
# which the supervisor already knows how to do.
MUSIC_TIERS = {
    "draft": {
        "model": "acestep-v15-turbo",
        "steps": 8,
        "label": "Draft — fast, distilled (8 steps)",
    },
    # Non-turbo. Needs DCW off (patches/apply_patches.py derives that from the
    # model's own is_turbo flag, which upstream never did) plus real CFG, which
    # turbo ignores. 50 steps is what this model was tuned for.
    "high": {
        "model": "acestep-v15-sft",
        "steps": 50,
        "label": "High — fine-tuned, 50 steps",
        "extra_params": {"guidance_scale": 7.0, "cfg_interval_start": 0.0,
                         "cfg_interval_end": 1.0, "use_adg": False},
    },
}
DEFAULT_MUSIC_TIER = os.environ.get("ANNEAL_MUSIC_TIER", "draft")


SERVICES = {
    "music": {
        "routes": [
            "/release_task", "/query_result", "/create_random_sample", "/format_input",
            "/v1/audio", "/v1/stats", "/v1/models", "/v1/init", "/v1/reinitialize",
            "/v1/model_inventory", "/v1/create_sample", "/v1/lora",
            # /v1/music/tiers is answered by the gateway, not proxied.
            # /docs and /openapi.json are deliberately NOT routed here — the
            # gateway serves its own spec covering all three services.
        ],
        "port": _int("ACESTEP_BACKEND_PORT", 8011),
        "cmd": [UV_BIN, "run", "acestep-api"],
        "cwd": ACESTEP_DIR,
        "env": {"ACESTEP_API_HOST": "127.0.0.1",
                "ACESTEP_CONFIG_PATH": MUSIC_TIERS[DEFAULT_MUSIC_TIER]["model"]},
        "port_env": "ACESTEP_API_PORT",
        "heavy": True,
        # Weights load lazily on the first generation request, not at boot.
        "ready_timeout": _int("ACESTEP_READY_TIMEOUT", 900),
        "idle_timeout": _int("ACESTEP_IDLE_TIMEOUT", 600),
        "busy_path": "/v1/stats",
        "log": os.path.join(AIMUSIC_ROOT, "api-server.log"),
    },
    "speech": {
        "routes": ["/v1/audio/speech", "/v1/speech", "/v1/voices"],
        "port": _int("SPEECH_PORT", 8012),
        "cmd": [GEN_PYTHON, os.path.join(HERE, "speech_server.py")],
        "cwd": HERE,
        "env": {},
        "port_env": "SPEECH_PORT",
        # Kokoro is ~350 MB — small enough to coexist with a heavy service.
        "heavy": False,
        "ready_timeout": _int("SPEECH_READY_TIMEOUT", 300),
        "idle_timeout": _int("SPEECH_IDLE_TIMEOUT", 900),
        "busy_path": None,
        "log": os.path.join(AIMUSIC_ROOT, "speech-server.log"),
    },
    "image": {
        "routes": ["/v1/images"],
        "port": _int("IMAGE_PORT", 8013),
        "cmd": [GEN_PYTHON, os.path.join(HERE, "image_server.py")],
        "cwd": HERE,
        "env": {},
        "port_env": "IMAGE_PORT",
        "heavy": True,
        "ready_timeout": _int("IMAGE_READY_TIMEOUT", 900),
        "idle_timeout": _int("IMAGE_IDLE_TIMEOUT", 600),
        "busy_path": "/busy",
        "log": os.path.join(AIMUSIC_ROOT, "image-server.log"),
    },
}


def resolve(path: str):
    """Return the service name owning `path`, by longest matching prefix."""
    best_name, best_len = None, -1
    for name, spec in SERVICES.items():
        for route in spec["routes"]:
            if (path == route or path.startswith(route.rstrip("/") + "/")
                    or path.startswith(route)) and len(route) > best_len:
                best_name, best_len = name, len(route)
    return best_name
