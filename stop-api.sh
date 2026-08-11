#!/usr/bin/env bash
# Stop the supervisor (which stops the model backend) and tear down the tailnet proxy.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/env.sh"

PIDFILE="$AIMUSIC_ROOT/supervisor.pid"

# If launchd owns the gateway, tell launchd — otherwise KeepAlive restarts it
# and "stopped" lasts about a second. The plist is left in place, so the next
# login (or ./start-api.sh) brings it back; this only stops it *now*, which is
# what someone running stop-api.sh means.
LABEL="com.anneal.gateway"
if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    echo "Unloading $LABEL (launchd would otherwise restart it) ..."
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
fi

if [[ -f "$PIDFILE" ]]; then
    PID="$(cat "$PIDFILE")"
    if kill -0 "$PID" 2>/dev/null; then
        echo "Stopping supervisor pid $PID ..."
        kill "$PID"                       # SIGTERM -> supervisor stops the backend
        for _ in $(seq 1 40); do
            kill -0 "$PID" 2>/dev/null || break
            sleep 1
        done
        kill -9 "$PID" 2>/dev/null || true
    fi
    rm -f "$PIDFILE"
fi

# Belt and braces: nothing should be left holding a model in memory.
#
# The backend patterns come from services.SERVICES — each entry already carries
# the command it is started with — rather than being named here. Two names were
# written here when music was the only backend, and speech, image and text were
# then never killed: they survived every restart, were reparented to init, and
# went on holding their weights while /health reported them cold (issue #46).
pkill -f "supervisor.py" 2>/dev/null || true

PATTERNS="$(/usr/bin/python3 "$HERE/services.py" --stop-patterns 2>/dev/null || true)"
if [[ -z "$PATTERNS" ]]; then
    echo "WARNING: could not read the service table, so no backend was killed by" >&2
    echo "         name. Check for leftovers: ps -A -o pid,ppid,command | grep -i anneal" >&2
fi
while IFS= read -r pattern; do
    [[ -n "$pattern" ]] || continue
    pkill -f "$pattern" 2>/dev/null || true
done <<<"$PATTERNS"

# Only tear the proxy down if this machine is the one that put it up. Stopping
# the gateway used to remove the serve config unconditionally and start-api.sh
# put it back unconditionally, which balanced. Now that exposure is opt-in, an
# unconditional teardown means the first stop after the change takes a working
# machine off the tailnet and nothing restores it.
if [[ "${ANNEAL_EXPOSE:-loopback}" == "tailnet" && -n "${TS_BIN:-}" ]]; then
    "$TS_BIN" serve --https=443 off 2>/dev/null || true
    "$TS_BIN" serve --http="${SUPERVISOR_PORT}" off 2>/dev/null || true
fi

echo "Stopped."
