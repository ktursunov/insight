#!/usr/bin/env bash
# Insight platform — sample-data seed wrapper.
#
# Spins up the compose `seed-sample` service one-shot, passes its args
# through to seed.py. Subcommands:
#
#   ./dev-compose-seed-sample.sh                 # default: all
#   ./dev-compose-seed-sample.sh identity        # MariaDB only
#   ./dev-compose-seed-sample.sh silver          # ClickHouse only (Phase 2)
#   ./dev-compose-seed-sample.sh all
#
# Reads .env.compose for VITE_DEV_USER_EMAIL, TENANT_DEFAULT_ID, etc.
# Requires the stack to be running (./dev-compose-up.sh).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

ENV_FILE=".env.compose"
[[ -f "$ENV_FILE" ]] || ENV_FILE=".env.compose.example"

OVERRIDE="compose/override.generated.yml"
COMPOSE=(docker compose --env-file "$ENV_FILE" -f docker-compose.yml)
[[ -f "$OVERRIDE" ]] && COMPOSE+=(-f "$OVERRIDE")

ARGS=("$@")
[[ ${#ARGS[@]} -eq 0 ]] && ARGS=("all")

exec "${COMPOSE[@]}" --profile seed run --rm seed-sample "${ARGS[@]}"
