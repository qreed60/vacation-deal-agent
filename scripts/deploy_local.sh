#!/usr/bin/env bash
# deploy_local.sh — Build and start the vacation-deal-agent locally via Docker Compose.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

echo "=== Building vacation-deal-agent image ==="
docker compose build app

echo ""
echo "=== Starting vacation-deal-agent ==="
docker compose up -d app

echo ""
echo "Waiting for health check..."
for i in $(seq 1 10); do
    if curl -sf http://127.0.0.1:8095/health >/dev/null 2>&1; then
        echo "App is healthy!"
        exit 0
    fi
    sleep 2
done

echo "Warning: health check did not pass within 20 seconds."
docker compose logs --tail=30 app
