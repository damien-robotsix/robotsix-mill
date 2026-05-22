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

## See also

- [README.md](../README.md) — project overview and quickstart
- [docs/docker-architecture.md](docker-architecture.md) — container topology
