#!/usr/bin/env bash
# Get from `git clone` to a running Anneal, on a Mac that has never seen it.
#
#   ./setup.sh                      ask what is needed, then do it
#   ./setup.sh --root ~/anneal --yes --models music,speech
#   ./setup.sh --no-models          everything except the weights
#   ./setup.sh --tools              also build tools-venv (sprites, screenshots)
#   ./setup.sh --sfx                also build sfx-venv (sound effects, Apple silicon)
#   ./setup.sh --dry-run            say what it would do, change nothing
#
# Safe to re-run. Every step checks whether it has already been done and says
# so rather than redoing it, because the most likely second run is after a
# failure partway through — a download interrupted, a disk that filled — and
# starting from the top should cost nothing but the step that failed.
#
# The order matters and used to be tribal knowledge:
#
#   1. prerequisites          uv, ffmpeg, git, Apple silicon, RAM, disk
#   2. a root to install into $AIMUSIC_ROOT, recorded in .anneal-root
#   3. the upstream checkout  nothing in this repo used to clone ACE-Step at all,
#                             yet three separate places assume it is there
#   4. gen-venv               --models runs Python from it, so it comes first
#   5. the weights            with the sizes stated before anything moves
#
# Steps 1-3 did not exist. Step 5's dependency on step 4 was undocumented and
# failed obscurely when reversed.
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Pinned like the ACE-Step checkout, and for the same reason: an unpinned clone
# is a different program later. MIT licensed; the weights it loads are not, and
# their terms are recorded in models.lock.json.
SFX_REPO="${SFX_REPO:-https://github.com/Stability-AI/stable-audio-3}"
SFX_PIN="${SFX_PIN:-a0b57f5483c4588f827f3552b7d5c6ca2a9687be}"
PYTHON="${PYTHON:-/usr/bin/python3}"

ROOT_ARG=""
ASSUME_YES=0
MODELS="required"
WANT_MODELS=1
WANT_TOOLS=0
WANT_SFX=0
DRY_RUN=0

# The header comment, to the first line that is not one — anchored to the shape
# of the file rather than to line numbers, so editing the preamble cannot start
# printing shell code as help.
usage() { awk 'NR > 1 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"; }

# `./setup.sh --root` with the path forgotten used to print nothing and exit 1:
# the branch consumed a value that was not there, and the loop's trailing
# `shift` then failed with nothing left to shift, which under `set -e` ends the
# script silently. An unknown option was already handled well — it is named,
# the usage follows, exit 2 — so a missing value is handled the same way.
need_value() {
    [[ -n "$2" ]] && return 0
    echo "$1 needs a value: $3" >&2
    usage >&2
    exit 2
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --root)      ROOT_ARG="${2:-}"
                     need_value "$1" "$ROOT_ARG" "a directory, as in --root ~/anneal"; shift ;;
        --root=*)    ROOT_ARG="${1#*=}"
                     need_value "--root" "$ROOT_ARG" "a directory, as in --root=~/anneal" ;;
        --models)    MODELS="${2:-}"
                     need_value "$1" "$MODELS" "what to fetch, as in --models music,speech (or all)"; shift ;;
        --models=*)  MODELS="${1#*=}"
                     need_value "--models" "$MODELS" "what to fetch, as in --models=music,speech (or all)" ;;
        --no-models) WANT_MODELS=0 ;;
        --tools)     WANT_TOOLS=1 ;;
        --sfx)       WANT_SFX=1 ;;
        -y|--yes)    ASSUME_YES=1 ;;
        -n|--dry-run) DRY_RUN=1 ;;
        -h|--help)   usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

STEP=""
STEPS_DONE=()

# A dry run also leaves the interpreter's bytecode cache behind — every Python
# it runs imports paths.py or tools/doctor.py, and on any interpreter that is
# not Apple's (which redirects the cache into ~/Library/Caches) that is a
# __pycache__ written into the checkout. Set before the first of them runs.
if (( DRY_RUN )); then export PYTHONDONTWRITEBYTECODE=1; fi

