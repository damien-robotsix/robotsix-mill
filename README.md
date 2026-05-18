# robotsix-mill

Self-contained, LLM-driven ticket solver. Tickets go in one end, merge
requests come out the other. **No forge dependency for orchestration**
and **no scheduler** — emit a ticket and an agent takes it in charge
immediately. The only time it touches GitHub/GitLab is the final
*deliver* step.

**Status:** scaffolding. The management plane (DB, API, event-driven
worker, state machine) works and is tested. The pipeline **stages are
stubs**, implemented one by one.

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
  awaiting_approval is a human gate (configurable via MILL_REQUIRE_APPROVAL).
```

- **Engine:** `pydantic-ai` over OpenRouter.
- **Event-driven:** ticket emission / state change enqueues; an
  in-process **pool** (`MILL_MAX_CONCURRENCY`, default 4) picks it up at
  once and **chains** stages until a terminal state or a stub. Distinct
  tickets run in parallel (one ticket's stages stay ordered; a dedupe
  set stops the same ticket running twice). No cron, no polling (except
  merge check).
- **Delivery:** pluggable forge adapter (GitHub / GitLab), invoked only
  by the `deliver` stage.
- **Tracing:** optional Langfuse; a no-op unless `LANGFUSE_*` is set.
- **Retrospect memory:** the retrospect agent maintains a Markdown ledger
  (`MILL_RETROSPECT_MEMORY_PATH`, default `<data_dir>/retrospect_memory.md`)
  that accumulates evidence across tickets and only files an improvement
  draft once it judges the evidence sufficient.
- **Audit agent:** a meta-audit agent periodically reviews the repo
  against web-sourced best practices, identifies gaps in quality/security
  tooling coverage, and emits improvement draft tickets. Uses a
  Markdown memory ledger (`MILL_AUDIT_MEMORY_PATH`) for dedup.

## Ticket provenance (`source` field)

Every ticket records which actor created it — a human user, the
retrospect agent, the audit agent, or a future emitter — in a free-form
`source` string field (default `"user"`):

| Source value | Set by | Board badge |
|---|---|---|
| `"user"` | `POST /tickets` (CLI `ticket new`, API, web) | blue **user** |
| `"retrospect"` | Retrospect stage when spawning an improvement draft | amber **retrospect** |
| `"audit"` | Audit agent when emitting a gap improvement draft | green **audit** |
| (future) | Any future agent or emitter | grey |

The board renders a small coloured badge on every card. Fallback: if
`source` is missing or empty, the board treats it as `"user"`.

Stored in the `ticket` table as `source TEXT NOT NULL DEFAULT 'user'`.
An idempotent migration in `db.init_db` adds the column to existing
databases that lack it.

## Run

```sh
cp .env.example .env      # set OPENROUTER_API_KEY (+ FORGE_* later)
docker compose up -d --build
```

**Ticket board:** http://localhost:8077 — a live Kanban (one column
per state, click a card for history + description, auto-refreshes).
It's the same FastAPI service the CLI uses; localhost-only (the API is
unauthenticated).

```sh
docker compose exec mill robotsix-mill ticket new --title "Add X" --description-file -
docker compose exec mill robotsix-mill ticket list
docker compose exec mill robotsix-mill ticket show <id>
docker compose exec mill robotsix-mill ticket approve <id>
# Run an audit pass to identify tooling gaps:
docker compose exec mill robotsix-mill audit
```

## Local development (no Docker)

Run the exact same service on the host before deploying. Data lives in a
repo-local `./.mill-data/` (gitignored); config is read from `./.env`.

```sh
cp .env.example .env        # set OPENROUTER_API_KEY
make install                # venv + editable install (.[dev,tracing])
make dev                    # service with hot-reload on http://127.0.0.1:8077
# in another shell — the CLI is just an HTTP client to that service:
.venv/bin/robotsix-mill ticket new --title "Add X" --description-file -
.venv/bin/robotsix-mill ticket list
.venv/bin/robotsix-mill ticket approve <id>
# Run an audit pass:
.venv/bin/robotsix-mill audit
make test                   # run the suite
```

`make serve` runs it without reload (as in Docker); `make docker` builds
and runs the container instead. Nothing host-specific differs between
local and Docker except the data dir (`./.mill-data` vs `/data`), set
purely by `MILL_DATA_DIR`.

Running the pipeline always needs Docker (the agent's commands run in
disposable containers — there is no in-process mode; see **Security
model**). The unit test suite does **not** need Docker — it fakes the
sandbox seam — so `make test` works anywhere.

## Continuous deployment

On every push to `main`, a GitHub Actions workflow
(`.github/workflows/docker-publish.yml`) builds and publishes the
Docker image to Docker Hub as **`robotsix/mill:latest`** (plus a
short-SHA tag for pinning). A [Watchtower](https://containrrr.dev/watchtower/)
sidecar in the compose stack polls for new images and auto-updates the
running `mill` container — no manual rebuilds or restarts needed.

### Required GitHub secrets

| Secret | Purpose |
|---|---|
| `DOCKERHUB_USERNAME` | Docker Hub username for pushing images |
| `DOCKERHUB_TOKEN` | Docker Hub access token (or password) |

Set these in the repository **Settings → Secrets and variables →
Actions**. The publish workflow fires on push to `main` and on manual
`workflow_dispatch`; it does **not** trigger on pull requests.

### How auto-update works

The `watchtower` service in `docker-compose.yml` polls Docker Hub
every 300 seconds for a new `robotsix/mill:latest` image. It is scoped
via `--label-enable`, so only containers with the label
`com.centurylinklabs.watchtower.enable=true` — i.e., just `mill` — are
updated. When a new image is found, Watchtower pulls it and restarts
the `mill` container in-place, preserving all mounts and configuration.
The `--cleanup` flag removes old images to avoid disk bloat.

### Local development with Docker

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

## Approval gate

By default (`MILL_REQUIRE_APPROVAL=true`), the refine stage transitions
tickets to `awaiting_approval` instead of `ready`. The pipeline pauses
until a human approves, giving you a chance to review the refined spec
before the implement stage starts. Approve via:

- **Web board:** click the "Approve" button on any card in the
  `awaiting_approval` column.
- **CLI:** `robotsix-mill ticket approve <id>`
- **API:** `POST /tickets/{id}/approve`

To run fully autonomous (refine → implement with no pause), set
`MILL_REQUIRE_APPROVAL=false`.

## Notifications

When a ticket enters a human-attention state — `awaiting_approval`,
`in_review`, `blocked`, or `errored` — the worker fires a best-effort
push notification via [ntfy.sh](https://ntfy.sh) so you know to
intervene without watching the board.

Configure with two environment variables:

| Variable | Description |
|---|---|
| `NTFY_URL` | Full ntfy topic URL, e.g. `https://ntfy.sh/mytopic`. Leave blank to disable (the default). |
| `NTFY_TOKEN` | Optional bearer token sent as `Authorization: Bearer <token>`. |

