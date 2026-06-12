#!/usr/bin/env bash
# Thin wrapper — delegates to the shared ``robotsix-autoupdate`` CLI from
# the ``robotsix-mill`` package (installed via ``uv sync``).
#
# Pull the latest robotsix-mill main and rebuild/restart the container,
# but only when origin has new commits AND the mill is idle — so a
# rebuild never kills an in-flight audit/implement/etc. run mid-way
# (wasted tokens). Meant to run from cron every 30 minutes.
#
# Install (cron, every 30 min):
#   */30 * * * * /path/to/robotsix-mill/dev/mill-autoupdate.sh
set -uo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Self-locating: REPO is the parent of this script's dev/ folder.
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
REPO="$(dirname "$SCRIPT_DIR")"
STATE_DIR="$(dirname "$REPO")"          # runtime files live outside the repo

# Bake the just-deployed commit's short SHA into the image as the
# per-deploy static-asset cache-busting token (compose build.args ->
# Dockerfile ARG/ENV -> asset_version()). Same commit => same token.
export DOCKER_GID
export MILL_BUILD_SHA
DOCKER_GID=$(getent group docker | cut -d: -f3)
MILL_BUILD_SHA=$(git -C "$REPO" rev-parse --short HEAD)

exec robotsix-autoupdate \
  --repo "$REPO" \
  --state-dir "$STATE_DIR" \
  --state-prefix mill-autoupdate \
  --service mill \
  --idle-check-cmd "python3 $REPO/dev/mill-idle-check.py" \
  --pre-build-wait "${PRE_BUILD_WAIT:-1200}" \
  --post-build-wait "${POST_BUILD_WAIT:-300}" \
  --poll-interval "${POLL_INTERVAL:-90}" \
  --max-deferrals "${MAX_DEFERRALS:-4}"
