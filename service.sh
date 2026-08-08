#!/usr/bin/env bash
# Make Anneal survive a reboot, or stop it doing so.
#
#   ./service.sh install     start at login, and restart if it dies
#   ./service.sh uninstall   stop doing that; leaves a running gateway alone
#   ./service.sh status      whether it is installed, loaded and answering
#   ./service.sh plist       write and validate the plist, touching nothing else
#
# A LaunchAgent rather than a LaunchDaemon, deliberately. A daemon runs without
# anyone logged in, which sounds better for a headless Mac mini, but it would
# run as root with no user session — and `tailscale serve` is configured per
# user. Getting that wrong produces a gateway that starts at boot and is
# reachable by nobody. If you genuinely need it up with no user logged in,
# enable automatic login for this user; that keeps one code path rather than two.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.anneal.gateway"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
# env.sh must succeed here: AIMUSIC_ROOT decides which directory the Full Disk
# Access probe checks and which root the plist's job will use, and a swallowed
# failure would silently probe the empty string. It is sourced quietly, not
# optionally.
source "$HERE/env.sh" >/dev/null

# The interpreter launchd runs, and the only thing that needs Full Disk Access.
#
# Models, venvs and outputs live on an external volume, and macOS blocks a
# LaunchAgent from touching one entirely — measured, not assumed: a probe agent
# could not even `ls` the directory, let alone read a model. Boot persistence
# therefore requires a Full Disk Access grant no matter how it is arranged.
#
# Granting it to /bin/bash would work and is what most guides say to do. It also
# means every shell script anything on this machine runs — including one piped
# from the internet — inherits full disk access. A private copy takes the grant
# instead, so the blast radius is this job. Same reasoning that keeps rembg out
# of the pinned model environment: the narrow thing is barely more work.
SHELL_DIR="$HOME/Library/Anneal"
SHELL_BIN="$SHELL_DIR/anneal-bash"

# Logs go to the home directory, not the volume. launchd opens StandardOutPath
# itself, before the job runs and before any grant applies to it, so a path on
# the external volume fails the spawn outright with EX_CONFIG and no diagnostic
# anywhere. Measured. The supervisor's own log still lives beside the models.
LOG_DIR="$HOME/Library/Logs/Anneal"
LOG="$LOG_DIR/launchd.log"

ensure_shell() {
    mkdir -p "$SHELL_DIR" "$LOG_DIR"
    # Re-copy if missing or stale: a bash updated by the OS leaves a copy that
    # still runs but no longer matches, and re-copying invalidates the grant, so
    # say so rather than letting it fail silently at the next reboot.
    local made=1
    if [[ ! -x "$SHELL_BIN" ]] || ! cmp -s /bin/bash "$SHELL_BIN"; then
        # /bin/bash is mode 555, so the copy is read-only too and a second cp
        # over it fails with "Permission denied" — quietly, since the next line
        # then reports success anyway. Remove first, and check.
        rm -f "$SHELL_BIN"
        if ! cp /bin/bash "$SHELL_BIN"; then
            echo "Could not create $SHELL_BIN" >&2
            exit 1
        fi
        # Copying a signed system binary invalidates its signature and macOS
        # kills the copy outright — SIGKILL, before it runs a single line. An
        # ad-hoc signature makes it runnable again. Found the hard way.
        codesign --force --sign - "$SHELL_BIN" >/dev/null 2>&1
        echo "Created $SHELL_BIN (ad-hoc signed)"
        made=0
    fi
    return $made
}

# Can *this shell* read the volume through the copy? Only ever a negative test.
#
# Run from a terminal, it inherits that terminal's own disk access, so a pass
# proves nothing about launchd — the context that actually matters. A failure
# does prove the grant is missing. So it is used to rule out early, never to
# claim success; success is verified after install by seeing whether the job
# actually comes up.
probably_denied() {
    ! "$SHELL_BIN" -c "ls '${AIMUSIC_ROOT}'" >/dev/null 2>&1
}

grant_instructions() {
    cat <<MSG

  Anneal needs permission to read $SHELL_DIR's copy of bash
  against the volume the models live on.

  Usually macOS just asks. The first time launchd runs anneal-bash you get a
  permission prompt naming it — allow that and everything works; it is why the
  copy is ad-hoc signed rather than run as plain /bin/bash, which cannot prompt
  for anything and is simply denied.

  If no prompt appeared, or it was dismissed, grant it by hand:
    1. System Settings -> Privacy & Security -> Full Disk Access
    2. Click +, press Cmd-Shift-G, paste:  $SHELL_DIR
    3. Choose anneal-bash, and make sure its toggle is on

  Then run ./service.sh install again. Nothing else on this machine gains
  access — the grant applies to this copy only.
MSG
}