Notification delivery is fire-and-forget: network errors and timeouts are
logged at warning level and never interfere with ticket processing. Only
worker-driven transitions trigger notifications — API/CLI transitions
(e.g. manual approve) do not.

## Cost controls & resilience

- **Implement agent + two lean sub-agents (each its own model).** A
  capable agent (`MILL_MODEL`) reads and edits the repo **itself**,
  kept lean by:
  - `explore(question)` — a cheap **scout** (`MILL_EXPLORE_MODEL`,
    `MILL_EXPLORE_REQUEST_LIMIT`) that returns concise pointers
    (paths/symbols/line-ranges), **never whole files**; the main
    agent then `read_file`s only what it needs.
  - `run_tests()` — a cheap **test sub-agent** (`MILL_TEST_MODEL`)
    runs the suite in the sandbox and **distills** failures into
    actionable feedback (never the raw log in the conversation).
  - `web_research(query)` — cheap web lookups, conclusion only, never
    `:online`.
  It loops read→edit→`run_tests` (≤`MILL_MAX_FIX_ITERATIONS`) until
  green or BLOCK-resumable. Refine likewise authors the spec with a
  `web_research` delegate. Each role has its own model so cheap models
  can be slotted in per-agent for cost leverage (all default to the
  capable model). No implement sub-agent and no `deep_*` layer — both
  re-explored everything and never converged.
