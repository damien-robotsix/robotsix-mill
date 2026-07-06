# robotsix-mill

Self-contained, LLM-driven ticket solver. Tickets go in one end, merge
requests come out the other. **No forge dependency for orchestration**
and **no scheduler** — emit a ticket and an agent takes it in charge
immediately. The only time it touches GitHub/GitLab is the final
*deliver* step.

**Status:** Full pipeline runs end-to-end.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

This repo follows the [robotsix stack standards](https://github.com/damien-robotsix/robotsix-standards).

## What is robotsix-mill?

robotsix-mill is a **self-contained, LLM-driven ticket-to-merge-request
pipeline**. It is not a CI plugin, a scheduler, or a webhook handler —
the mill *is* the orchestrator. Emit a ticket and it runs the full
autonomous pipeline: refine the spec → wait for human approval →
implement the change → deliver the merge request → merge once CI is green.

**Core design principles**

- **Self-contained.** No forge webhooks, no CI plugins, no external
  scheduler. The mill polls its own SQLite-backed task queue and drives
  the pipeline from end to end.
- **Autonomous pipeline.** Each ticket proceeds through refine →
  approve → implement → deliver → merge, with a human gate after
  refine. Everything after approval runs hands-off.
- **SQLite management plane.** All ticket state, run logs, and cost
  tracking live in a single SQLite database — zero external DB
  dependencies.
- **Containerized agents.** Every agent runs in a disposable Docker
  container (`--network none`, non-root, read-only rootfs). The host
  filesystem is protected by path confinement.

**Scope and tone**

This is a solo/hobby project — no SLAs, no enterprise ceremony, no
compliance theatre. It is provided as-is, built for a single developer
and their AI assistant. See [SECURITY.md](SECURITY.md) for the
pragmatic security stance.

## Configuration

Settings are managed through a YAML pipeline (see
[docs/configuration.md](docs/configuration.md) for full details):

- **`config/config.yaml`** — THE single config file (gitignored): every
  non-secret knob plus a top-level `secrets:` block (API keys, tokens).
- **`config/config.example.yaml`** — committed template (safe defaults +
  `SECRET` sentinel placeholders); the source of truth for every
  configurable knob.
- **Environment variables** — any `MILL_*` variable overrides the
  YAML value (e.g. `MILL_MODEL=anthropic/claude-sonnet-4`).
- **`repos:` key in `config/config.yaml`** — per-repo board config
  (a standalone `config/repos.yaml` is no longer read). Example
  entries at `config/repos.example.yaml`.

The loading order is: `config/config.yaml` (else the committed
`config/config.example.yaml`) → environment variables (highest). The
loader falls back to the committed example when `config.yaml` is absent.

## Getting started

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) (agents run in disposable containers).
- Python 3.14 (for local dev; not needed if using Docker exclusively).

### 1. Clone and configure

```sh
git clone https://github.com/damien-robotsix/robotsix-mill.git
cd robotsix-mill
cp config/config.example.yaml config/config.yaml         # set secrets.openrouter_api_key + any overrides
# then add a `repos:` block to config/config.yaml (example entries
# in config/repos.example.yaml)
```

> **Note:** `board_id` is mandatory — every ticket must belong to a repo
> configured under the `repos:` key of `config/config.yaml`. There is no
> longer a board-less default. For single-repo deployments, configure
> exactly one repo.

### 2. Start the server

**Docker (recommended):**

```sh
docker compose up -d --build
```

Open `http://localhost:8077` — the ticket board is the primary interface.

**Local dev (hot-reload):**

```sh
make install                    # venv + editable install
make dev                        # hot-reload on http://127.0.0.1:8077
                                # (use --repo-id for single-repo mode)
```

### 3. Create your first ticket

```sh
# Docker:
docker compose exec mill robotsix-mill ticket new --title "Add X" --description-file -

# Local:
.venv/bin/robotsix-mill ticket new --title "Add X" --description-file -
```

Attach screenshots to a ticket for the refine agent to review. Via the CLI (the flag is repeatable):

```sh
robotsix-mill ticket new --title "Layout is broken" \
  --description-file issue.md \
  --screenshot error.png \
  --screenshot layout.png
```

Or via the web board: open the board, click **New Ticket**, and use the **Screenshot** file input to attach an image directly in the modal. Both paths support PNG, JPEG, GIF, and WebP formats. Each screenshot is limited to 10 MiB. If a screenshot upload fails, the modal shows a clear error message with options to retry or skip and keep the created ticket.

To create an epic instead of a task, use `robotsix-mill epic new`:

