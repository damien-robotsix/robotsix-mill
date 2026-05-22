# robotsix-mill

Self-contained, LLM-driven ticket solver. Tickets go in one end, merge
requests come out the other. **No forge dependency for orchestration**
and **no scheduler** — emit a ticket and an agent takes it in charge
immediately. The only time it touches GitHub/GitLab is the final
*deliver* step.

**Status:** Full pipeline runs end-to-end.

## Architecture — two planes

**Management plane (smart, DB-backed).** A single service in the
container owns a **SQLite** DB (via SQLModel): ticket metadata, state,
history, queue. It exposes an **HTTP API** (FastAPI) — the CLI is a thin
client, and a future web frontend uses the same API.

**Work plane (filesystem, agent-owned).** Each ticket gets a workspace
dir on the volume. `description.md` is **file-canonical** (agents edit
it directly); the DB row only holds the pointer + a content hash.

```
/data/
  mill.db                       # management plane (SQLite)
  workspaces/<ticket-id>/
    description.md              # canonical body (agent-editable)
    artifacts/                 # per-stage output
    repo/                      # git clone (removed on close by default)
  retrospect_memory.md          # agent-maintained issue ledger
  audit_memory.md              # audit agent's gap ledger

emit ticket ─▶ API inserts row + enqueues ─▶ worker chains stages
  draft ─refine▶ awaiting_approval ─approve▶ ready ─implement▶ deliverable
        ─deliver▶ in_review ─(PR merged; merge-poll)▶ done ─retrospect▶ closed
  in_review = PR open (the PR is the review); merge poll flips it.
  retrospect audits the run + Langfuse and may spawn an improvement draft.
  closed = terminal. errored = a stage threw; blocked = needs a human
  (both resumable: a human transition re-enqueues).

  BLOCKED recovery (no raw-DB editing ever needed):
    BLOCKED ─resume-blocked▶ <blocked_from>   (re-run only the failed stage)
    BLOCKED → READY | DRAFT                    (manual override: full re-run)
  awaiting_approval is a human gate (configurable via MILL_REQUIRE_APPROVAL).
```

- **Engine:** `pydantic-ai` over OpenRouter.
- **Event-driven:** ticket emission / state change enqueues; an
  in-process **pool** (`MILL_MAX_CONCURRENCY`, default 4) picks it up at
  once and **chains** stages until a terminal state. Distinct tickets
  run in parallel (one ticket's stages stay ordered; a dedupe set stops
  the same ticket running twice). No cron, no polling (except merge
  check).
- **Delivery:** pluggable forge adapter (GitHub / GitLab), invoked only
  by the `deliver` stage.
- **Tracing:** optional Langfuse; a no-op unless `LANGFUSE_*` is set.

## Quickstart

```sh
cp .env.example .env      # set OPENROUTER_API_KEY (+ FORGE_* later)
docker compose up -d --build
```

**Ticket board:** http://localhost:8077 — a live Kanban (one column per
state, click a card for history + description, auto-refreshes). Each
card shows the cumulative LLM spend for that ticket (e.g. `$0.0943`).

```sh
docker compose exec mill robotsix-mill ticket new --title "Add X" --description-file -
docker compose exec mill robotsix-mill ticket list
docker compose exec mill robotsix-mill ticket show <id>
docker compose exec mill robotsix-mill ticket approve <id>
docker compose exec mill robotsix-mill ticket resume-blocked <id>
docker compose exec mill robotsix-mill audit
docker compose exec mill robotsix-mill trace-health
```

### Local development (no Docker)

```sh
cp .env.example .env        # set OPENROUTER_API_KEY
make install                # venv + editable install (.[dev,tracing])
.venv/bin/pre-commit install
make dev                    # service with hot-reload on http://127.0.0.1:8077

# in another shell — the CLI is an HTTP client to that service:
.venv/bin/robotsix-mill ticket new --title "Add X" --description-file -
.venv/bin/robotsix-mill ticket list
.venv/bin/robotsix-mill ticket approve <id>
.venv/bin/robotsix-mill ticket resume-blocked <id>
.venv/bin/robotsix-mill audit
.venv/bin/robotsix-mill trace-health
make test
```

`make serve` runs without reload (Docker mode); `make docker` builds
the container. Running the pipeline always needs Docker (agent commands
run in disposable containers — see [docs/security.md](docs/security.md)).
The unit test suite fakes the sandbox seam, so `make test` works without
Docker.

## Documentation

| Doc | Description |
|---|---|
| [docs/agents.md](docs/agents.md) | Full agent catalog — pipeline agents, periodic agents, sub-agents, infrastructure |
| [docs/configuration.md](docs/configuration.md) | Complete env-var reference (all `MILL_*` vars + forge/tracing/notifications) |
| [docs/ticket-provenance.md](docs/ticket-provenance.md) | How `source` tracks which actor created each ticket |
| [docs/cost-and-resilience.md](docs/cost-and-resilience.md) | Per-ticket cost (on-demand Langfuse read) and cost controls |
| [docs/deployment.md](docs/deployment.md) | Continuous deployment via GitHub Actions + Watchtower |
| [docs/approval-gate.md](docs/approval-gate.md) | Human approval gate after refine |
| [docs/dedup-guard.md](docs/dedup-guard.md) | Pre-refine duplicate / already-done check |
| [docs/blocked-ticket-recovery.md](docs/blocked-ticket-recovery.md) | Recovering from BLOCKED tickets without raw DB edits |
| [docs/notifications.md](docs/notifications.md) | ntfy.sh push notifications on human-attention states |
| [docs/merge-stage.md](docs/merge-stage.md) | Auto-rebase of stale PRs + auto-fix of failing CI |
| [docs/retrospect-memory.md](docs/retrospect-memory.md) | Retrospect agent's Markdown memory ledger |
| [docs/audit-agent.md](docs/audit-agent.md) | Meta-audit agent for quality/security coverage gaps |
| [docs/trace-health.md](docs/trace-health.md) | Deterministic check for unsessioned Langfuse traces |
| [docs/workspace-cleanup.md](docs/workspace-cleanup.md) | Automatic clone pruning on ticket close |
| [docs/security.md](docs/security.md) | Security model — sandbox isolation, path confinement |
| [docs/docker-architecture.md](docs/docker-architecture.md) | Container topology — mill vs. sibling sandbox |
| [docs/github-app.md](docs/github-app.md) | Delivery identity setup (PAT or GitHub App bot) |

## Key configuration

The most commonly-set env vars. For the full reference (86+ vars), see
[docs/configuration.md](docs/configuration.md).

| Env var | Default | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | (required) | OpenRouter API key |
| `FORGE_KIND` | `none` | Forge platform: `github`, `gitlab`, or `none` |
| `FORGE_REMOTE_URL` | — | Remote URL for clone + push |
| `FORGE_TOKEN` | — | PAT for forge authentication |
| `MILL_REQUIRE_APPROVAL` | `true` | Pause after refine for human approval |
| `LANGFUSE_BASE_URL` | — | Langfuse base URL (tracing, optional) |
| `LANGFUSE_PUBLIC_KEY` | — | Langfuse public key |
| `LANGFUSE_SECRET_KEY` | — | Langfuse secret key |
| `NTFY_URL` | — | ntfy.sh topic URL for notifications |
| `MILL_MAX_CONCURRENCY` | `4` | Max parallel tickets |
| `MILL_SANDBOX_IMAGE` | `python:3.14-slim` | Docker image for sandbox containers |
| `DOCKER_GID` | — | Host docker group ID (for socket access) |
