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

# Pass the docker group id as a build arg so the container can
# access the host's docker socket with matching group membership.
export DOCKER_GID
DOCKER_GID=$(getent group docker | cut -d: -f3)

# One-time marker migration: the old bash autoupdater recorded the deployed
# SHA as .mill-deployed-sha; the CLI uses .{PREFIX}-deployed-sha. Without
# this copy the first CLI run would see "first run" and redeploy (or
# force-deploy after deferrals) even when already current.
if [ -f "$STATE_DIR/.mill-deployed-sha" ] && [ ! -f "$STATE_DIR/.mill-autoupdate-deployed-sha" ]; then
  cp "$STATE_DIR/.mill-deployed-sha" "$STATE_DIR/.mill-autoupdate-deployed-sha"
fi

# The console script is installed in the repo venv by `uv sync`, not on the
# system PATH this script resets above — under cron a bare name exits 127.
exec "$REPO/.venv/bin/robotsix-autoupdate" \
  --repo "$REPO" \
  --state-dir "$STATE_DIR" \
  --state-prefix mill-autoupdate \
  --service mill \
  --ensure-branch main \
  --idle-check-cmd "python3 $REPO/dev/mill-idle-check.py" \
  --pre-build-wait "${PRE_BUILD_WAIT:-1200}" \
  --post-build-wait "${POST_BUILD_WAIT:-300}" \
  --poll-interval "${POLL_INTERVAL:-90}" \
  --max-deferrals "${MAX_DEFERRALS:-4}" \
  ${NO_FORCE_DEPLOY:+--no-force-deploy}