write_plist() {
    mkdir -p "$(dirname "$PLIST")"
    cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>

  <!-- boot.sh, not start-api.sh: it waits for the external volume and then
       execs the supervisor in the foreground, so the pid launchd watches is
       the real one. start-api.sh backgrounds and exits, which would look like
       a clean exit on every launch. -->
  <key>ProgramArguments</key>
  <array>
    <string>$SHELL_BIN</string>
    <string>$HERE/boot.sh</string>
  </array>

  <key>RunAtLoad</key><true/>

  <!-- Restart on crash, but not after a deliberate stop. stop-api.sh exits the
       supervisor cleanly (0), so SuccessfulExit=false leaves it stopped instead
       of fighting the person who stopped it. -->
  <key>KeepAlive</key>
  <dict><key>SuccessfulExit</key><false/></dict>

  <!-- Do not hammer a machine whose disk never mounts. -->
  <key>ThrottleInterval</key><integer>60</integer>

  <key>WorkingDirectory</key><string>$HERE</string>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLIST_EOF
    plutil -lint "$PLIST" >/dev/null
}

loaded() { launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; }

case "${1:-status}" in
install)
    ensure_shell || true
    if probably_denied; then
        grant_instructions
        exit 1
    fi
    write_plist
    loaded && launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true

    # Stop a hand-started gateway first, or the verification below is worthless:
    # boot.sh sees the pidfile, exits 0 without doing anything, and the health
    # check passes against the process that was already there. "Installed and
    # running" would then be true of the wrong process, and the machine would
    # still come back dead after a reboot.
    if curl -fsS -m 2 "http://127.0.0.1:${SUPERVISOR_PORT:-8001}/health" >/dev/null 2>&1; then
        echo "Stopping the hand-started gateway so launchd owns it ..."
        "$HERE/stop-api.sh" >/dev/null 2>&1 || true
    fi

    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "Installed $LABEL. Verifying it actually starts ..."

    # The only verification worth having. A pre-flight check runs in this
    # terminal's permission context, not launchd's, so it can pass while the
    # real job is denied everything — which is exactly what happened here.
    for _ in $(seq 1 45); do
        curl -fsS -m 2 "http://127.0.0.1:${SUPERVISOR_PORT:-8001}/health" >/dev/null 2>&1 && break
        sleep 1
    done
    if curl -fsS -m 2 "http://127.0.0.1:${SUPERVISOR_PORT:-8001}/health" >/dev/null 2>&1; then
        echo "Running. Anneal will now start at login and restart if it dies."
        echo "  runs:  $SHELL_BIN $HERE/boot.sh"
        echo "  plist: $PLIST"
        echo "  log:   $LOG"
    else
        echo
        echo "  The job is installed but did not come up." >&2
        echo "  Almost always Full Disk Access. Last launchd exit code:" >&2
        launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null \
            | grep -E "last exit code" | sed 's/^/    /' >&2
        echo "  Recent log ($LOG):" >&2
        tail -5 "$LOG" 2>/dev/null | sed 's/^/    /' >&2
        grant_instructions >&2
        exit 1
    fi
    ;;
uninstall)
    if loaded; then launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true; fi
    rm -f "$PLIST"
    echo "Removed $LABEL. A gateway running right now is left alone — ./stop-api.sh stops it."
    echo "$SHELL_BIN is left in place; remove it, and its Full Disk Access entry,"
    echo "if you are not reinstalling."
    ;;
plist)
    # Generate and lint only — no launchctl. Exists so the tests can check the
    # plist without registering a job on the machine running them, which is
    # exactly what they did once: a test bootstrapped a LaunchAgent pointing at
    # a plist in its own temp directory and left it behind after the directory
    # was deleted.
    ensure_shell >/dev/null || true
    write_plist
    echo "$PLIST"
    ;;
status)
    [[ -f "$PLIST" ]] && echo "plist:   installed at $PLIST" || echo "plist:   not installed"
    loaded && echo "launchd: loaded" || echo "launchd: not loaded"
    if [[ ! -x "$SHELL_BIN" ]]; then
        echo "access:  $SHELL_BIN not created yet"
    elif probably_denied; then
        echo "access:  DENIED — anneal-bash cannot read ${AIMUSIC_ROOT}"
        echo "         a reboot will not bring Anneal back until this is granted"
    else
        # Deliberately not "granted": this ran in your shell, which has its own
        # permissions. Whether launchd can is answered by the line below.
        echo "access:  readable from this terminal (says nothing about launchd)"
    fi
    if curl -fsS -m 5 "http://127.0.0.1:${SUPERVISOR_PORT:-8001}/health" >/dev/null 2>&1; then
        echo "gateway: answering on 127.0.0.1:${SUPERVISOR_PORT:-8001}"
    else
        echo "gateway: not answering on 127.0.0.1:${SUPERVISOR_PORT:-8001}"
    fi
    ;;
*)
    echo "usage: $0 {install|uninstall|status|plist}" >&2; exit 2 ;;
esac
