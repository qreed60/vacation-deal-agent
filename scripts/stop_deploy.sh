#!/usr/bin/env bash
# stop_deploy.sh — Stop and remove the app container (keeps data volume intact).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

echo "=== Stopping vacation-deal-agent ==="
docker compose down app

echo ""
echo "Done. Data in ./data is preserved."
