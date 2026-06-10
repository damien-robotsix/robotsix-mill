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
