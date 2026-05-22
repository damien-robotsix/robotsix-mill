#!/usr/bin/env bash
# Pull the latest robotsix-mill main and rebuild/restart the container,
# but only when origin has new commits AND the mill is idle — so a
# rebuild never kills an in-flight audit/implement/etc. run mid-way
# (wasted tokens). Meant to run from cron every 30 minutes.
#
# Dev-environment helper. Lives in the repo (dev/) but its runtime
# files (log, deployed-SHA marker) are written to the repo's PARENT
# directory so they never dirty the working tree.
#
# Install (cron, every 30 min):
#   */30 * * * * /path/to/robotsix-mill/dev/mill-autoupdate.sh
set -uo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Self-locating: REPO is the parent of this script's dev/ folder.
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
REPO="$(dirname "$SCRIPT_DIR")"
STATE_DIR="$(dirname "$REPO")"          # runtime files live outside the repo
LOG="$STATE_DIR/mill-autoupdate.log"
DEPLOYED_FILE="$STATE_DIR/.mill-deployed-sha"
LOCK="/tmp/mill-autoupdate.lock"
API="http://localhost:8077"

PRE_BUILD_WAIT=1200   # max secs to wait for idle before starting a rebuild
POST_BUILD_WAIT=300   # max secs to wait again after build, before recreate
POLL_INTERVAL=90      # secs between idle checks

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >>"$LOG"; }

# Skip if a previous run is still going (a long idle-wait can outlast
# the 30-min cron interval).
exec 9>"$LOCK"
if ! flock -n 9; then
  log "another run in progress — skipping"
  exit 0
fi

cd "$REPO" || { log "ERROR: cannot cd to $REPO"; exit 1; }

# Never clobber manual WIP — bail on uncommitted TRACKED changes.
# `.env` is excluded: it is tracked (a committed config template) but
# the local copy legitimately holds the user's own config/secrets, so
# it is effectively always "modified". It is backed up and preserved
# specially around the merge below. Untracked files are ignored (they
# don't block a fast-forward).
if [ -n "$(git status --porcelain --untracked-files=no | grep -v '^.. \.env$')" ]; then
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
  log "container already on ${remote:0:7} — nothing to do"
  exit 0
fi

# mill_busy: exit 0 if the mill is doing expensive (token-burning) work
# — a periodic pass (audit/health/agent-check/survey/deep-review) is
# running, or a ticket is in an active LLM stage. Dep-gated tickets are
# parked (not computing) and don't count. API unreachable => not busy.
mill_busy() {
  python3 - <<'PY'
import json, sys, urllib.request

API = "http://localhost:8077"

def get(path):
    try:
        with urllib.request.urlopen(API + path, timeout=10) as r:
            return json.load(r)
    except Exception:
        return None

runs = get("/runs")
if runs and any(r.get("status") == "running" for r in runs):
    sys.exit(0)  # a periodic pass is in flight

# refine=draft, implement=ready, retrospect=done, plus the rebase /
# ci-fix agent states. deliver/in_review are git-only (cheap) so skip.
BUSY = {"draft", "ready", "done", "rebasing", "fixing_ci"}
tickets = get("/tickets")
if tickets and any(
    t.get("state") in BUSY and not t.get("unmet_deps") for t in tickets
):
    sys.exit(0)  # a ticket stage is being worked

sys.exit(1)  # idle (or API unreachable)
PY
}

# wait_until_idle <max_seconds>: poll until the mill is idle. Returns 0
# when idle, 1 if still busy after the cap.
wait_until_idle() {
  local cap=$1 waited=0
  while mill_busy; do
    if [ "$waited" -ge "$cap" ]; then
      return 1
    fi
    log "mill busy (audit/stage running) — waiting ${POLL_INTERVAL}s (waited ${waited}s)"
    sleep "$POLL_INTERVAL"
    waited=$((waited + POLL_INTERVAL))
  done
  return 0
}

dep_short=${deployed:0:7}
[ -z "$dep_short" ] && dep_short="(first run)"
log "new commits on origin/main ($dep_short -> ${remote:0:7}):"
git --no-pager log --oneline "${deployed:-HEAD}..$remote" 2>/dev/null \
  | sed 's/^/    /' >>"$LOG"

if ! wait_until_idle "$PRE_BUILD_WAIT"; then
  log "mill still busy after ${PRE_BUILD_WAIT}s — deferring update to next run"
  exit 0
fi

# Protect the user's .env across the merge. .env is tracked, but the
# local copy holds the user's real config/secrets and must survive the
# pull. If origin/main changes .env, a plain `git merge --ff-only`
# would abort ("local changes would be overwritten"). So: snapshot the
# current .env, detach it from the merge, then restore it verbatim.
# Origin's .env changes are NOT auto-merged — the backup lets the user
# reconcile any new keys by hand.
env_restore=""
if [ -f .env ] && ! git diff --quiet HEAD origin/main -- .env; then
  env_restore="$STATE_DIR/.env.autoupdate-bak-$(date +%Y%m%dT%H%M%S)"
  cp .env "$env_restore"
  git checkout --quiet -- .env   # clean .env so the fast-forward applies
  log "origin/main changes .env — saved current .env to $env_restore"
fi

if ! git merge --ff-only origin/main >>"$LOG" 2>&1; then
  log "ERROR: ff-only merge failed (local diverged from origin/main) — skipping"
  [ -n "$env_restore" ] && cp "$env_restore" .env
  exit 1
fi

if [ -n "$env_restore" ]; then
  cp "$env_restore" .env         # restore the user's .env verbatim
  log "restored your .env — review $env_restore vs the new committed .env for new keys"
fi

export DOCKER_GID
DOCKER_GID=$(getent group docker | cut -d: -f3)

log "mill idle — building image for ${remote:0:7}"
if ! docker compose build mill >>"$LOG" 2>&1; then
  log "ERROR: docker compose build failed"
  exit 1
fi

# A pass/stage may have started during the build — wait again so the
# container recreate doesn't interrupt it. The image is already built,
# so if this times out the next cron run just recreates (cheap).
if ! wait_until_idle "$POST_BUILD_WAIT"; then
  log "mill became busy during build — deferring container recreate to next run"
  exit 0
fi

if docker compose up -d mill >>"$LOG" 2>&1; then
  echo "$remote" >"$DEPLOYED_FILE"
  log "rebuild + restart OK — container now on ${remote:0:7}"
else
  log "ERROR: docker compose up failed"
  exit 1
fi
