#!/usr/bin/env bash
# Stop the supervisor (which stops the model backend) and tear down the tailnet proxy.
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/env.sh"

PIDFILE="$AIMUSIC_ROOT/supervisor.pid"

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

# Belt and braces: nothing should be left holding the model in memory.
pkill -f "supervisor.py" 2>/dev/null || true
pkill -f "acestep.api_server:app" 2>/dev/null || true
pkill -f "acestep-api" 2>/dev/null || true

"$TS_BIN" serve --https=443 off 2>/dev/null || true
"$TS_BIN" serve --http="${SUPERVISOR_PORT}" off 2>/dev/null || true

echo "Stopped."
