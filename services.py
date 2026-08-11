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

import json
import os
import shutil

import paths

AIMUSIC_ROOT = paths.aimusic_root()
ACESTEP_DIR = os.environ.get("ACESTEP_DIR") or os.path.join(AIMUSIC_ROOT, "ACE-Step-1.5")
# Homebrew is /opt/homebrew on Apple silicon and /usr/local on Intel and most
# manual installs, so a single absolute default is one machine's layout stated
# as a fact. env.sh resolves this properly and exports UV_BIN; this fallback
# only matters when the module is imported on its own.
UV_BIN = (os.environ.get("UV_BIN")
          or shutil.which("uv")
          or next((c for c in ("/opt/homebrew/bin/uv", "/usr/local/bin/uv")
                   if os.path.isfile(c)), "uv"))
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

# The text model, named once. mlx_lm is given a *resolved local snapshot*
# rather than a repo id, so nothing can attempt a Hub lookup at run time.
#
# That path used to be written out in full — including the sha of one machine's
# download — which meant a second install had a different sha and the text
# service failed to start with a path nobody had ever typed. The revision now
# comes from models.lock.json (the same place the downloader reads it) and the
# location from HF_HOME, so it is derived on whatever machine is running.
# One model served two jobs that want different things: planning lyrics, where
# a smaller model would be quicker and quality matters less, and driving an
# agent, where a coder-specialised model is better than a general instruct one.
# Text is heavy and only one heavy model fits, so choosing means restarting —
# the same trade `MUSIC_TIERS` already makes, and surfaced the same way.
#
# All three are Apache-2.0 and ungated. Sizes are the download, and only the
# default is required: nobody should fetch a coder to write lyrics.
TEXT_MODELS = {
    "gemma": {
        "repo": "mlx-community/gemma-4-e4b-it-4bit",
        "label": "Gemma 4 E4B — general, reasoning",
        "licence": "Gemma Terms of Use",
        "note": "The default, and what Press uses to plan. A reasoning model: "
                "send chat_template_kwargs {\"enable_thinking\": false} for "
                "tool-calling turns, or a small max_tokens is spent thinking "
                "and comes back empty.",
    },
    "qwen-coder": {
        "repo": "mlx-community/Qwen2.5-Coder-7B-Instruct-4bit",
        "label": "Qwen2.5 Coder 7B — code and tool use",
        "licence": "Apache-2.0",
        "note": "Trained for code. Same size as the default, no reasoning "
                "preamble to disable.",
    },
    "gpt-oss": {
        "repo": "mlx-community/gpt-oss-20b-MXFP4-Q4",
        "label": "GPT-OSS 20B — agent work, biggest that fits",
        "licence": "Apache-2.0",
        "note": "20B mixture-of-experts, so it is faster than its size suggests: "
                "measured 13 tok/s against 10 for the default, and a 28 s cold "
                "load. It peaks at 11.2 GB against about 11.8 GB usable on a "
                "16 GB machine — it fits, with little to spare, and nothing else "
                "heavy can be resident. Native tool calling.",
    },
    "qwen-fast": {
        "repo": "mlx-community/Qwen3-4B-Instruct-2507-4bit",
        "label": "Qwen3 4B — small and quick",
        "licence": "Apache-2.0",
        "note": "Half the size of the others. For lyric planning and short "
                "replies where the wait costs more than the polish.",
    },
}
DEFAULT_TEXT_MODEL = os.environ.get("ANNEAL_TEXT_MODEL", "gemma")
if DEFAULT_TEXT_MODEL not in TEXT_MODELS:
    DEFAULT_TEXT_MODEL = "gemma"
# Kept because env.sh and older installs set it, and because a model outside the
# registry is still a legitimate thing to point this at by hand.
TEXT_MODEL_REPO = os.environ.get("ANNEAL_TEXT_MODEL_REPO",
                                 TEXT_MODELS[DEFAULT_TEXT_MODEL]["repo"])


