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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import paths  # noqa: E402  — repo root, stdlib only

ACESTEP_DIR = os.environ.get("ACESTEP_DIR") or paths.under_root("ACE-Step-1.5")

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
    {
        "name": "free-torch-decoder-after-mlx",
        "file": "acestep/core/generation/handler/mlx_dit_init.py",
        "why": (
            "convert_and_load copies the DiT decoder into MLX but never releases the torch "
            "original, so the bulk of a 2B model sits in memory twice, both in fp32. That is most "
            "of the ~21 GB footprint on a 16 GB machine. Releasing the redundant copy changes no "
            "numbers — the MLX decoder does the work from here.\n\n"
            "MEASURED, AND OFF BY DEFAULT. Freeing it took the peak footprint from 22 GB to "
            "18 GB — real, but a third of what was predicted, and still above the 16 GB of "
            "physical RAM. The paging it was meant to eliminate was unchanged (pagein peak "
            "13337/s before, 14659/s after: noise). Meanwhile it costs the PyTorch fallback when "
            "MLX diffusion fails, and Gradio's LRC timestamping and lyric-alignment scoring, both "
            "of which reach for model.decoder. Paying two real costs for a saving that does not "
            "cross the threshold that matters is a bad trade, so it is opt-in: set "
            "ANNEAL_FREE_TORCH_DECODER=1. Worth revisiting alongside the fp32->fp16 half of #7, "
            "which together might get under 16 GB where this alone cannot."
        ),
        "anchor": '            mlx_decoder.materialize_static_buffers()\n'
                  '            self.mlx_decoder = mlx_decoder\n'
                  '            self.use_mlx_dit = True',
        "replacement":
            '            mlx_decoder.materialize_static_buffers()\n'
            '            self.mlx_decoder = mlx_decoder\n'
            '            ' + MARKER + ' free-torch-decoder-after-mlx\n'
            '            # The decoder now exists twice: once as torch tensors, once as MLX\n'
            '            # arrays, both fp32. Only the MLX copy is used from here, so release\n'
            '            # the other. Costs the PyTorch diffusion fallback and Gradio\'s LRC /\n'
            '            # lyric-scoring, neither of which the REST API can reach.\n'
            '            import os as _os\n'
            '            if _os.environ.get("ANNEAL_FREE_TORCH_DECODER", "") in ("1", "true"):\n'
            '                try:\n'
            '                    import gc as _gc, torch as _torch\n'
            '                    self.model.decoder = None\n'
            '                    _gc.collect()\n'
            '                    if hasattr(_torch, "mps") and _torch.mps.is_available():\n'
            '                        _torch.mps.empty_cache()\n'
            '                    logger.info("[MLX-DiT] Released the duplicate PyTorch decoder "\n'
            '                                "(ANNEAL_FREE_TORCH_DECODER=1). PyTorch diffusion "\n'
            '                                "fallback and Gradio LRC/scoring are now unavailable.")\n'
            '                except Exception as _exc:\n'
            '                    logger.warning("[MLX-DiT] Could not release torch decoder: {}", _exc)\n'
            '            self.use_mlx_dit = True',
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