```sh
# Docker:
docker compose exec mill robotsix-mill epic new --title "Refactor auth" --description-file epics/auth.md

# Local:
.venv/bin/robotsix-mill epic new --title "Refactor auth" --description-file epics/auth.md
```

The pipeline runs automatically from here. Other useful commands:

```sh
# Docker:
docker compose exec mill robotsix-mill repos list
docker compose exec mill robotsix-mill ticket list
docker compose exec mill robotsix-mill ticket show <id>
docker compose exec mill robotsix-mill ticket approve <id>
docker compose exec mill robotsix-mill audit
docker compose exec mill robotsix-mill trace-health
docker compose exec mill robotsix-mill copy-paste
docker compose exec mill robotsix-mill forge-parity
docker compose exec mill robotsix-mill cost-reconciliation

# Local:
.venv/bin/robotsix-mill ticket list
.venv/bin/robotsix-mill ticket show <id>
.venv/bin/robotsix-mill ticket approve <id>
.venv/bin/robotsix-mill audit
.venv/bin/robotsix-mill trace-health
.venv/bin/robotsix-mill copy-paste
.venv/bin/robotsix-mill forge-parity
make test
```

Running the pipeline needs Docker (agents run in disposable containers);
`make test` works without it.

Configuration loading order, multi-repo mode, and the full settings
reference are covered in [docs/configuration.md](docs/configuration.md).

## Documentation

- [docs/configuration.md](docs/configuration.md) — Complete configuration reference (YAML schema, loading order, secrets)
- [docs/deployment.md](docs/deployment.md) — Continuous deployment via GitHub Actions + Watchtower
- [docs/docker-architecture.md](docs/docker-architecture.md) — Container topology & conceptual architecture
- [docs/github-app.md](docs/github-app.md) — Delivery identity setup (PAT or GitHub App bot)
- [docs/inquiry-to-task.md](docs/inquiry-to-task.md) — Convert an answered inquiry into an actionable task ticket
- [docs/sandbox/security.md](docs/sandbox/security.md) — Security model
- [docs/agents/index.md](docs/agents/index.md) — Full agent catalog
- [docs/board-operations.md](docs/board-operations.md) — Board UI and automated column transitions
- [docs/agents/agent-yaml-schema.md](docs/agents/agent-yaml-schema.md) — Field reference for `agent_definitions/*.yaml` files
- [docs/expert-yaml-schema.md](docs/expert-yaml-schema.md) — Field reference for `expert_definitions/*.yaml` files
- [docs/approval-gate.md](docs/approval-gate.md) — Human approval gate after refine
- [docs/agents/md-candidates.md](docs/agents/md-candidates.md) — Review and validate AGENT.md rule proposals from retrospect agent
- [docs/core/dedup-guard.md](docs/core/dedup-guard.md) — Pre-refine duplicate / already-done check
- [docs/epic-dedup.md](docs/epic-dedup.md) — Advisory pre-filing dedup for epic-decomposition children
- [docs/merge-stage.md](docs/merge-stage.md) — Gate-check, auto-rebase, and auto-fix for merge-ready PRs
- [docs/agents/audit-agent.md](docs/agents/audit-agent.md) — Meta-audit agent for quality/security coverage gaps
- [docs/blocked-ticket-recovery.md](docs/blocked-ticket-recovery.md) — Recovering from BLOCKED tickets
- [docs/retrospect-memory.md](docs/retrospect-memory.md) — Retrospect agent's Markdown memory ledger
- [docs/trace-health.md](docs/trace-health.md) — Deterministic check for unsessioned Langfuse traces
- [docs/cost-and-resilience.md](docs/cost-and-resilience.md) — Per-ticket cost tracking & cost controls
- [docs/notify/notifications.md](docs/notify/notifications.md) — ntfy.sh push notifications for human-attention states
- [docs/core/ticket-provenance.md](docs/core/ticket-provenance.md) — How `source` tracks which actor created each ticket
- [docs/core/workspace-cleanup.md](docs/core/workspace-cleanup.md) — Automatic clone pruning on ticket close
- [docs/ci-policy.md](docs/ci-policy.md) — CI gate-or-remove policy & checklist for new checks
- [docs/design/forge-architecture.md](docs/design/forge-architecture.md) — Forge abstraction design: GitHub + GitLab adapters, auth, and extension points

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

robotsix-mill is licensed under the [MIT License](LICENSE).
Copyright (c) 2026 Damien Robotsix. See [LICENSE](LICENSE) for the full text.
