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
# One frame is about half a minute. Eight of them is four minutes of the machine
# for one animation, which is where this stops being worth it.
MAX_POSES = 8

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
    """Change the pose and nothing else.

    Everything that must not move is named, because the model edits what you
    mention and drifts on what you do not. Physical instructions work and
    abstract ones do not: "shield raised in front" returned the original pose,
    while "shield lifted high above the head with both arms" was obeyed exactly.
    """
    return ("change only the pose: the same character, now %s. Identical "
            "character design, colours, proportions, outfit and art style. "
            "Keep the plain flat white background, and keep the character fully "
            "in frame." % pose.strip())


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