say()  {
    if [[ -n "$STEP" ]]; then STEPS_DONE+=("$STEP"); fi
    STEP="$*"
    printf '\n\033[1m==> %s\033[0m\n' "$*"
}
note() { printf '    %s\n' "$*"; }
run()  { if (( DRY_RUN )); then note "would run: $*"; else "$@"; fi; }

# What a non-zero exit leaves behind. The header promises that re-running picks
# up where it stopped, and every step checks whether it is already done — but a
# run killed by `set -e` said none of that, so a failure four steps in showed
# only the failing command's own message and looked like starting over. The
# steps reported are the `say` calls themselves, not a list written out here,
# so a step added later reports itself.
on_error() {
    local code=$? step
    printf '\n\033[1m==> Stopped during: %s\033[0m\n' "${STEP:-startup}" >&2
    if (( ${#STEPS_DONE[@]} )); then
        echo "    These are done, and are kept:" >&2
        for step in ${STEPS_DONE[@]+"${STEPS_DONE[@]}"}; do
            echo "      - $step" >&2
        done
    fi
    echo "    Fix what it reported above and run ./setup.sh again: every step" >&2
    echo "    checks whether it has already been done, so it resumes from here." >&2
    exit "$code"
}
trap on_error ERR

confirm() {
    (( ASSUME_YES )) && return 0
    [[ -t 0 ]] || { echo "Not a terminal and --yes was not given — stopping." >&2; return 1; }
    local reply
    read -r -p "    $1 [Y/n] " reply
    [[ -z "$reply" || "$reply" =~ ^[Yy] ]]
}

# --------------------------------------------------------------- 1. prerequisites
say "Checking prerequisites"
if ! "$PYTHON" "$HERE/tools/doctor.py" --prereqs; then
    echo
    echo "Fix the failures above and run ./setup.sh again." >&2
    echo "Nothing has been changed." >&2
    exit 1
fi

# --------------------------------------------------------------- 2. the root
# Everything bulky goes here: weights, both virtualenvs, the wheel cache, the
# upstream checkout, the databases, the logs and everything generated. It is
# recorded in .anneal-root — gitignored, per machine — so that env.sh, the
# gateway, the launchd job and the tests all resolve the same directory without
# anyone having to export a variable in every shell.
say "Choosing where to install"
CURRENT="$("$PYTHON" -c "import sys; sys.path.insert(0, '$HERE'); import paths; print(paths.aimusic_root())")"
if [[ -n "$ROOT_ARG" ]]; then
    ROOT="${ROOT_ARG/#\~/$HOME}"
elif (( ASSUME_YES )) || [[ ! -t 0 ]]; then
    ROOT="$CURRENT"
else
    note "Models, virtualenvs and everything you generate will live here."
    note "About 45 GB for the full set; ~20 GB for music and speech only."
    read -r -p "    Install root [$CURRENT]: " ROOT
    ROOT="${ROOT:-$CURRENT}"
    ROOT="${ROOT/#\~/$HOME}"
fi
# Made absolute through its parent, which is the only way to canonicalise a
# path that does not exist yet. The previous form appended the basename to an
# empty string whenever that `cd` failed, so `--root /no/such/parent/anneal`
# installed into `/anneal` — and its `|| ROOT="$ROOT"` guard could never fire,
# because an assignment takes its status from the last substitution and
# `basename` always succeeds (ShellCheck SC2269).
#
# A missing parent is now a refusal rather than a substitution. The way to
# produce one is `--root /Volumes/Something/anneal` with the volume not
# mounted, which README documents as the way onto an external disk; installing
# 45 GB somewhere else because a disk is unplugged is the worst answer
# available, and "mount it, or create the parent" is the useful one.
ROOT="${ROOT%/}"
if [[ -z "$ROOT" ]]; then
    echo "An install root is needed, and / is not one." >&2
    exit 2
fi
if ROOT_PARENT="$(cd "$(dirname "$ROOT")" 2>/dev/null && pwd)"; then
    ROOT="${ROOT_PARENT%/}/$(basename "$ROOT")"
else
    echo "Cannot install into $ROOT: $(dirname "$ROOT") does not exist." >&2
    echo "  Mount it if it is an external volume, or create it first:" >&2
    echo "      mkdir -p $(dirname "$ROOT")" >&2
    echo "Nothing has been changed." >&2
    exit 1
fi
note "Root: $ROOT"

run mkdir -p "$ROOT"
if (( ! DRY_RUN )); then
    printf '%s\n' "$ROOT" > "$HERE/.anneal-root"
    note "Recorded in $HERE/.anneal-root"
fi
export AIMUSIC_ROOT="$ROOT"
# The upstream checkout belongs under the root by construction — three separate
# places assume it is at $AIMUSIC_ROOT/ACE-Step-1.5. An ACESTEP_DIR inherited
# from some earlier shell would silently send the clone somewhere else, and the
# symlink and the patches with it, so the choice made above wins.
unset ACESTEP_DIR

# From here on env.sh is authoritative: it exports HF_HOME, the checkpoints
# directory, the offline flags and the API key, and generates env.local.sh on
# first use. Sourcing it after AIMUSIC_ROOT is exported means the choice above
# wins over every fallback in it.
#
# That generation is a written file, mode 600, holding a new API key — so a dry
# run, which says it changes nothing, wrote one. ANNEAL_DRY_RUN is how this
# script tells env.sh (and update.sh, which sources it too) which kind of run
# this is; env.sh describes the file instead of writing it.
export ANNEAL_DRY_RUN="$DRY_RUN"
# shellcheck source=/dev/null
source "$HERE/env.sh"
if (( DRY_RUN )) && [[ ! -f "$HERE/env.local.sh" ]]; then
    note "would generate an API key in $HERE/env.local.sh"
fi

# The free-disk check is worth repeating now that the root is known: the first
# one ran against whatever the default resolved to, which may be a different
# volume entirely.
"$PYTHON" - "$ROOT" "$HERE" <<'PYDISK'
import os, sys
sys.path.insert(0, os.path.join(sys.argv[2], "tools"))
from doctor import free_gb, MIN_DISK_GB, FULL_DISK_GB
# free_gb walks up to the nearest existing ancestor, so this works before the
# directory has been created -- which under --dry-run it has not been.
free = free_gb(sys.argv[1])
if free is None:
    print("    Could not measure free space on %s" % sys.argv[1])
elif free < MIN_DISK_GB:
    print("    WARNING: %.0f GB free. Music and speech alone want about %d GB, "
          "and everything about %d." % (free, MIN_DISK_GB, FULL_DISK_GB))
else:
    print("    Free on this volume: %.0f GB" % free)
PYDISK

# --------------------------------------------------------------- 3. ACE-Step
# services.py launches music with `uv run acestep-api` in $ACESTEP_DIR;
# start-api.sh symlinks $ACESTEP_DIR/checkpoints and patches the checkout. All
# three assumed a repository that nothing put there. models.lock.json has
# recorded the exact commit all along — only the step that consumes it was
# missing.
say "Upstream ACE-Step checkout"
ACESTEP_URL="$("$PYTHON" -c "
import json;u=json.load(open('$HERE/models.lock.json'))['upstream']['ACE-Step/ACE-Step-1.5']
print(u.get('url','https://github.com/ace-step/ACE-Step-1.5'))")"
ACESTEP_COMMIT="$("$PYTHON" -c "
import json;print(json.load(open('$HERE/models.lock.json'))['upstream']['ACE-Step/ACE-Step-1.5']['commit'])")"

if [[ -d "$ACESTEP_DIR/.git" ]]; then
    note "Already cloned at $ACESTEP_DIR"
else
    note "Cloning $ACESTEP_URL -> $ACESTEP_DIR (about 2.7 GB with its venv)"
    run git clone "$ACESTEP_URL" "$ACESTEP_DIR"
fi
if [[ -d "$ACESTEP_DIR/.git" ]]; then
    HEAD_NOW="$(git -C "$ACESTEP_DIR" rev-parse HEAD 2>/dev/null || echo none)"
    if [[ "$HEAD_NOW" == "$ACESTEP_COMMIT" ]]; then
        note "At the pinned commit ${ACESTEP_COMMIT:0:12}"
    else
        note "Checking out pinned ${ACESTEP_COMMIT:0:12} (was ${HEAD_NOW:0:12})"
        run git -C "$ACESTEP_DIR" fetch --quiet origin
        run git -C "$ACESTEP_DIR" checkout --quiet "$ACESTEP_COMMIT"
    fi
fi

# Upstream hardcodes <project>/checkpoints and ignores ACESTEP_CHECKPOINTS_DIR.
# Without the symlink it silently re-downloads 9.4 GB into the checkout.
if [[ ! -L "$ACESTEP_DIR/checkpoints" ]]; then
    note "Pointing $ACESTEP_DIR/checkpoints at $ACESTEP_CHECKPOINTS_DIR"
    run mkdir -p "$ACESTEP_CHECKPOINTS_DIR"
    run rm -rf "$ACESTEP_DIR/checkpoints"
    run ln -s "$ACESTEP_CHECKPOINTS_DIR" "$ACESTEP_DIR/checkpoints"
else
    note "checkpoints symlink already in place"
fi

if [[ -d "$ACESTEP_DIR" ]] && (( ! DRY_RUN )); then
    "$HERE/patches/apply_patches.py" >/dev/null || \
        note "WARNING: an upstream patch did not apply — ./start-api.sh will say which."
fi

# --------------------------------------------------------------- 4. gen-venv
say "Model environment (gen-venv)"
if [[ -x "$AIMUSIC_ROOT/gen-venv/bin/python" ]]; then
    note "Already built at $AIMUSIC_ROOT/gen-venv — leaving it alone."
    note "Rebuild deliberately with ./update.sh --deps"
else
    note "Building from gen-venv.lock.txt (~1.3 GB; speech, image and text run from it)"
    run "$HERE/update.sh" --deps
fi

# --------------------------------------------------------------- 5. weights
if (( WANT_MODELS )); then
    say "Model weights"
    # The same listing on both paths. It used to be a separate `|| true` call
    # under --dry-run, where it could only fail: it ran from gen-venv, which a
    # dry run has not built, so every preview of a first install ended in
    # "gen-venv/bin/python does not exist" — an error the reader cannot avoid
    # and did not cause. Listing needs nothing from gen-venv (it reads
    # models.lock.json and stats the checkpoints directory), and update.sh now
    # says so, which is also what makes `./anneal models list` work before the
    # install README tells the reader to price with it.
    "$HERE/update.sh" --models list "$MODELS"
    if (( DRY_RUN )); then
        note "A real run offers to download these; declining leaves them for later."
    else
        note "Downloads resume if interrupted — re-run ./setup.sh and it picks up."
        if confirm "Download these now?"; then
            "$HERE/update.sh" --models "$MODELS"
        else
            note "Skipped. Fetch later with: ./anneal models $MODELS"
        fi
    fi
else
    say "Model weights — skipped (--no-models)"
    note "Fetch later with: ./anneal models"
fi

# --------------------------------------------------------------- 6. tools-venv
# Deliberately separate from gen-venv: rembg pulls onnxruntime, and gen-venv is
# version-pinned because it serves the models. Coupling them would let a
# background-removal dependency block an image-model upgrade.
if (( WANT_SFX )); then
    say "Sound effects (sfx-venv)"
    # Its own environment, on the same reasoning as tools-venv: the runner is
    # pure MLX and the environment that serves the models is version-pinned, so
    # a sound-effects dependency must not be able to break music generation.
    # It is small — mlx, numpy, sentencepiece, soundfile — about 240 MB.
    if [[ -x "$AIMUSIC_ROOT/sfx-venv/bin/python" ]]; then
        note "Already built at $AIMUSIC_ROOT/sfx-venv"
    else
        note "Building (~240 MB)"
        run "$UV_BIN" venv --python 3.12 "$AIMUSIC_ROOT/sfx-venv"
    fi
    SFX_SRC="$AIMUSIC_ROOT/stable-audio-3"
    if [[ -d "$SFX_SRC/.git" ]]; then
        note "Runner already cloned at $SFX_SRC"
    else
        note "Cloning the runner (MIT) at the pinned commit"
        run git clone --filter=blob:none "$SFX_REPO" "$SFX_SRC"
    fi
    if [[ -d "$SFX_SRC/.git" ]]; then
        run git -C "$SFX_SRC" fetch --quiet origin "$SFX_PIN"
        run git -C "$SFX_SRC" checkout --quiet "$SFX_PIN"
        run "$UV_BIN" pip install --python "$AIMUSIC_ROOT/sfx-venv/bin/python" \
            -r "$SFX_SRC/optimized/mlx/requirements.txt"
    fi
    # The weights live in the shared model directory and are linked into the
    # checkout, exactly as ACE-Step's checkpoints are. Without this an update
    # that re-clones the checkout throws away 1.8 GB that is still on disk.
    if (( ! DRY_RUN )); then
        mkdir -p "$ACESTEP_CHECKPOINTS_DIR/stable-audio-3-mlx/MLX"
        rm -rf "$SFX_SRC/optimized/mlx/models/mlx"
        mkdir -p "$SFX_SRC/optimized/mlx/models"
        ln -sfn "$ACESTEP_CHECKPOINTS_DIR/stable-audio-3-mlx/MLX" \
                "$SFX_SRC/optimized/mlx/models/mlx"
        note "Weights link -> $ACESTEP_CHECKPOINTS_DIR/stable-audio-3-mlx/MLX"
    else
        note "would link the weights directory into the checkout"
    fi
    note "Fetch the weights with ./anneal models sfx (about 1.8 GB)"
fi

if (( WANT_TOOLS )); then
    say "Tooling environment (tools-venv)"
    if [[ -x "$AIMUSIC_ROOT/tools-venv/bin/python" ]]; then
        note "Already built at $AIMUSIC_ROOT/tools-venv"
    else
        note "Building (~140 MB, plus 554 MB of browsers for the UI screenshots)"
        run "$UV_BIN" venv --python 3.12 "$AIMUSIC_ROOT/tools-venv"
        run "$UV_BIN" pip install --python "$AIMUSIC_ROOT/tools-venv/bin/python" \
            "rembg[cpu]" pillow playwright
        PLAYWRIGHT_BROWSERS_PATH="$AIMUSIC_ROOT/playwright-browsers" \
            run "$AIMUSIC_ROOT/tools-venv/bin/playwright" install chromium
    fi
fi

# --------------------------------------------------------------- done
say "Where that leaves things"
if (( DRY_RUN )); then
    note "Nothing was changed: no root, no checkout, no venv, no weights."
    note "Run the same command without --dry-run to do it."
    exit 0
fi
# Only on a real run. A dry run installs nothing, so this reported every model
# missing and closed on "N required check(s) failed" — accurate, unavoidable,
# and reading as though the run had failed.
"$PYTHON" "$HERE/tools/doctor.py" || true

cat <<DONE

Next:

  ./anneal start            start the gateway (loopback only, by default)
  open http://127.0.0.1:${SUPERVISOR_PORT:-8001}

The first music request is slow — the model loads on demand and a cold start is
about 3-4 minutes on the reference machine. That is the design, not a hang:
only one heavy model fits in 16 GB, so each is loaded when asked for and
released once idle. Everything after that is faster.

  ./anneal status           what is installed and what is warm
  ./anneal models list      the models, their sizes, and which are optional
  ./anneal service install  start at login and restart if it dies
DONE
