#!/usr/bin/env bash
# launchd entry point. Not for interactive use — run ./start-api.sh instead.
#
# Two things make this different from start-api.sh, and both are the reason it
# exists as a separate file rather than a flag.
#
# 1. It waits for the external volume. Models, venvs and outputs live on
#    /Volumes/Storage, and launchd starts login items well before an external
#    disk is mounted. start-api.sh is right to fail immediately when the volume
#    is missing — a human typed the command and wants to know. At boot the same
#    condition is normal and temporary, so this waits instead.
#
# 2. It runs the supervisor in the FOREGROUND and execs into it. start-api.sh
#    backgrounds it and exits, which is what an interactive caller wants and
#    exactly wrong for launchd: the job would look like it exited successfully
#    every time, and KeepAlive would restart it forever. Exec'ing means the pid
#    launchd supervises is the supervisor itself, so KeepAlive, crash restarts
#    and `launchctl kill` all mean what they say.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# env.sh first, so the root is resolved by the same rules as everything else
# rather than by a second copy of the default that can drift from it. It only
# builds strings and reads files inside the repo, so it is safe to source
# before the volume that holds the models is mounted.
#
# One caveat, and it is why setup.sh always writes .anneal-root: with no
# AIMUSIC_ROOT in the environment and no .anneal-root, env.sh detects the
# legacy external volume by looking *inside* it — which cannot work before it
# is mounted. The recorded root removes the ordering problem entirely.
source "$HERE/env.sh"

ROOT="$AIMUSIC_ROOT"
WAIT_SECONDS="${ANNEAL_BOOT_WAIT:-300}"
waited=0
while [[ ! -d "$ROOT" ]]; do
    if (( waited >= WAIT_SECONDS )); then
        echo "$(date '+%F %T') boot: $ROOT never appeared after ${WAIT_SECONDS}s — giving up" >&2
        [[ -s "$HERE/.anneal-root" ]] \
            || echo "$(date '+%F %T') boot: no .anneal-root recorded — run ./setup.sh, or set AIMUSIC_ROOT" >&2
        exit 1
    fi
    [[ $waited -eq 0 ]] && echo "$(date '+%F %T') boot: waiting for $ROOT to mount ..."
    sleep 5
    waited=$((waited + 5))
done
[[ $waited -gt 0 ]] && echo "$(date '+%F %T') boot: $ROOT appeared after ${waited}s"

LOG="$AIMUSIC_ROOT/supervisor.log"
PIDFILE="$AIMUSIC_ROOT/supervisor.pid"

if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "$(date '+%F %T') boot: already running (pid $(cat "$PIDFILE")) — nothing to do"
    exit 0
fi
rm -f "$PIDFILE"

# Same upstream setup start-api.sh does. Both are idempotent by design.
if [[ -d "$ACESTEP_DIR" && ! -L "$ACESTEP_DIR/checkpoints" ]]; then
    mkdir -p "$ACESTEP_CHECKPOINTS_DIR"
    rm -rf "$ACESTEP_DIR/checkpoints"
    ln -s "$ACESTEP_CHECKPOINTS_DIR" "$ACESTEP_DIR/checkpoints"
fi
if ! "$HERE/patches/apply_patches.py"; then
    echo "$(date '+%F %T') boot: WARNING — an upstream patch could not be applied" >&2
fi

# Exposure stays opt-in here too. An existing serve configuration lives in
# Tailscale's own state and survives a reboot on its own, which is why the
# tailnet URL can be up and answering 502 while nothing is listening behind it.
if [[ "${ANNEAL_EXPOSE:-loopback}" == "tailnet" && -n "${TS_BIN:-}" ]]; then
    if ! "$TS_BIN" serve status 2>/dev/null | grep -q "127.0.0.1:${SUPERVISOR_PORT}"; then
        echo "$(date '+%F %T') boot: configuring tailscale serve"
        "$TS_BIN" serve --bg --https=443 "http://127.0.0.1:${SUPERVISOR_PORT}" 2>/dev/null \
            || "$TS_BIN" serve --bg --http="${SUPERVISOR_PORT}" "http://127.0.0.1:${SUPERVISOR_PORT}" \
            || echo "$(date '+%F %T') boot: WARNING — tailscale serve failed" >&2
    fi
fi

export ACESTEP_DIR UV_BIN
echo "$(date '+%F %T') boot: starting supervisor on 127.0.0.1:${SUPERVISOR_PORT}"
echo $$ >"$PIDFILE"          # exec keeps this pid, so it stays correct
exec /usr/bin/python3 "$HERE/supervisor.py" >>"$LOG" 2>&1
