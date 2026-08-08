#!/usr/bin/env python3
"""Verify the downloaded weights are complete — all of them, not just ACE-Step's.

HuggingFace downloads to an external APFS volume here have silently produced
*sparse* files: correct logical size, but large runs of never-written zero
blocks. safetensors only validates the header, so a bad file loads far enough to
fail deep inside model init with a confusing error. `HF_HUB_DISABLE_XET=1` is
what prevents it; this is what catches it when prevention fails.

Two things this used to miss, both found by running it on a fresh install:

- **It only looked at `$ACESTEP_CHECKPOINTS_DIR`.** Everything downloaded into
  the Hub cache — FLUX at 9 GB, Gemma at 4.8, Kokoro, Qwen3-TTS — was never
  checked at all, despite being the larger half of the install and downloaded
  by the same code path over the same transfer backend.
- **It exited 2 when a directory was empty**, so a speech-only install reported
  a hard failure for weights it had never been asked to fetch.

    ./verify-models.py              every directory the models land in
    ./verify-models.py /some/dir    just that one

The safetensors header check needs safetensors importable, which the system
Python does not have — run it under gen-venv for the full check. It says which
check it performed rather than quietly doing half of one.
"""

from __future__ import annotations

import os
import sys
import pathlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402

# A file is suspect if allocated bytes fall below this fraction of logical size.
# Real compression/cloning is not in play here, so anything under ~0.98 is a hole.
SPARSE_THRESHOLD = 0.98


def default_roots():
    """Every directory a pinned model can land in.

    Derived from the same environment the downloader uses, so adding a model
    with a new target does not need this file changed as well.
    """
    checkpoints = os.environ.get("ACESTEP_CHECKPOINTS_DIR") or paths.under_root("models")
    hub = os.path.join(paths.hf_home(), "hub")
    return [checkpoints, hub]


def weight_files(root):
    return sorted(root.rglob("*.safetensors")) + sorted(root.rglob("*.bin"))


def main() -> int:
    roots = [pathlib.Path(p) for p in (sys.argv[1:] or default_roots())]

    try:
        from safetensors import safe_open
    except ImportError:
        safe_open = None

    checked = 0
    bad = 0
    empty = []
    for root in roots:
        if not root.is_dir():
            empty.append("%s (does not exist)" % root)
            continue
        files = weight_files(root)
        if not files:
            empty.append("%s (no weight files)" % root)
            continue
        print("%s" % root)
        for f in files:
            st = f.stat()
            logical = st.st_size
            allocated = st.st_blocks * 512
            ratio = allocated / logical if logical else 1.0
            problems = []

            if ratio < SPARSE_THRESHOLD:
                problems.append("sparse (%.2f GB allocated of %.2f GB)"
                                % (allocated / 1e9, logical / 1e9))

            if safe_open is not None and f.suffix == ".safetensors":
                try:
                    with safe_open(f, framework="pt") as h:
                        h.keys()
                except Exception as exc:
                    problems.append("unreadable header: %s" % exc)

            checked += 1
            rel = f.relative_to(root)
            if problems:
                bad += 1
                print("  BAD   %7.3f GB  %s\n          %s"
                      % (logical / 1e9, rel, "; ".join(problems)))
            else:
                print("  OK    %7.3f GB  %s" % (logical / 1e9, rel))

    print()
    for note in empty:
        # Not an error. A music-only or speech-only install is a supported
        # choice, and half these directories are legitimately absent.
        print("skipped: %s" % note)

    if not checked:
        print("\nNo weight files found in any of: %s"
              % ", ".join(str(r) for r in roots), file=sys.stderr)
        print("Nothing has been downloaded yet — ./anneal models", file=sys.stderr)
        return 2

    if safe_open is None:
        print("\nnote: safetensors is not importable under %s, so only sparseness "
              "was checked.\n      For the header check too, run it under the model "
              "environment:\n      $AIMUSIC_ROOT/gen-venv/bin/python verify-models.py"
              % sys.executable)

    if bad:
        print("\n%d file(s) incomplete. Re-download with Xet disabled:" % bad)
        print("  ./anneal models all")
        return 1
    print("\nAll %d weight files complete." % checked)
    return 0


if __name__ == "__main__":
    sys.exit(main())
