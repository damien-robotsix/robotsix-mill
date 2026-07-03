#!/usr/bin/env bash
# API-level board smoke check (Tier 1).
#
# Boots the mill server against a throwaway temp data dir, waits for it
# to become healthy, then asserts HTTP 200 + key markers on the three
# canonical board routes (GET /, GET /tickets, GET /board/cards). Reds
# the gate (exit non-zero) on any failed assertion.
#
# Kept SEPARATE from Tier 2's scripts/smoke_board_browser.sh (headless
# Chromium DOM/console verification) by design — this is the cheap
# process-startup + HTTP endpoint layer that needs no browser. ``curl``
# is already installed in the sandbox image; no new dependency.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# Throwaway data dir so mill.db is created under a disposable location
# (see core/db.py::_db_path / init_db). Defaults for host/port.
TMP_DATA_DIR="$(mktemp -d)"
export MILL_DATA_DIR="${TMP_DATA_DIR}"
export MILL_API_HOST="127.0.0.1"
# Explicit non-default port so the smoke server never collides with a
# real mill instance already bound to the default 8077.
export MILL_API_PORT="8901"
BASE_URL="http://${MILL_API_HOST}:${MILL_API_PORT}"

# `robotsix-mill serve` needs a repos registry, but config/repos.yaml is a
# host-mounted secret absent from the clone. Synthesize a minimal one in
# the throwaway dir and point MILL_REPOS_FILE at it so the server boots
# self-contained (lifespan init_db's this board; no forge/langfuse calls
# are made just to render the board + read tickets).
REPOS_FILE="${TMP_DATA_DIR}/repos.yaml"
cat >"${REPOS_FILE}" <<'YAML'
repos:
  smoke-repo:
    board_id: "smoke-board"
    langfuse:
      project_name: "smoke"
      public_key: "pk-smoke"
      secret_key: "sk-smoke"
YAML
export MILL_REPOS_FILE="${REPOS_FILE}"

SERVER_PID=""

cleanup() {
  if [[ -n "${SERVER_PID}" ]] && kill -0 "${SERVER_PID}" 2>/dev/null; then
    kill "${SERVER_PID}" 2>/dev/null || true
    wait "${SERVER_PID}" 2>/dev/null || true
  fi
  rm -rf "${TMP_DATA_DIR}"
}
trap cleanup EXIT

# Start the server in the background.
robotsix-mill serve &
SERVER_PID=$!

# Poll /health until ready (bounded). Fail loudly if it never comes up.
ready=0
for _ in $(seq 1 60); do
  if ! kill -0 "${SERVER_PID}" 2>/dev/null; then
    echo "smoke: server process exited before becoming ready" >&2
    exit 1
  fi
  if curl -fsS "${BASE_URL}/health" 2>/dev/null | grep -q '"status":"alive"'; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "${ready}" -ne 1 ]]; then
  echo "smoke: server never became healthy at ${BASE_URL}/health" >&2
  exit 1
fi

fail() {
  echo "smoke: assertion failed — $1" >&2
  exit 1
}

# GET / — board HTML shell.
root_html="$(curl -fsS "${BASE_URL}/" )" || fail "GET / did not return 200"
grep -q 'id="board"' <<<"${root_html}" || fail "GET / missing id=\"board\""
grep -q 'board-column' <<<"${root_html}" || fail "GET / missing board-column"
grep -q 'robotsix-mill' <<<"${root_html}" || fail "GET / missing robotsix-mill"

# GET /tickets — JSON array.
tickets_body="$(curl -fsS "${BASE_URL}/tickets")" || fail "GET /tickets did not return 200"
grep -q '^\[' <<<"${tickets_body}" || fail "GET /tickets is not a JSON array"

# GET /board/cards — JSON array.
cards_body="$(curl -fsS "${BASE_URL}/board/cards")" || fail "GET /board/cards did not return 200"
grep -q '^\[' <<<"${cards_body}" || fail "GET /board/cards is not a JSON array"

echo "smoke: board API smoke passed"
