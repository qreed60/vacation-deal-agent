#!/usr/bin/env bash
# update_deploy.sh — Pull latest code, rebuild, and restart the deployment.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

echo "=== Stopping current deployment ==="
docker compose down app 2>/dev/null || true

echo ""
echo "=== Pulling latest code (main) ==="
git pull origin main

echo ""
echo "=== Rebuilding and restarting ==="
docker compose build app
docker compose up -d app

echo ""
echo "Waiting for health check..."
for i in $(seq 1 10); do
    if curl -sf http://127.0.0.1:8095/health >/dev/null 2>&1; then
        echo "App is healthy after update!"
        exit 0
    fi
    sleep 2
done

echo "Warning: health check did not pass within 20 seconds."
docker compose logs --tail=30 app
