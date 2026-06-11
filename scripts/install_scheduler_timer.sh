#!/usr/bin/env bash
# Phase 5B: Install systemd service/timer for scheduled searches.
#
# This script generates systemd unit files under deploy/systemd/ and prints
# installation instructions. It does NOT enable or start anything until you run
# the install commands it outputs.
#
# Usage:
#   bash scripts/install_scheduler_timer.sh          # generate + show instructions
#   bash scripts/install_scheduler_timer.sh --install  # copy files + enable timer

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEPLOY_DIR="$PROJECT_ROOT/deploy/systemd"
INSTALL=0

if [[ "${1:-}" == "--install" ]]; then
    INSTALL=1
fi

mkdir -p "$DEPLOY_DIR"

# ── Generate service file ───────────────────────────────────────────────
cat > "$DEPLOY_DIR/vacation-scheduler.service" <<'UNIT'
[Unit]
Description=Vacation Deal Agent — Scheduled Search Runner (Phase 5B)
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
EnvironmentFile=%h/.env.deploy
WorkingDirectory=/opt/vacation-deal-agent
ExecStart=docker compose exec -T app python scripts/run_due_searches.py
StandardOutput=journal
StandardError=journal

# Safety: run at most once per interval; kill after 10 min.
TimeoutStopSec=600
UNIT

# ── Generate timer file ────────────────────────────────────────────────
cat > "$DEPLOY_DIR/vacation-scheduler.timer" <<'UNIT'
[Unit]
Description=Run vacation scheduled searches every hour (Phase 5B)

[Timer]
OnBootSec=5min
OnUnitActiveSec=60min
AccuracySec=1min
Persistent=true

[Install]
WantedBy=timers.target
UNIT

if [[ $INSTALL -eq 1 ]]; then
    echo "=== Installing vacation-scheduler timer ==="
    echo ""

    # Copy unit files to systemd user directory (or system if root)
    if [[ $(id -u) -eq 0 ]]; then
        TARGET_DIR="/etc/systemd/system"
    else
        TARGET_DIR="$HOME/.config/systemd/user"
        mkdir -p "$TARGET_DIR"
    fi

    cp "$DEPLOY_DIR/vacation-scheduler.service" "$TARGET_DIR/"
    cp "$DEPLOY_DIR/vacation-scheduler.timer" "$TARGET_DIR/"

    echo "Unit files copied to: $TARGET_DIR/"
    echo ""
    echo "Next steps:"
    if [[ $(id -u) -ne 0 ]]; then
        echo "  # Enable user manager (if not already running):"
        echo "  loginctl enable-linger \$(whoami)"
        echo ""
    fi
    echo "  # Reload systemd and enable the timer:"
    echo "  systemctl --user daemon-reload"
    echo "  systemctl --user enable --now vacation-scheduler.timer"
    echo ""
    echo "  # Check status:"
    echo "  systemctl --user status vacation-scheduler.timer"
    echo "  journalctl --user -u vacation-scheduler -f"
else
    echo "=== Vacation Scheduler Timer (dry-run) ==="
    echo ""
    echo "Generated unit files under: $DEPLOY_DIR/"
    echo ""
    echo "--- vacation-scheduler.service ---"
    cat "$DEPLOY_DIR/vacation-scheduler.service"
    echo ""
    echo "--- vacation-scheduler.timer ---"
    cat "$DEPLOY_DIR/vacation-scheduler.timer"
    echo ""
    echo "To install and enable the timer, run:"
    echo "  bash scripts/install_scheduler_timer.sh --install"
    echo ""
    echo "Manual / dry-run commands (for testing):"
    echo "  # Dry-run (no DB changes):"
    echo "  docker compose exec -T app python scripts/run_due_searches.py --dry-run"
    echo ""
    echo "  # Force a specific vacation:"
    echo "  docker compose exec -T app python scripts/run_due_searches.py --vacation-id 5 --force"
    echo ""
    echo "  # Check timer status after install:"
    echo "  systemctl --user status vacation-scheduler.timer"
    echo "  journalctl --user -u vacation-scheduler -f"
fi
