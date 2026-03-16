#!/usr/bin/env bash
#
# service.sh — manage Recordurbate as a macOS launchd daemon
#
# Usage:
#   ./service.sh enable   — install and load the launchd plist (runs at login)
#   ./service.sh disable  — unload and remove the launchd plist
#   ./service.sh start    — start the daemon now
#   ./service.sh stop     — stop the daemon now
#   ./service.sh status   — show whether the service is loaded and running
#

set -euo pipefail

LABEL="com.recordurbate.daemon"
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_SRC="${PROJECT_DIR}/launchd.plist"
PLIST_DST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
VENV_PYTHON="${PROJECT_DIR}/venv/bin/python"

# ── helpers ──────────────────────────────────────────────────────────

die() { echo "error: $*" >&2; exit 1; }

is_loaded() {
    launchctl list "$LABEL" &>/dev/null
}

generate_plist() {
    cat > "$PLIST_SRC" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-c</string>
        <string>cd "${PROJECT_DIR}" &amp;&amp; source ./venv/bin/activate &amp;&amp; python cli.py start</string>
    </array>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <false/>

    <key>WorkingDirectory</key>
    <string>${PROJECT_DIR}</string>

    <key>StandardOutPath</key>
    <string>${PROJECT_DIR}/configs/launchd.stdout.log</string>

    <key>StandardErrorPath</key>
    <string>${PROJECT_DIR}/configs/launchd.stderr.log</string>

    <key>ThrottleInterval</key>
    <integer>30</integer>
</dict>
</plist>
PLIST
}

# ── commands ─────────────────────────────────────────────────────────

cmd_enable() {
    [[ -x "$VENV_PYTHON" ]] || die "venv not found at ${VENV_PYTHON}"

    generate_plist
    mkdir -p "$(dirname "$PLIST_DST")"
    cp "$PLIST_SRC" "$PLIST_DST"

    if is_loaded; then
        launchctl unload "$PLIST_DST" 2>/dev/null || true
    fi
    launchctl load "$PLIST_DST"

    echo "Enabled: ${LABEL}"
    echo "  plist: ${PLIST_DST}"
    echo "  The daemon will start automatically at login."
}

cmd_disable() {
    if is_loaded; then
        launchctl unload "$PLIST_DST" 2>/dev/null || true
    fi
    rm -f "$PLIST_DST"
    echo "Disabled: ${LABEL}"
}

cmd_start() {
    if ! is_loaded; then
        if [[ -f "$PLIST_DST" ]]; then
            launchctl load "$PLIST_DST"
        else
            die "Service not enabled. Run './service.sh enable' first."
        fi
    fi
    launchctl start "$LABEL"
    echo "Started: ${LABEL}"
}

cmd_stop() {
    if is_loaded; then
        launchctl stop "$LABEL"
        echo "Stopped: ${LABEL}"
    else
        # Fall back to IPC stop in case the daemon was started manually
        echo "Service not loaded in launchd; sending IPC stop..."
        cd "$PROJECT_DIR"
        source ./venv/bin/activate
        python cli.py stop
    fi
}

cmd_status() {
    echo "Label:   ${LABEL}"
    echo "Plist:   ${PLIST_DST}"
    if [[ -f "$PLIST_DST" ]]; then
        echo "Enabled: yes"
    else
        echo "Enabled: no"
    fi
    if is_loaded; then
        echo "Loaded:  yes"
        # Show PID if running
        local pid
        pid=$(launchctl list "$LABEL" 2>/dev/null | awk 'NR==2{print $1}')
        if [[ "$pid" =~ ^[0-9]+$ ]]; then
            echo "PID:     ${pid}"
        else
            echo "PID:     (not running)"
        fi
    else
        echo "Loaded:  no"
    fi
}

# ── main ─────────────────────────────────────────────────────────────

case "${1:-}" in
    enable)  cmd_enable  ;;
    disable) cmd_disable ;;
    start)   cmd_start   ;;
    stop)    cmd_stop    ;;
    status)  cmd_status  ;;
    *)
        echo "Usage: $0 {enable|disable|start|stop|status}"
        exit 1
        ;;
esac
