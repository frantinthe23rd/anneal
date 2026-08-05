#!/usr/bin/env bash
# Download every model Anneal needs, at the revisions recorded in
# models.lock.json, then verify the files are actually complete.
#
# Pinned rather than latest: an unpinned re-download silently pulls whatever
# upstream is current, so a rebuilt machine can end up with different weights
# than the one that was tested. Change pins deliberately via ./update.sh.
#
# HF_HUB_DISABLE_XET=1 is not optional. The Xet transfer backend silently wrote
# *sparse* files to this external APFS volume — correct logical size, zero-filled
# interiors — which then failed deep inside model loading with an opaque
# "invalid JSON in header". That is what verify-models.py exists to catch.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$HERE/update.sh" --models
