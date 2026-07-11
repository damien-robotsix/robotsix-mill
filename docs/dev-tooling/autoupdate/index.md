# Autoupdate

The `autoupdate` module provides a shared CLI for self-updating
robotsix-\* Docker Compose services. It is a **stdlib-only** orchestrator
(no third-party dependencies) that coordinates the full update lifecycle:
lock acquisition, git fetch, idle polling, fast-forward merge, Docker
Compose build/up, and deployed-SHA recording.

## Entry point

```bash
robotsix-autoupdate [OPTIONS]
```

Registered as a `console_scripts` target in `pyproject.toml`.

## CLI flags

| Flag | Default | Purpose |
|---|---|---|
| `--repo` | cwd | Git repository path |
| `--state-dir` | parent of `--repo` | Runtime state file directory |
| `--state-prefix` | `mill-autoupdate` | Prefix for `.log`, `.deployed-sha`, `.deferrals` files |
| `--remote` | `origin/main` | `<remote>/<branch>` to fetch and merge |
| `--service` | `mill` | Docker Compose service name to build and restart |
| `--idle-check-cmd` | none | Shell command; exit 0 = busy, non-0 = idle |
| `--ensure-branch` | none | `git checkout <BRANCH>` before fetch |
| `--pre-build-wait` | `1200` | Maximum seconds polling idle before build |
| `--post-build-wait` | `300` | Maximum seconds polling idle before `docker compose up` |
| `--poll-interval` | `90` | Seconds between idle polls |
| `--max-deferrals` | `4` | Busy-deferral cap before force-deploy |
| `--no-force-deploy` | false | Never force-deploy when busy |
| `--no-idle-check` | false | Skip all idle polling |

Exit codes: **0** (success or intentional skip), **1** (operational failure).

## Flock-based locking

A non-blocking exclusive `flock` on `/tmp/{state-prefix}.lock` prevents
concurrent runs. If the lock is already held, the process logs "another
run in progress — skipping" and exits 0.

## Update lifecycle

Inside the lock, the CLI runs a sequential pipeline:

1. **`--ensure-branch`** — `git checkout <BRANCH>` if requested.
2. **Guard uncommitted changes** — `git status --porcelain --untracked-files=no`;
   blocks the update (exit 1) unless the only dirty entry is `.env`.
3. **Fetch** — `git fetch <remote> <branch>`.
4. **SHA comparison** — reads the remote SHA via `git rev-parse` and
   compares it against the recorded SHA from `.{prefix}-deployed-sha`.
   If identical, exits 0 ("already on SHA — nothing to do").
5. **Log commit range** — `git --no-pager log --oneline <deployed>..<remote>`.
6. **Idle polling (pre-build)** — polls `--idle-check-cmd` at
   `--poll-interval` seconds for up to `--pre-build-wait` seconds.
   Each busy poll increments a deferral counter stored in
   `.{prefix}-deferrals`; if deferrals exceed `--max-deferrals`,
   the update proceeds anyway (force-deploy, unless `--no-force-deploy`
   is set).
7. **`.env` backup** — if the incoming commits touch `.env`, the
   current `.env` is backed up to the state directory and `.env` is
   detached from the index (`git checkout -- .env`) so the merge
   proceeds cleanly.
8. **Fast-forward merge** — `git merge --ff-only <remote>/<branch>`.
   On failure with `--ensure-branch` set, reconciles via
   `git reset --hard <remote>/<branch>`; otherwise exits 1.
9. **`.env` restore** — copies the backed-up `.env` back into the
   repo root.
10. **Build** — `docker compose build --build-arg DOCKER_GID=<gid>
    --build-arg MILL_BUILD_SHA=<short-sha> <service>`.
11. **Idle polling (post-build)** — same polling loop as step 6 but
    bounded by `--post-build-wait`. Deferrals from pre-build carry
    over (shared counter).
12. **`docker compose up -d <service>`**.
13. **Record deployed SHA** — writes the remote SHA to
    `.{prefix}-deployed-sha` in the state directory.
14. **Reset deferral counter** — removes `.{prefix}-deferrals` on
    success.

## Deployed-SHA recording

The file `{state-dir}/.{prefix}-deployed-sha` (e.g.
`.mill-autoupdate-deployed-sha`) stores the most recently deployed
commit SHA. It is written on each successful update and read at the
start of the next run to determine whether new commits exist.

## Bash wrapper (`dev/mill-autoupdate.sh`)

A thin self-locating bash script that:

- Resolves the repo root and state directory relative to its own
  location.
- Exports `DOCKER_GID` from `getent group docker`.
- Performs a one-time migration from an older `.mill-deployed-sha`
  filename to the current `.mill-autoupdate-deployed-sha`.
- Invokes `robotsix-autoupdate` from the repo's venv with the
  standard flags (`--ensure-branch main`, `--service mill`, and the
  idle-check command pointing at `dev/mill-idle-check.py`).
- Allows overrides via environment variables: `PRE_BUILD_WAIT`,
  `POST_BUILD_WAIT`, `POLL_INTERVAL`, `MAX_DEFERRALS`,
  `NO_FORCE_DEPLOY`.

The wrapper is typically invoked by cron every 30 minutes.

## Idle-check script (`dev/mill-idle-check.py`)

A companion Python script that queries the mill's REST API to detect
active work. It checks:

- **`/runs`** — if any run has `status == "running"`, the service is
  busy.
- **`/tickets`** — if any ticket has its state in
  `{draft, ready, done, rebasing, fixing_ci}` and no unmet
  dependencies, the service is busy.

Exit 0 means busy (do NOT restart), exit 1 means idle (safe to
restart).
