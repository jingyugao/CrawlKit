#!/usr/bin/env bash
set -euo pipefail

# Scales browserless/chrome replicas using docker compose.
# Usage:
#   ./scale_browserless.sh 4
#   PUBLISHED_PORT=9223 ./scale_browserless.sh       # override gateway port
#   ./scale_browserless.sh                           # prompt for desired count

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.browserless.yml}"

if [[ ! -f "${COMPOSE_FILE}" ]]; then
  echo "Compose file '${COMPOSE_FILE}' not found. Run from repo root or set COMPOSE_FILE." >&2
  exit 1
fi

TARGET_COUNT="${1:-}"
if [[ -z "${TARGET_COUNT}" ]]; then
  read -r -p "Desired number of browserless/chrome instances: " TARGET_COUNT
fi

if [[ ! "${TARGET_COUNT}" =~ ^[0-9]+$ ]]; then
  echo "Please provide a non-negative integer (e.g., 3)." >&2
  exit 1
fi

echo "Scaling browserless/chrome to ${TARGET_COUNT} instance(s)..."
docker compose -f "${COMPOSE_FILE}" up -d --remove-orphans --scale browserless="${TARGET_COUNT}" gateway

echo
echo "Current service state:"
docker compose -f "${COMPOSE_FILE}" ps
echo
echo "Gateway is exposed on host port ${PUBLISHED_PORT:-9222} (set PUBLISHED_PORT to override)."
