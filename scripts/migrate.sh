#!/usr/bin/env bash
# Run Alembic migrations against a per-board SQLite database.
#
# Usage:
#   scripts/migrate.sh [board_id]  # default: run against "default" board
#   scripts/migrate.sh my-repo     # run against ".data/my-repo/mill.db"
#   scripts/migrate.sh --stamp     # stamp (don't upgrade) the default board
#   scripts/migrate.sh --stamp my-repo
#
# Pre-req: ``uv sync`` (or ``make install``) so alembic is available.

set -euo pipefail

BOARD="${1:-default}"
ACTION="upgrade"

if [[ "${1:-}" == "--stamp" ]]; then
    ACTION="stamp"
    BOARD="${2:-default}"
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

DB_PATH="${REPO_ROOT}/.data/${BOARD}/mill.db"
DB_URL="sqlite:///${DB_PATH}"

cd "$REPO_ROOT"

echo "==> Board:  ${BOARD}"
echo "==> DB:     ${DB_PATH}"
echo "==> Action: alembic ${ACTION} head"

exec uv run alembic --sqlalchemy.url="$DB_URL" "${ACTION}" head
