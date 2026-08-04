#!/usr/bin/env python3
"""Verify the downloaded ACE-Step weights are complete.

HuggingFace downloads to this external APFS volume have silently produced
*sparse* files — correct logical size, but large runs of never-written zero
blocks. safetensors only validates the header, so a bad file loads far enough
to fail deep inside model init with a confusing error.

This checks both: the safetensors header parses, and the file's allocated
blocks actually match its logical size.

    ./verify-models.py            # check $ACESTEP_CHECKPOINTS_DIR
    ./verify-models.py /some/dir
"""

from __future__ import annotations

import os
import sys
import pathlib

DEFAULT_DIR = os.environ.get("ACESTEP_CHECKPOINTS_DIR", "/Volumes/Storage/AIMusic/models")

# A file is suspect if allocated bytes fall below this fraction of logical size.
# Real compression/cloning is not in play here, so anything under ~0.98 is a hole.
SPARSE_THRESHOLD = 0.98


def main() -> int:
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DIR)
    if not root.is_dir():
        print(f"ERROR: {root} does not exist", file=sys.stderr)
        return 2

    try:
        from safetensors import safe_open
    except ImportError:
        safe_open = None
        print("note: safetensors not importable, checking sparseness only\n")

    files = sorted(root.rglob("*.safetensors")) + sorted(root.rglob("*.bin"))
    if not files:
        print(f"ERROR: no weight files under {root}", file=sys.stderr)
        return 2

    bad = 0
    for f in files:
        st = f.stat()
        logical = st.st_size
        allocated = st.st_blocks * 512
        ratio = allocated / logical if logical else 1.0
        problems = []

        if ratio < SPARSE_THRESHOLD:
            problems.append(f"sparse ({allocated / 1e9:.2f} GB allocated of {logical / 1e9:.2f} GB)")

        if safe_open is not None and f.suffix == ".safetensors":
            try:
                with safe_open(f, framework="pt") as h:
                    h.keys()
            except Exception as exc:
                problems.append(f"unreadable header: {exc}")

        rel = f.relative_to(root)
        if problems:
            bad += 1
            print(f"BAD   {logical / 1e9:7.3f} GB  {rel}\n        {'; '.join(problems)}")
        else:
            print(f"OK    {logical / 1e9:7.3f} GB  {rel}")

    print()
    if bad:
        print(f"{bad} file(s) incomplete. Re-download with Xet disabled:")
        print("  ./download-models.sh")
        return 1
    print(f"All {len(files)} weight files complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
