#!/usr/bin/env bash
# (Re)download the ACE-Step 1.5 weights to the SSD.
#
# HF_HUB_DISABLE_XET=1 is required: the Xet transfer backend silently produced
# sparse (partially zero-filled) files on this external APFS volume, which then
# fail deep inside model loading. The classic HTTP path writes them correctly.
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/env.sh"

export HF_HUB_DISABLE_XET=1
export HF_HUB_ENABLE_HF_TRANSFER=0

mkdir -p "$ACESTEP_CHECKPOINTS_DIR"
cd "$ACESTEP_DIR"

echo "Downloading ACE-Step/Ace-Step1.5 -> $ACESTEP_CHECKPOINTS_DIR"
"$UV_BIN" run python - <<PY
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id="ACE-Step/Ace-Step1.5",
    local_dir="$ACESTEP_CHECKPOINTS_DIR",
    max_workers=4,
)
print("download finished")
PY

echo
"$(dirname "${BASH_SOURCE[0]}")/verify-models.py"
