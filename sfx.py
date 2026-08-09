#!/usr/bin/env python3
"""Sound effects — a one-shot from a description.

Runs `sa3-sm-sfx` through Stability's pure-MLX runner, as a subprocess. Three
things follow from measurement rather than preference:

**It is not a service.** Peak footprint is 1.49 GB for a five-second clip,
against roughly 7 GB for music and 9 for image, and the runner loads and exits
in a single pass. So there is nothing to keep warm and nothing to evict: asking
for a door slam does not cost a three-minute music reload. That is the whole
reason this is a subprocess and not an entry in `services.py`.

**It shells out for the same reason the sprite cutter does.** The runner needs
mlx, sentencepiece and soundfile; the environment that serves the models is
version-pinned, and a sound-effects dependency must not be able to break music
generation. Nothing crosses the boundary except argv and a path.

**Timing is ours, not the vendor's.** The model card says "a few seconds on a
MacBook Pro M4". Measured here on an M4 mini: 5.23 s wall for 5.0 s of audio,
cold, including every load. Roughly realtime, which is why MAX_SECONDS is a
time budget as much as a length.

The upstream checkout is pinned by setup.sh and is not part of this repo. The
weights live in the shared model directory and are symlinked into it, so an
update that re-clones the checkout does not throw away 1.8 GB.
"""

from __future__ import annotations

import os
import re
import subprocess
import time

import paths

# Roughly realtime generation, so this bounds how long one request can occupy
# the machine as much as how long the result is. Sound effects are one-shots;
# anything wanting a minute of audio wants the music model.
MAX_SECONDS = 30
DEFAULT_SECONDS = 5
# The runner's own names for the pair. sm-sfx is the sound-effects DiT; same-s
# is the small codec it was trained against, and mixing sizes is not a choice
# the caller should be offered.
DIT = "sm-sfx"
DECODER = "same-s"

SFX_PYTHON = os.environ.get(
    "ANNEAL_SFX_PYTHON",
    os.path.join(paths.aimusic_root(), "sfx-venv", "bin", "python"))
SFX_DIR = os.environ.get(
    "ANNEAL_SFX_DIR",
    os.path.join(paths.aimusic_root(), "stable-audio-3", "optimized", "mlx"))


def runner():
    """The upstream script, or None if this host has not installed it."""
    script = os.path.join(SFX_DIR, "scripts", "sa3_mlx.py")
    if os.path.isfile(SFX_PYTHON) and os.path.isfile(script):
        return script
    return None


def why_unavailable():
    """Why this host cannot make sound effects, or None."""
    if not os.path.isfile(SFX_PYTHON):
        return ("no interpreter for sound effects. The runner is pure MLX and is "
                "kept out of the pinned environment that serves the models — "
                "build it with ./setup.sh --sfx, or point ANNEAL_SFX_PYTHON at "
                "one that has mlx and soundfile.")
    if not runner():
        return ("the stable-audio-3 checkout is missing. ./setup.sh --sfx clones "
                "it at the pinned commit.")
    if not os.path.exists(os.path.join(SFX_DIR, "models", "mlx", "dit_%s_f16.npz" % DIT)):
        return ("the sound-effects weights are not downloaded. "
                "./anneal models sfx fetches them (about 1.8 GB).")
    return None


def problem(payload):
    """Why this request cannot be served, or None. No side effects."""
    if not (payload.get("prompt") or "").strip():
        return "'prompt' is required — describe the sound, e.g. 'a heavy door slamming'"
    seconds = payload.get("seconds", DEFAULT_SECONDS)
    # Refused rather than coerced: int("five") raising inside the handler is a
    # 500 for what is a caller mistake, and int(7.9) silently making it 7 is a
    # different request from the one that was sent.
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return "'seconds' must be a number"
    if not 1 <= seconds <= MAX_SECONDS:
        return "'seconds' must be between 1 and %d" % MAX_SECONDS
    return None


def generate(payload, out_dir, timeout=300):
    """Make one effect. Returns the path written, or raises RuntimeError.

    Deliberately no retry: the runner either has its weights or does not, and a
    second attempt at a missing file costs another cold load to fail the same way.
    """
    script = runner()
    if not script:
        raise RuntimeError(why_unavailable() or "sound effects are not available")

    prompt = payload["prompt"].strip()
    seconds = float(payload.get("seconds", DEFAULT_SECONDS))
    stem = re.sub(r"[^a-z0-9]+", "-", prompt.lower())[:48].strip("-") or "effect"
    os.makedirs(out_dir, exist_ok=True)
    dest = os.path.join(out_dir, "%s_%s.wav" % (time.strftime("%Y-%m-%dT%H-%M-%S"), stem))

    argv = [SFX_PYTHON, script, "--prompt", prompt, "--dit", DIT,
            "--decoder", DECODER, "--seconds", "%g" % seconds, "--out", dest]
    if payload.get("seed") is not None:
        argv += ["--seed", str(int(payload["seed"]))]

    env = dict(os.environ)
    # The runner fetches any missing weight lazily, and env.sh sets
    # HF_HUB_OFFLINE=1 so that a mis-pinned model fails loudly instead of being
    # downloaded mid-request. Both are right; leave them as they are, and let
    # a missing weight be the clear error above rather than a silent 1.8 GB
    # download inside somebody's request.
    env.setdefault("HF_HUB_DISABLE_XET", "1")

    started = time.time()
    done = subprocess.run(argv, capture_output=True, text=True,
                          timeout=timeout, cwd=SFX_DIR, env=env)
    if done.returncode != 0 or not os.path.isfile(dest):
        tail = (done.stderr or done.stdout or "").strip().splitlines()
        raise RuntimeError("the runner failed: %s" % (tail[-1] if tail else "no output")[:300])
    return {"path": dest, "seconds": seconds, "prompt": prompt,
            "took": round(time.time() - started, 1)}
