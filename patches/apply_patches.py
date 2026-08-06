#!/usr/bin/env python3
"""Apply Anneal's local fixes to the upstream ACE-Step checkout.

The checkout is not part of this repo, so anything changed there is lost the
moment it is updated. These patches are therefore expressed as anchored string
edits applied at start-up: idempotent, so running twice is harmless, and loud
when an anchor disappears, so an upstream change that invalidates a fix is
reported rather than silently dropped.

Deliberately not a .patch file — those bind to line numbers and fail messily
against a moving upstream. Matching on distinctive code is more durable and
gives a far better error when it stops matching.

    ./apply_patches.py            apply anything missing (default)
    ./apply_patches.py --check    report status only, change nothing
"""

from __future__ import annotations

import os
import sys

ACESTEP_DIR = os.environ.get(
    "ACESTEP_DIR",
    os.path.join(os.environ.get("AIMUSIC_ROOT", "/Volumes/Storage/AIMusic"), "ACE-Step-1.5"),
)

MARKER = "# ANNEAL-PATCH"

PATCHES = [
    {
        "name": "dcw-off-for-non-turbo",
        "file": "acestep/core/generation/handler/service_generate_execute.py",
        "why": (
            "DCW is a turbo-only feature. inference.py defaults dcw_enabled=True and the "
            "REST API exposes no way to change it, so non-turbo models (sft, base) ran with "
            "turbo settings and produced garbled noise. Gradio sets this per model type; the "
            "API never could. Derive it from the model's own is_turbo flag instead."
        ),
        "anchor": '        if timesteps is not None:\n'
                  '            kwargs["timesteps"] = torch.tensor(timesteps, dtype=torch.float32, device=self.device)\n'
                  '        return kwargs',
        "replacement":
            '        ' + MARKER + ' dcw-off-for-non-turbo\n'
            '        # DCW is turbo-only; on sft/base models it produces noise. The REST API\n'
            '        # cannot override the hardcoded default, so decide from the model config.\n'
            '        if not getattr(getattr(self, "config", None), "is_turbo", False):\n'
            '            kwargs["dcw_enabled"] = False\n'
            '        if timesteps is not None:\n'
            '            kwargs["timesteps"] = torch.tensor(timesteps, dtype=torch.float32, device=self.device)\n'
            '        return kwargs',
    },
    {
        "name": "no-mlx-dit-for-non-turbo",
        "file": "acestep/core/generation/handler/init_service_setup.py",
        "why": (
            "acestep/models/mlx/dit_model.py describes itself as a re-implementation of "
            "modeling_acestep_v15_turbo.py. Non-turbo checkpoints are dimensionally identical, "
            "so they load through it without error and render smeared, noisy audio with poor "
            "prompt adherence. Fall back to the PyTorch path for anything that is not turbo."
        ),
        "anchor": '        mlx_dit_status = "Disabled"\n'
                  '        if use_mlx_dit and device in ("mps", "cpu"):',
        "replacement":
            '        mlx_dit_status = "Disabled"\n'
            '        ' + MARKER + ' no-mlx-dit-for-non-turbo\n'
            '        # The MLX DiT is a port of the turbo model specifically. Non-turbo\n'
            '        # weights load through it cleanly and produce degraded audio, so keep\n'
            '        # them on the PyTorch path.\n'
            '        if use_mlx_dit and not getattr(getattr(self, "config", None), "is_turbo", False):\n'
            '            use_mlx_dit = False\n'
            '        if use_mlx_dit and device in ("mps", "cpu"):',
    },
]


def status(patch):
    path = os.path.join(ACESTEP_DIR, patch["file"])
    if not os.path.isfile(path):
        return "missing-file", path
    with open(path) as fh:
        src = fh.read()
    if MARKER + " " + patch["name"] in src:
        return "applied", path
    if patch["anchor"] in src:
        return "applicable", path
    return "anchor-lost", path


def apply(patch):
    state, path = status(patch)
    if state == "applied":
        return True, "already applied"
    if state == "missing-file":
        return False, "file not found: %s" % path
    if state == "anchor-lost":
        return False, ("anchor no longer matches — upstream has changed. "
                       "Re-derive this patch before trusting the affected feature.")
    with open(path) as fh:
        src = fh.read()
    with open(path, "w") as fh:
        fh.write(src.replace(patch["anchor"], patch["replacement"], 1))
    return True, "applied"


def main():
    check_only = "--check" in sys.argv
    failures = 0
    for patch in PATCHES:
        if check_only:
            state, _ = status(patch)
            print("  %-26s %s" % (patch["name"], state))
            if state == "anchor-lost":
                failures += 1
            continue
        ok, msg = apply(patch)
        print("  %-26s %s" % (patch["name"], msg))
        if not ok:
            failures += 1
            print("      %s" % patch["why"])
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