def _locked_revision(repo_id, lock_path=None):
    """The pinned revision for `repo_id`, or None if the lockfile cannot say."""
    try:
        with open(lock_path or os.path.join(HERE, "models.lock.json")) as handle:
            return json.load(handle)["models"][repo_id]["revision"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def text_model_path(repo=None):
    """Where mlx_lm should load the text model from.

    When nothing is downloaded this returns the path the pinned revision *will*
    occupy rather than the bare repo id. Two reasons, the second found by
    running the suite on a fresh clone:

    - the error names the directory `./anneal models text` is about to create,
      which is more use than "not found in cache";
    - `supervisor.TEXT_MODEL_NAME` derives the model's display name from this
      path's grandparent. Handed a bare repo id it produced an empty string, so
      `/health` reported `"model": ""` on any machine that had not downloaded
      the text model. Returning the canonical location keeps that derivation
      right without the caller having to know about it.

    Still nothing is fetched: env.sh sets HF_HUB_OFFLINE=1, and a path that does
    not exist cannot become a download.
    """
    override = os.environ.get("ANNEAL_TEXT_MODEL")
    if override:
        return override
    repo = repo or TEXT_MODEL_REPO
    revision = _locked_revision(repo)
    snapshot = paths.hf_snapshot(repo, revision)
    if snapshot:
        return snapshot
    return os.path.join(paths.hf_home(), "hub",
                        "models--" + repo.replace("/", "--"),
                        "snapshots", revision or "main")

# Roughly how long a cold start takes, measured on the reference machine. Not a
# timeout and not a promise — it is what the interface needs in order to warn
# before a slow request, and it belongs next to the services it describes
# rather than in prose on three pages.
COLD_START_SECONDS = {
    "music": _int("ACESTEP_COLD_START", 210),
    "speech": _int("SPEECH_COLD_START", 8),
    "text": _int("TEXT_COLD_START", 20),
    "image": _int("IMAGE_COLD_START", 45),
}


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
    # Text and code generation. mlx-lm ships an OpenAI-compatible server, so
    # this needs no wrapper of its own — just a service entry.
    #
    # NOTE the routes: mlx_lm.server also serves /v1/models, which already
    # belongs to music. Only the completion endpoints are routed here, so the
    # collision never arises.
    "text": {
        "routes": ["/v1/chat/completions", "/v1/completions", "/v1/text"],
        "port": _int("TEXT_PORT", 8014),
        "cmd": [GEN_PYTHON, "-m", "mlx_lm", "server",
                # See text_model_path(): a resolved local snapshot, derived on
                # this machine rather than one person's download sha.
                "--model", text_model_path(),
                "--host", "127.0.0.1", "--port", str(_int("TEXT_PORT", 8014))],
        "cwd": HERE,
        "env": {},
        "port_env": None,          # the port is already on the command line
        # ~5 GB. Not in the same class as music or image, so it does not evict
        # them — but it is not free either, hence the short idle timeout.
        "heavy": False,
        "ready_timeout": _int("TEXT_READY_TIMEOUT", 600),
        "idle_timeout": _int("TEXT_IDLE_TIMEOUT", 300),
        "busy_path": None,
        "log": os.path.join(AIMUSIC_ROOT, "text-server.log"),
        "health_path": "/v1/models",   # mlx_lm has no /health
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


# Routes the gateway answers itself. They must never resolve to a backend, or
# the proxy would try to forward them and 404.
GATEWAY_ROUTES = ("/v1/press", "/v1/press/download", "/v1/press/resume",
                  "/v1/press/review", "/v1/press/names", "/v1/artists", "/v1/outputs",
                  "/v1/music/tiers", "/v1/sprites", "/v1/sfx", "/v1/agent", "/v1/vector",
                  "/supervisor", "/health")


def resolve(path: str):
    for own in GATEWAY_ROUTES:
        if path == own or path.startswith(own + "/"):
            return None
    """Return the service name owning `path`, by longest matching prefix."""
    best_name, best_len = None, -1
    for name, spec in SERVICES.items():
        for route in spec["routes"]:
            if (path == route or path.startswith(route.rstrip("/") + "/")
                    or path.startswith(route)) and len(route) > best_len:
                best_name, best_len = name, len(route)
    return best_name