- **No-progress safety net.** If a ticket re-enters the same
  model-driven stage `MILL_MAX_STUCK_CYCLES` times (default 3) without
  ever advancing — e.g. a run repeatedly killed before any checkpoint —
  the worker escalates it to `blocked` (resumable) and notifies, rather
  than silently re-billing the LLM on every requeue. Poll stages
  (`in_review` waiting on an open PR) are exempt.

## Retrospect memory

The retrospect agent maintains a single Markdown file — a living ledger
of issues observed across tickets. Each retrospect run:

1. Reads the current memory (empty if missing).
2. Passes it to the agent, which analyses the ticket in light of the
   memory, updates the ledger, and decides whether any tracked issue now
   has enough corroboration to file an improvement draft.
3. Writes the agent's updated memory back verbatim.

Deduplication is the agent's responsibility: it records when it has
already filed a draft for an issue and does not re-file.

Configure via `MILL_RETROSPECT_MEMORY_PATH` (defaults to
`<MILL_DATA_DIR>/retrospect_memory.md`).

## Audit agent

The audit agent is a **meta-audit** agent that proactively identifies
gaps in the repository's quality and security tooling coverage. It
reviews the repo against current web-sourced best practices, compares
findings against an agent-owned memory ledger, and emits concrete
improvement draft tickets — one per gap — that flow through the
existing pipeline.

### How it works

1. **Reads memory:** The agent reads its Markdown memory ledger
   (`MILL_AUDIT_MEMORY_PATH`, default `<MILL_DATA_DIR>/audit_memory.md`).
   Missing file → empty ledger, never fail.

2. **Web research:** Uses `web_research` to identify current best
   practices for repo quality/security coverage.

3. **Gap analysis:** Compares findings against the memory ledger to
   identify gaps NOT already recorded as proposed or done.

4. **Emits drafts:** For each specific, worthwhile gap, emits one
   improvement draft ticket (`source="audit"`) via the normal ticket
   pipeline.

5. **Updates memory:** Returns an updated memory ledger that the runner
   writes back verbatim.

Deduplication is the agent's responsibility via the memory ledger: it
will NOT re-emit a draft for a gap already recorded as proposed or done.

### Usage

**CLI:**
```sh
robotsix-mill audit              # summary output
robotsix-mill audit --json      # full JSON result
```

**API:**
```sh
curl -X POST http://localhost:8077/audit
```

**Web board:** Click the "Run Audit" button on the board page.

**Periodic polling (opt-in):**
```sh
# In .env:
MILL_AUDIT_PERIODIC=true
MILL_AUDIT_INTERVAL_SECONDS=3600  # 1 hour
```

### Configuration

| Variable | Default | Description |
|---|---|---|
| `MILL_AUDIT_PERIODIC` | `false` | Enable periodic audit passes |
| `MILL_AUDIT_INTERVAL_SECONDS` | `3600` | Seconds between automatic audits |
| `MILL_AUDIT_MEMORY_PATH` | (empty) | Override path for the audit memory ledger; falls back to `<data_dir>/audit_memory.md` |

### Important notes

- The audit agent does **NOT** scan code itself — it's a meta-coverage
  agent that proposes tools/agents/checks.
- The audit agent does **NOT** edit the repo directly — its only output
  is draft tickets (and its own memory ledger).
- The agent does **NOT** hard-code a fixed list of dimensions — it
  chooses targeted scopes dynamically based on web research and repo
  analysis.
