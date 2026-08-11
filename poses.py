#!/usr/bin/env python3
"""One base sprite, then one directed edit per pose.

The sheet method asks a single generation for a *layout* — several poses of one
character, spaced apart — and then recovers frames by finding blobs in it. Two
things go wrong and both were measured rather than guessed: the character drifts
between poses, and sprites drawn touching each other get cut as one frame. Three
runs of the same four-pose brief returned four frames, three, and seven.

Editing removes both problems by construction. Identity comes from the reference
image instead of from asking the model nicely, and each frame is its own image,
so there is no layout to recover and nothing to merge.

Measured here before this file existed, FLUX.2-klein-4B through
`mflux-generate-flux2-edit` at 512x512:

    4 steps   33 s per frame, peak 4.90 GB
    8 steps   55 s per frame, peak 4.90 GB

Four steps is the default because the extra four bought nothing visible and cost
two thirds again in time. 4.90 GB is lighter than music or the image model, but
it is still gigabytes held in a subprocess the supervisor cannot see, so the
caller has to free the heavy slot first.
"""

from __future__ import annotations

import os
import subprocess
import time

import paths

# Apache-2.0 and ungated, unlike the FLUX.1 Kontext model this method was
# originally specified against — which was never wired up and would have needed
# a non-commercial licence and a 9 GB download.
EDIT_MODEL = os.environ.get("ANNEAL_EDIT_MODEL", "Runpod/FLUX.2-klein-4B-mflux-4bit")
EDIT_BASE = os.environ.get("ANNEAL_EDIT_BASE", "flux2-klein-4b")
DEFAULT_STEPS = 4
FRAME_SIZE = 512
# Sixteen: four directions of four frames, which is the common case a sprite
# sheet exists for. The sheet method caps at eight because more figures in one
# image makes each too small to use — a real quality limit that does not apply
# here, since every frame is its own 512x512 generation and the tenth is exactly
# as good as the first. The only cost is linear time.
MAX_POSES = 16
# Measured across two sets on this machine: 33.3 s mean per frame at four steps,
# 512x512. Served so the page can multiply rather than let someone find out by
# waiting — sixteen frames is about ten minutes.
SECONDS_PER_FRAME = 35

GEN_PYTHON = os.path.join(paths.aimusic_root(), "gen-venv", "bin",
                          "mflux-generate-flux2-edit")


def base_prompt(subject, style="flat pixel art"):
    """The one sprite every frame is edited from.

    Deliberately not a sheet prompt: one figure, centred, plainly lit. Anything
    that produces two figures here reintroduces the layout problem this method
    exists to avoid, one stage earlier.
    """
    return ("A single %s game character sprite: %s. One figure only, centred, "
            "full body, facing the viewer, standing upright. Plain flat white "
            "background, no scenery, no shadow, no text, no border."
            % (style, subject.strip()))


def edit_prompt(pose):
    """Move the character into one pose, keeping who they are.

    What must not change is named, because the model edits what you mention and
    drifts on what you do not. Physical instructions work and abstract ones do
    not: "shield raised in front" returned the original pose, while "shield
    lifted high above the head with both arms" was obeyed exactly.

    Cloth is deliberately *not* pinned. This said "identical ... outfit", which
    argues with a request for a flowing cape. Measured on a caped knight, same
    base and seed: pinning the outfit still let the cape move, but freeing it
    explicitly turned a sweep to one side into a billow on both. Secondary
    motion — cloth, hair, a tail — is most of what makes a sprite look animated
    rather than posed.
    """
    return ("the same character, now %s. Identical character design, colours, "
            "proportions and art style. Cloth, hair, cape and anything loose "
            "may move with the motion. Keep the plain flat white background, "
            "and keep the character fully in frame." % pose.strip())


BREAKDOWN_PROMPT = """Break one movement into {count} sprite frames.

Movement: {action}

Return one instruction per frame, each describing a single moment of the
movement as a physical body position. Say what the limbs are doing, and say what
any cloth, hair, cape or tail is doing in that moment — secondary motion is what
makes it read as movement rather than a series of poses.

The frames must loop: frame {count} should lead back into frame 1 without a
jump.

Write instructions, not sentences about the character. "Mid stride, left leg
forward, cape swept back" — not "the knight walks confidently".

Reply with JSON and nothing else:
{{"poses": ["...", "..."]}}"""


def breakdown_prompt(action, count):
    """Ask the text model to turn one movement into one instruction per frame."""
    return BREAKDOWN_PROMPT.format(action=(action or "").strip(), count=int(count))


def parse_breakdown(raw, count):
    """The poses in a reply, or [] if there are none worth using.

    Junk yields nothing rather than raising: this runs before any frame exists,
    and a formatting lapse from a 4B model is not something a caller should have
    to handle. The caller falls back to asking for poses by hand.
    """
    import json as _json
    import re as _re
    text = (raw or "").strip()
    if not text:
        return []
    fenced = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, _re.S)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start = text.find("{")
        if start < 0:
            return []
        depth = 0
        for i, ch in enumerate(text[start:], start):
            depth += (ch == "{") - (ch == "}")
            if depth == 0:
                candidate = text[start:i + 1]
                break
    if not candidate:
        return []
    try:
        data = _json.loads(candidate)
    except ValueError:
        return []
    out = []
    for entry in (data.get("poses") or []):
        if isinstance(entry, str) and entry.strip():
            out.append(entry.strip())
    return out[:int(count)]


def why_unavailable():
    """Why this host cannot edit poses, or None."""
    if not os.path.isfile(GEN_PYTHON):
        return ("the mflux edit runner is missing from the model environment — "
                "./setup.sh rebuilds it")
    return None


def generate(base_path, pose_list, out_dir, steps=DEFAULT_STEPS, timeout=600,
             on_frame=None):
    """Edit `base_path` once per pose. Returns a list of written paths.

    A pose that fails is skipped rather than failing the set: the frames already
    made cost half a minute each, and a four-frame cycle missing one is still
    worth returning with the gap named.
    """
    missing = why_unavailable()
    if missing:
        raise RuntimeError(missing)
    os.makedirs(out_dir, exist_ok=True)

    made, failures = [], []
    for index, pose in enumerate(pose_list):
        dest = os.path.join(out_dir, "frame-%02d.png" % index)
        argv = [GEN_PYTHON, "-m", EDIT_MODEL, "--base-model", EDIT_BASE,
                "--image-paths", base_path, "--prompt", edit_prompt(pose),
                "--steps", str(steps), "--height", str(FRAME_SIZE),
                "--width", str(FRAME_SIZE), "--output", dest]
        started = time.time()
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            failures.append((pose, "timed out"))
            continue
        if done.returncode != 0 or not os.path.isfile(dest):
            tail = (done.stderr or done.stdout or "").strip().splitlines()
            failures.append((pose, (tail[-1] if tail else "no output")[:200]))
            continue
        made.append({"index": len(made), "file": dest, "pose": pose,
                     "seconds": round(time.time() - started, 1)})
        if on_frame:
            on_frame(len(made), len(pose_list))
    return made, failures
