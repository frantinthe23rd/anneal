#!/usr/bin/env bash
# Start the music generation API and expose it on the tailnet.
#
# What actually starts here is supervisor.py, which is small and always-on.
# It launches ACE-Step on demand and stops it once idle, so the ~7 GB of model
# weights are only resident while they're being used.
set -euo pipefail

# Captured before env.sh, which resolves and exports AIMUSIC_ROOT — afterwards
# there is no way to tell a value the caller chose from one env.sh worked out,
# and the error below would confidently name the wrong reason.
_ROOT_FROM_ENV="${AIMUSIC_ROOT:-}"

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/env.sh"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

LOG="$AIMUSIC_ROOT/supervisor.log"
PIDFILE="$AIMUSIC_ROOT/supervisor.pid"

if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "Already running (pid $(cat "$PIDFILE")). Use ./stop-api.sh first."
    exit 0
fi

# stop-api.sh unloads the launchd job so its KeepAlive cannot undo the stop.
# Restore it here, so the pair stays symmetrical and a stop/start cycle does not
# quietly leave the machine without boot persistence it was configured to have.
LABEL="com.anneal.gateway"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
if [[ -f "$PLIST" ]] && ! launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    echo "Restoring launchd job $LABEL ..."
    launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || true
    # launchd starts it via boot.sh (RunAtLoad), so there is nothing else to do.
    for _ in $(seq 1 30); do
        curl -fsS -m 2 "http://127.0.0.1:${SUPERVISOR_PORT}/health" >/dev/null 2>&1 && break
        sleep 1
    done
    if curl -fsS -m 2 "http://127.0.0.1:${SUPERVISOR_PORT}/health" >/dev/null 2>&1; then
        echo "Started by launchd. http://127.0.0.1:${SUPERVISOR_PORT}"
        exit 0
    fi
    echo "launchd did not bring it up; starting directly instead." >&2
fi

# This used to say "is the Storage SSD mounted?", which on a fresh clone was a
# hardware diagnosis for a default nobody else could satisfy — the single most
# misleading message in the repo, and the opening example in #17. Say which root
# was chosen and where the choice came from, then name the command that makes it.
if [[ ! -d "$AIMUSIC_ROOT" ]]; then
    echo "ERROR: the install root $AIMUSIC_ROOT does not exist." >&2
    if [[ -n "$_ROOT_FROM_ENV" ]]; then
        echo "  (from AIMUSIC_ROOT in the environment)" >&2
    elif [[ -s "$HERE/.anneal-root" ]]; then
        echo "  (recorded in $HERE/.anneal-root — an external volume that is not mounted?)" >&2
    else
        echo "  (the default — nothing has been installed here yet)" >&2
    fi
    echo >&2
    echo "  ./setup.sh        install, or finish an install" >&2
    echo "  ./anneal doctor   what is missing, and the command that fixes it" >&2
    exit 1
fi

# The API server hardcodes <project>/checkpoints and ignores ACESTEP_CHECKPOINTS_DIR,
# so point it at the shared model dir. Without this it re-downloads ~9.4 GB.
# mkdir first: without it this makes a dangling symlink on a machine whose
# weights have not been downloaded yet, and upstream then treats the target as
# absent and starts fetching 9.4 GB of its own.
if [[ -d "$ACESTEP_DIR" && ! -L "$ACESTEP_DIR/checkpoints" ]]; then
    mkdir -p "$ACESTEP_CHECKPOINTS_DIR"
    rm -rf "$ACESTEP_DIR/checkpoints"
    ln -s "$ACESTEP_CHECKPOINTS_DIR" "$ACESTEP_DIR/checkpoints"
fi

# Upstream fixes live outside this repo, so re-apply them on every start.
# Idempotent, and loud if an anchor stops matching after an upstream update.
if ! "$HERE/patches/apply_patches.py"; then
    echo "WARNING: an upstream patch could not be applied — see above." >&2
fi

export ACESTEP_DIR UV_BIN

echo "Starting supervisor on http://${SUPERVISOR_HOST}:${SUPERVISOR_PORT} ..."
nohup /usr/bin/python3 "$HERE/supervisor.py" >>"$LOG" 2>&1 &
echo $! >"$PIDFILE"

echo -n "Waiting for supervisor"
for _ in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:${SUPERVISOR_PORT}/health" >/dev/null 2>&1; then
        echo " OK"
        break
    fi
    if ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo
        echo "ERROR: supervisor exited. Last log lines:" >&2
        tail -30 "$LOG" >&2
        exit 1
    fi
    echo -n "."
    sleep 1
done

# Reach beyond loopback is opt-in. The supervisor only ever binds 127.0.0.1;
# what puts Anneal on the tailnet is `tailscale serve`, and configuring that
# automatically means a fresh clone exposes itself to everyone on the tailnet
# without being asked. That is a surprising default for something a stranger
# just downloaded, so it now takes ANNEAL_EXPOSE=tailnet.
#
# An existing serve configuration is left alone either way: it lives in
# Tailscale, not here, so machines already set up keep working and the check
# below simply finds it and says so.
ALREADY_SERVED=""
if [[ -n "$TS_BIN" ]] && "$TS_BIN" serve status 2>/dev/null | grep -q "127.0.0.1:${SUPERVISOR_PORT}"; then
    ALREADY_SERVED="yes"
fi

if [[ -n "$ALREADY_SERVED" ]]; then
    echo "Tailnet: already configured, left as it is."
elif [[ "${ANNEAL_EXPOSE:-loopback}" != "tailnet" ]]; then
    echo "Loopback only. Set ANNEAL_EXPOSE=tailnet to serve on the tailnet."
elif [[ -z "$TS_BIN" ]]; then
    echo "ANNEAL_EXPOSE=tailnet but Tailscale was not found — staying on loopback."
else
    echo "Configuring tailscale serve..."
    "$TS_BIN" serve --bg --https=443 "http://127.0.0.1:${SUPERVISOR_PORT}" 2>/dev/null \
        || "$TS_BIN" serve --bg --http="${SUPERVISOR_PORT}" "http://127.0.0.1:${SUPERVISOR_PORT}"
fi

echo
echo "Local:   http://127.0.0.1:${SUPERVISOR_PORT}"
[[ -n "$TS_BIN" && ( -n "$ALREADY_SERVED" || "${ANNEAL_EXPOSE:-loopback}" == "tailnet" ) ]] \
    && echo "Tailnet: https://${TAILNET_HOST}"
echo "Status:  curl -s http://127.0.0.1:${SUPERVISOR_PORT}/supervisor/status"
echo "Logs:    $LOG  and  $AIMUSIC_ROOT/api-server.log"
echo
echo "The model loads on the first generation request (~3-4 min) and unloads"
echo "after ${ACESTEP_IDLE_TIMEOUT}s idle. Pre-warm with:"
echo "  curl -X POST -H \"Authorization: Bearer \$ACESTEP_API_KEY\" http://127.0.0.1:${SUPERVISOR_PORT}/supervisor/start"