- All repo-side output is draft tickets that must go through the
  approval gate (`awaiting_approval` → `ready` → `implement`).

## Workspace cleanup on close

When a ticket reaches the terminal `closed` state, its workspace's
`repo/` clone has served its purpose and can be deleted to reclaim
disk space. This happens automatically by default — configure with:

| Variable | Default | Description |
|---|---|---|
| `MILL_PRUNE_CLONE_ON_CLOSE` | `true` | Delete `repo/` when ticket closes |

When `true` (the default), the `repo/` directory is removed right before
the ticket transitions to `closed`. The `description.md` and the entire
`artifacts/` tree (including `retrospect.md`, `implement.md`, etc.) are
left intact.

This is a **best-effort** operation — if deletion fails (e.g. permission
error), the ticket still reaches `closed` and the error is logged but
never raised.

Set to `false` if you need to inspect the final repository state after a
ticket is finished (for post-mortem debugging).

## Security model

> Full container topology (mill vs. sibling sandbox, the three code
> copies, the docker.sock trust boundary):
> [docs/docker-architecture.md](docs/docker-architecture.md).

The `implement` agent runs LLM-chosen shell commands, and ticket text /
cloned repo content can steer that LLM (prompt injection). So command
execution is isolated from the mill process:

- **File tools** (`read_file`/`write_file`/`list_dir`) run in-process
  but are **path-confined** to the ticket's clone (`..`/symlink/abs
  escapes are rejected).
- **Command execution** (`run_command` and the test command) **always**
  runs in a fresh, disposable sibling container — `--network none`,
  `--rm`, non-root, read-only root + tmpfs `/tmp`, pids/memory capped,
  only the ticket's repo reachable. Needs the host Docker socket
  (root-equivalent on the host — see `docker-compose.yml`). There is
  **no in-process/local mode**: it was a foot-gun that let the agent
  edit the host and recursively re-invoke the pipeline. Tests fake the
  sandbox seam instead.

## Layout

| Path | Role |
|---|---|
| `config.py` | settings (env / .env) |
| `core/states.py` | state machine (single source of truth) |
| `core/models.py` | SQLModel tables + API schemas |
| `core/db.py` · `core/service.py` | DB lifecycle + management-plane operations |
| `core/workspace.py` | per-ticket file workspace (file-canonical body) |
| `runtime/worker.py` | event-driven queue + stage chaining (+ audit poll) |
| `runtime/api.py` | FastAPI app (API + worker lifespan + audit route) |
| `runtime/tracing.py` | Langfuse tracing + OpenRouter cost ✅ |
| `sandbox.py` | isolated command execution (always containerized) |
| `stages/` refine·implement·deliver·merge·retrospect | ✅ all real |
| `audit_runner.py` | audit pass orchestration |
| `agents/auditing.py` | audit agent (meta-audit for gaps) |
| `forge/github.py` · `forge/auth.py` | GitHub PR/status + PAT/App-bot auth ✅ |
| `langfuse_client.py` | read-side session summary (for retrospect) |
| `agents/coding.py` · `fs_tools.py` · `retrospecting.py` | agents + sandboxed tools |
| `vcs/git_ops.py` | clone / branch / commit / push helpers |

**Delivery identity** (PAT or GitHub App bot) setup procedure:
[docs/github-app.md](docs/github-app.md).

## Next steps

Full chain `refine → approve → implement → deliver → merge → retrospect`
runs end-to-end. The human approval gate after refine (configurable via
`MILL_REQUIRE_APPROVAL`) gives you control over when implementation
begins and bounds the retrospect→draft loop. Remaining: the **GitLab**
forge adapter (`forge/gitlab.py` is still a stub`).

**New:** The **audit agent** (`agents/auditing.py`) meta-audits the
repo for quality/security coverage gaps and emits improvement drafts.
Enable with `MILL_AUDIT_PERIODIC=true` for automatic periodic passes,
or trigger on-demand via CLI (`robotsix-mill audit`), API (`POST /audit`),
or the web board button.
