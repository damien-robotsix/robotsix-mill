#!/usr/bin/env bash
# Pull the latest robotsix-auto-mail main and rebuild/restart the
# compose stack, but only when origin has new commits. Meant to run
# from cron every 30 minutes. Mirrors robotsix-mill's
# dev/mill-autoupdate.sh.
#
# Unlike the mill helper there is NO idle-wait: auto-mail has no
# equivalent "expensive LLM run in flight" API, and its email
# processing is restart-tolerant (the app retries), so a brief
# recreate is safe. If that ever changes, add a busy-check the way
# mill-autoupdate.sh polls localhost:8077.
#
# Dev-environment helper. Lives in the repo (dev/) but its runtime
# files (log, deployed-SHA marker, lock) are written to the repo's
# PARENT directory so they never dirty the working tree.
#
# Install (cron, every 30 min, offset from mill so they don't both
# build at once):
#   15,45 * * * * /path/to/robotsix-auto-mail/dev/auto-mail-autoupdate.sh
set -uo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Self-locating: REPO is the parent of this script's dev/ folder.
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
REPO="$(dirname "$SCRIPT_DIR")"
STATE_DIR="$(dirname "$REPO")"          # runtime files live outside the repo
LOG="$STATE_DIR/auto-mail-autoupdate.log"
DEPLOYED_FILE="$STATE_DIR/.auto-mail-deployed-sha"
LOCK="/tmp/auto-mail-autoupdate.lock"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >>"$LOG"; }

# Skip if a previous run is still going.
exec 9>"$LOCK"
if ! flock -n 9; then
  log "another run in progress — skipping"
  exit 0
fi

cd "$REPO" || { log "ERROR: cannot cd to $REPO"; exit 1; }

# Pin the deploy tree to main. This script ff-merges origin/main into
# the CURRENTLY checked-out branch — so if the tree is parked on a
# feature branch that diverged from main, every ff-merge aborts and
# deploys silently stop. Switch back to main first. Refuse to auto-switch
# if there are uncommitted tracked changes (don't clobber manual WIP).
current_branch=$(git symbolic-ref --short -q HEAD || echo "(detached)")
if [ "$current_branch" != "main" ]; then
  if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    log "on '$current_branch' with uncommitted changes — refusing to auto-switch to main; skipping"
    exit 0
  fi
  log "deploy tree on '$current_branch', not main — switching to main"
  if ! git checkout main >>"$LOG" 2>&1; then
    log "ERROR: failed to checkout main — skipping"
    exit 1
  fi
fi

# Never clobber manual WIP — bail on uncommitted TRACKED changes.
# (.env is untracked here, so it is ignored automatically; untracked
# files don't block a fast-forward.)
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  log "working tree has uncommitted changes — skipping pull/rebuild"
  exit 0
fi

if ! git fetch origin main >>"$LOG" 2>&1; then
  log "ERROR: git fetch failed (SSH auth / network?) — skipping"
  exit 1
fi

remote=$(git rev-parse origin/main)
deployed=$(cat "$DEPLOYED_FILE" 2>/dev/null || true)
if [ "$deployed" = "$remote" ]; then
  log "stack already on ${remote:0:7} — nothing to do"
  exit 0
fi

dep_short=${deployed:0:7}
[ -z "$dep_short" ] && dep_short="(first run)"
log "new commits on origin/main ($dep_short -> ${remote:0:7}):"
git --no-pager log --oneline "${deployed:-HEAD}..$remote" 2>/dev/null \
  | sed 's/^/    /' >>"$LOG"

if ! git merge --ff-only origin/main >>"$LOG" 2>&1; then
  log "ERROR: ff-only merge failed (local diverged from origin/main) — skipping"
  exit 1
fi

export DOCKER_GID
DOCKER_GID=$(getent group docker | cut -d: -f3)

log "building images for ${remote:0:7}"
if ! docker compose build >>"$LOG" 2>&1; then
  log "ERROR: docker compose build failed"
  exit 1
fi

if docker compose up -d >>"$LOG" 2>&1; then
  echo "$remote" >"$DEPLOYED_FILE"
  log "rebuild + restart OK — stack now on ${remote:0:7}"
else
  log "ERROR: docker compose up failed"
  exit 1
fi
