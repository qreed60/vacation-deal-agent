#!/usr/bin/env bash
# logs_deploy.sh — Follow or tail Docker Compose logs for the app service.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

if [[ "${1:-}" == "-f" || "${1:-}" == "--follow" ]]; then
    echo "Following logs (Ctrl+C to stop)..."
    docker compose logs -f --tail=200 app
else
    docker compose logs --tail=100 app
fi
