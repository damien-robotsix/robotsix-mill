# Continuous deployment

On every push to `main`, a GitHub Actions workflow
(`.github/workflows/docker-publish.yml`) builds and publishes the
Docker image to Docker Hub as **`robotsix/mill:latest`** (plus a
short-SHA tag for pinning). A [Watchtower](https://containrrr.dev/watchtower/)
sidecar in the compose stack polls for new images and auto-updates the
running `mill` container — no manual rebuilds or restarts needed.

## Required GitHub secrets

| Secret | Purpose |
|---|---|
| `DOCKERHUB_USERNAME` | Docker Hub username for pushing images |
| `DOCKERHUB_TOKEN` | Docker Hub access token (or password) |
| `DEPS_BUMP_TOKEN` | PAT used by `deps-bump.yml` to open the weekly `uv.lock` bump PR so its CI runs (a PR created with the default `GITHUB_TOKEN` triggers no workflows) |

Set these in the repository **Settings → Secrets and variables →
Actions**. The publish workflow fires on push to `main` and on manual
`workflow_dispatch`; it does **not** trigger on pull requests.

## How auto-update works

The `watchtower` service in `docker-compose.yml` polls Docker Hub
every 300 seconds for a new `robotsix/mill:latest` image. It is scoped
via `--label-enable`, so only containers with the label
`com.centurylinklabs.watchtower.enable=true` — i.e., just `mill` — are
updated. When a new image is found, Watchtower pulls it and restarts
the `mill` container in-place, preserving all mounts and configuration.
The `--cleanup` flag removes old images to avoid disk bloat.

## Local development with Docker

The production `docker-compose.yml` pulls `robotsix/mill:latest` from
Docker Hub (no `build:` directive). To build and run the local
Dockerfile instead, copy the provided override file:

```sh
cp docker-compose.override.example.yml docker-compose.override.yml
docker compose up -d --build
```

The override file (`docker-compose.override.yml`) is git-ignored and
adds `build: .` back to the `mill` service. Docker Compose merges the
two files automatically. Omit `--build` to reuse a previously cached
local image.

### Static asset cache-busting via `MILL_BUILD_SHA`

The board's static assets (JS/CSS) carry a per-deploy cache-busting token
appended to their URLs (e.g. `/static/board.js?v=<token>`). The build
argument `MILL_BUILD_SHA` provides this token:

```bash
export MILL_BUILD_SHA="$(git rev-parse --short HEAD)"
docker compose build mill
```

When `MILL_BUILD_SHA` is set, its value (typically a git short SHA) becomes
the token. The autoupdate script (`dev/mill-autoupdate.sh`) does this
automatically. For dev/uvicorn runs without the build argument, a stable
process-start fallback token is used instead (ensuring non-empty tokens and
fresh cache on each restart).

**Why this matters:** UI fixes deployed in a new commit produce different
tokens, so browsers fetch fresh JS/CSS instead of serving stale cached
bundles. Without this, users would see old UI behavior until they
hard-refresh.

## Autoupdate CLI reference

The `robotsix-autoupdate` CLI (invoked by `dev/mill-autoupdate.sh`) is a
stdlib-only orchestrator that handles flock, git fetch, merge, docker
compose build/up, and deployed-SHA recording. Exit codes: **0** (success
or intentional skip), **1** (operational failure).

### Flags

| Flag | Default | Purpose |
|---|---|---|
| `--repo` | `cwd` | Path to the git repository |
| `--state-dir` | parent of `--repo` | Directory for runtime state files |
| `--state-prefix` | `mill-autoupdate` | Prefix for state filenames |
| `--remote` | `origin/main` | Remote ref to fetch and merge, as `<remote>/<branch>` |
| `--service` | `mill` | Docker Compose service name to build and restart |
| `--idle-check-cmd` | unset | Shell command for idle check; unset means skip polling |
| `--ensure-branch` | unset | `git checkout <BRANCH>` before fetch |
| `--pre-build-wait` | `1200` | Max seconds to poll idle before build |
| `--post-build-wait` | `300` | Max seconds to poll idle after build, before `up` |
| `--poll-interval` | `90` | Seconds between idle-check polls |
| `--max-deferrals` | `4` | Consecutive busy-deferral cap before force-deploy |
| `--no-force-deploy` | `false` | Never force-deploy when busy; always defer regardless of `--max-deferrals` |
| `--no-idle-check` | `false` | Skip all idle polling (overrides `--idle-check-cmd` and both wait flags) |

### Environment variable overrides

The bash wrapper (`dev/mill-autoupdate.sh`) reads these environment
variables and passes them as CLI flags:

| Variable | Default | Maps to |
|---|---|---|
| `PRE_BUILD_WAIT` | `1200` | `--pre-build-wait` |
| `POST_BUILD_WAIT` | `300` | `--post-build-wait` |
| `POLL_INTERVAL` | `90` | `--poll-interval` |
| `MAX_DEFERRALS` | `4` | `--max-deferrals` |
| `NO_FORCE_DEPLOY` | unset | `--no-force-deploy` (set to any non-empty value to enable) |

Set these in the crontab or environment before the script runs:

```sh
# In crontab:
NO_FORCE_DEPLOY=1 /path/to/robotsix-mill/dev/mill-autoupdate.sh
```

## Resource limits in `docker-compose.yml`

The `mill` service in `docker-compose.yml` applies two resource limits
to prevent a single runaway agent from exhausting host resources and
triggering an OOM kill (which would interrupt every concurrently-running
periodic agent):

- **`mem_limit: 4g`** — 4 GiB memory ceiling. The mill worker is
  single-process; 4 GiB leaves headroom for concurrent agent
  subprocesses (git, trivy, model CLI) spawned by asyncio tasks.
- **`cpus: 2`** — 2 CPU cores ceiling.

A `nofile` ulimit of **65536** prevents `"OSError: [Errno 24] Too many
open files"` crashes when many workers each spawn git/trivy/agent
subprocesses with their own pipes.

These limits are baked into the production compose file. Override them
in `docker-compose.override.yml` if your host has differing capacity.

## Enable GitHub Pages (one-time setup)

The `.github/workflows/docs.yml` workflow builds the MkDocs site and
pushes it to the `gh-pages` branch automatically on every merge to
`main`. However, GitHub Pages must be enabled **once** on the
repository before GitHub will serve that branch — the workflow alone
is not enough.

Two equivalent ways to enable it:

- **UI:** Repository **Settings → Pages** → Source =
  "Deploy from a branch" → Branch = `gh-pages`, Folder = `/ (root)`
  → **Save**.
- **CLI:**
  ```sh
  gh api -X POST repos/<owner>/<repo>/pages \
    -F 'source[branch]=gh-pages' \
    -F 'source[path]=/'
  ```

Once enabled, the existing latest `gh-pages` commit is served
immediately at `https://<owner>.github.io/<repo>/`.

## See also

- [index.md](index.md) — documentation home
- [docs/docker-architecture.md](docker-architecture.md) — container topology
