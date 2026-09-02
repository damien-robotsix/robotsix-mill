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

# alembic has no ``--sqlalchemy.url`` flag (it errors with
# "unrecognized arguments"); point a temp copy of alembic.ini at the
# per-board DB instead, mirroring the ``make check-migrations`` recipe.
TMP_INI="$(mktemp "${TMPDIR:-/tmp}/alembic-migrate.XXXXXX.ini")"
trap 'rm -f "$TMP_INI"' EXIT
cp "$REPO_ROOT/alembic.ini" "$TMP_INI"
sed -i "s|^sqlalchemy\.url = .*|sqlalchemy.url = ${DB_URL}|" "$TMP_INI"

uv run alembic -c "$TMP_INI" "${ACTION}" head
