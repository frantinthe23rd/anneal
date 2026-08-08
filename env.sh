#!/usr/bin/env bash
# Shared environment for Anneal. Everything heavy (venvs, wheel cache, model
# weights, generated audio, logs) lives under one directory, $AIMUSIC_ROOT.

# Where models, venvs, logs and output live. Resolved in the same order, and
# with the same rules, as paths.aimusic_root() — the two must agree, because
# bash decides what the launchers do and Python decides what the gateway does,
# and a disagreement means the server writes somewhere the scripts do not look.
# tests/unit/test_root_resolution.py pins them together.
#
#   1. $AIMUSIC_ROOT              explicit wins, always
#   2. <repo>/.anneal-root        written by setup.sh, gitignored
#   3. /Volumes/Storage/AIMusic   only if it exists *and* looks like an install
#   4. ~/anneal                   the default for everyone else
#
# The old default was (3) unconditionally, which is one person's external SSD.
# On a fresh clone that produced "is the Storage SSD mounted?" — a hardware
# fault, apparently, rather than a default nobody else can satisfy.
_ANNEAL_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# `read` rather than a pipeline: `head -1` mid-pipeline SIGPIPEs the stage
# feeding it, and under the launchers' `set -euo pipefail` that silently aborts
# the whole script. This repo has been bitten by exactly that once already.
if [[ -z "${AIMUSIC_ROOT:-}" && -s "$_ANNEAL_REPO/.anneal-root" ]]; then
    IFS= read -r AIMUSIC_ROOT < "$_ANNEAL_REPO/.anneal-root" || true
    AIMUSIC_ROOT="${AIMUSIC_ROOT#"${AIMUSIC_ROOT%%[![:space:]]*}"}"
    AIMUSIC_ROOT="${AIMUSIC_ROOT%"${AIMUSIC_ROOT##*[![:space:]]}"}"
    AIMUSIC_ROOT="${AIMUSIC_ROOT/#\~/$HOME}"
fi
# ANNEAL-LEGACY-ROOT — the one place bash is allowed to name it. Overridable
# only so the tests can reach branches 3 and 4 on the machine that has the
# volume; with it mounted they are otherwise unreachable.
_ANNEAL_LEGACY_ROOT="${ANNEAL_LEGACY_ROOT:-/Volumes/Storage/AIMusic}"  # ANNEAL-LEGACY-ROOT
if [[ -z "${AIMUSIC_ROOT:-}" ]]; then
    for _marker in models gen-venv hf-cache ACE-Step-1.5 outputs; do
        if [[ -e "$_ANNEAL_LEGACY_ROOT/$_marker" ]]; then
            AIMUSIC_ROOT="$_ANNEAL_LEGACY_ROOT"
            break
        fi
    done
    unset _marker
fi
export AIMUSIC_ROOT="${AIMUSIC_ROOT:-$HOME/anneal}"
export ACESTEP_DIR="${ACESTEP_DIR:-$AIMUSIC_ROOT/ACE-Step-1.5}"

# --- keep all bulk data off the internal disk ---
export UV_CACHE_DIR="$AIMUSIC_ROOT/uv-cache"
export UV_PYTHON_INSTALL_DIR="$AIMUSIC_ROOT/uv-python"
# launchd hands a job PATH=/usr/bin:/bin:/usr/sbin:/sbin, so Homebrew is absent
# and ffmpeg — which speech and press downloads both need — cannot be found.
# The code resolves it explicitly as well; this is so anything else spawned
# from here (uv, tailscale, a subprocess) behaves the same from a LaunchAgent
# as it does from a terminal.
for _bin in /opt/homebrew/bin /usr/local/bin; do
    case ":$PATH:" in
        *":$_bin:"*) ;;
        *) [[ -d "$_bin" ]] && export PATH="$_bin:$PATH" ;;
    esac
done
unset _bin

export HF_HOME="$AIMUSIC_ROOT/hf-cache"
export ACESTEP_CHECKPOINTS_DIR="$AIMUSIC_ROOT/models"

# The Xet transfer backend silently writes sparse (zero-filled) weight files on
# this external APFS volume; the model then fails deep inside loading with an
# "invalid JSON in header" error. Force the classic HTTP download path.
export HF_HUB_DISABLE_XET=1
export HF_HUB_ENABLE_HF_TRANSFER=0

# Every model runs locally. This makes that structural rather than merely true:
# with HF_HUB_OFFLINE set, the hub libraries resolve from the local cache and
# raise instead of silently reaching out, so a missing or mis-pinned model fails
# loudly rather than being fetched mid-request. download-models.sh and update.sh
# unset it deliberately, which are the only places a download should happen.
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
export TRANSFORMERS_OFFLINE="${TRANSFORMERS_OFFLINE:-1}"

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
_ENV_LOCAL="$_ANNEAL_REPO/env.local.sh"
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

# Resolve tools rather than hardcoding one machine's layout.
export UV_BIN="${UV_BIN:-$(command -v uv || echo /opt/homebrew/bin/uv)}"

# Executable is not the same as working: on macOS the Homebrew `tailscale`
# symlink points into the app bundle and dies with "The current bundleIdentifier
# is unknown to the registry" when run directly. Prefer the bundle, and prove
# each candidate answers before accepting it.
if [[ -z "${TS_BIN:-}" ]]; then
    for _candidate in \
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale" \
        "$(command -v tailscale 2>/dev/null || true)" \
        "/usr/bin/tailscale" \
        "/usr/local/bin/tailscale"; do
        [[ -n "$_candidate" && -x "$_candidate" ]] || continue
        if "$_candidate" status --json >/dev/null 2>&1; then export TS_BIN="$_candidate"; break; fi
    done
fi
export TS_BIN="${TS_BIN:-}"

# Reach beyond loopback is opt-in: `loopback` (default) or `tailnet`. The
# supervisor binds 127.0.0.1 regardless — this only decides whether
# start-api.sh configures `tailscale serve`. An existing serve config is never
# touched, so this changes fresh installs and not working ones.
export ANNEAL_EXPOSE="${ANNEAL_EXPOSE:-loopback}"

# Ask Tailscale for this machine's name instead of baking one host in. Empty is
# fine — it only affects the URL printed at startup.
# Deliberately no early-exiting filter (`grep -m1`, `head -1`) in this pipeline.
# The callers run `set -euo pipefail`, and an early exit SIGPIPEs the upstream
# stage, which makes the whole pipeline fail and silently aborts start-api.sh
# before it does anything. awk reads to EOF and prints the first match instead.
if [[ -z "${TAILNET_HOST:-}" && -n "$TS_BIN" && -x "$TS_BIN" ]]; then
    TAILNET_HOST="$("$TS_BIN" status --json 2>/dev/null \
        | tr ',' '\n' \
        | awk -F'"' '/DNSName/ && !seen { sub(/\.$/, "", $4); host = $4; seen = 1 } END { print host }' \
        )" || true
fi
export TAILNET_HOST="${TAILNET_HOST:-localhost}"
