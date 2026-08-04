#!/usr/bin/env bash
# Shared environment for the local ACE-Step 1.5 music generation server.
# Everything heavy (venv, wheel cache, model weights, generated audio) lives on
# the external SSD at /Volumes/Storage/AIMusic — the internal disk is nearly full.

export AIMUSIC_ROOT="/Volumes/Storage/AIMusic"
export ACESTEP_DIR="$AIMUSIC_ROOT/ACE-Step-1.5"

# --- keep all bulk data off the internal disk ---
export UV_CACHE_DIR="$AIMUSIC_ROOT/uv-cache"
export UV_PYTHON_INSTALL_DIR="$AIMUSIC_ROOT/uv-python"
export HF_HOME="$AIMUSIC_ROOT/hf-cache"
export ACESTEP_CHECKPOINTS_DIR="$AIMUSIC_ROOT/models"

# The Xet transfer backend silently writes sparse (zero-filled) weight files on
# this external APFS volume; the model then fails deep inside loading with an
# "invalid JSON in header" error. Force the classic HTTP download path.
export HF_HUB_DISABLE_XET=1
export HF_HUB_ENABLE_HF_TRANSFER=0

# --- Apple Silicon native acceleration ---
export ACESTEP_LM_BACKEND="mlx"
export TOKENIZERS_PARALLELISM="false"
export ACESTEP_INIT_LLM="auto"

# --- server ---
# Bound to loopback only. Tailnet access is provided by `tailscale serve`
# (see start-api.sh), so the API is never exposed to the local LAN.
export ACESTEP_API_HOST="127.0.0.1"
export ACESTEP_API_PORT="8001"

# The API key lives in env.local.sh, which is gitignored. Generated on first run.
_ENV_LOCAL="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/env.local.sh"
if [[ ! -f "$_ENV_LOCAL" ]]; then
    printf '#!/usr/bin/env bash\n# Local secrets — not tracked in git.\nexport ACESTEP_API_KEY="sk-aimusic-%s"\n' \
        "$(LC_ALL=C tr -dc 'A-Za-z0-9_-' </dev/urandom | head -c 32)" >"$_ENV_LOCAL"
    chmod 600 "$_ENV_LOCAL"
    echo "Generated a new API key in $_ENV_LOCAL" >&2
fi
source "$_ENV_LOCAL"

# --- on-demand model lifecycle ---
# supervisor.py owns port 8001 and runs ACE-Step on 8011, starting it when a
# request arrives and stopping it once idle. ACE-Step pins ~7 GB once loaded and
# has no idle-unload of its own, so ending the process is the only way to get
# the memory back on a 16 GB machine.
export SUPERVISOR_HOST="127.0.0.1"
export SUPERVISOR_PORT="8001"
export ACESTEP_BACKEND_PORT="8011"
# Seconds of no requests (and no queued/running jobs) before the model is unloaded.
export ACESTEP_IDLE_TIMEOUT="600"

# LM used for prompt/lyric planning. ACE-Step classifies this machine as tier4
# (11.8 GB usable unified memory) and only permits the 0.6B LM at that tier —
# asking for the bundled 1.7B here is silently overridden, so don't bother.
export ACESTEP_LM_MODEL_PATH="acestep-5Hz-lm-0.6B"

export UV_BIN="/opt/homebrew/bin/uv"
export TS_BIN="/Applications/Tailscale.app/Contents/MacOS/Tailscale"
export TAILNET_HOST="jons-mac-mini.pangolin-darter.ts.net"
