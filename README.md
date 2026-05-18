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
  scout_memory.md              # scout agent's model-evaluation ledger

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
- **Scout agent:** a standing draft-only agent that queries the
  OpenRouter API to evaluate models per agent role on provider count,
  health, stability, capability, price, and latency. Emits model-switch
  draft tickets when a materially better option exists or a configured
  model regresses. Uses a Markdown memory ledger (`MILL_SCOUT_MEMORY_PATH`)
  for dedup.
- **Trace-health check:** a deterministic, no-LLM check that scans
  Langfuse for traces lacking a `sessionId` (unsessioned) in the last
  24 hours and files a single draft alert ticket when any are found.
  Deduplicates against existing open trace-health tickets. Runs on-demand
  or periodically (opt-in).

## Ticket provenance (`source` field)

Every ticket records which actor created it — a human user, the
retrospect agent, the audit agent, or a future emitter — in a free-form
`source` string field (default `"user"`):

| Source value | Set by | Board badge |
|---|---|---|
| `"user"` | `POST /tickets` (CLI `ticket new`, API, web) | blue **user** |
| `"retrospect"` | Retrospect stage when spawning an improvement draft | amber **retrospect** |
| `"audit"` | Audit agent when emitting a gap improvement draft | green **audit** |
| `"scout"` | Scout agent when emitting a model-switch draft | violet **scout** |
| `"trace-health"` | Trace-health check when unsessioned traces detected | cyan **trace-health** |
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
unauthenticated). Each card shows the cumulative LLM spend for that
ticket (e.g. `$0.0943`), updated automatically as the ticket moves
through stages.

```sh
docker compose exec mill robotsix-mill ticket new --title "Add X" --description-file -
docker compose exec mill robotsix-mill ticket list
docker compose exec mill robotsix-mill ticket show <id>
docker compose exec mill robotsix-mill ticket approve <id>
docker compose exec mill robotsix-mill ticket resume-blocked <id>
# Run an audit pass to identify tooling gaps:
docker compose exec mill robotsix-mill audit
# Run a scout pass to evaluate models:
docker compose exec mill robotsix-mill scout
# Run a trace-health check to detect unsessioned traces:
docker compose exec mill robotsix-mill trace-health
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
.venv/bin/robotsix-mill ticket resume-blocked <id>
# Run an audit pass:
.venv/bin/robotsix-mill audit
# Run a scout pass:
.venv/bin/robotsix-mill scout
# Run a trace-health check:
.venv/bin/robotsix-mill trace-health
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

## Blocked ticket recovery

When a ticket is blocked (e.g. a retrospect agent failure), the state
it was blocked *from* is recorded. You can recover in two ways:

- **Resume to the originating state** (re-runs only the failed stage):
  ```sh
  robotsix-mill ticket resume-blocked <id>
  ```
  This transitions `BLOCKED → <blocked_from>` (e.g. `BLOCKED → DONE`
  to re-run retrospect, skipping implement and refine).

- **Manual override** (re-runs the full downstream chain):
  - `BLOCKED → READY` (re-runs implement → deliver → merge → retrospect)
  - `BLOCKED → DRAFT` (re-runs refine → implement → ...)
  
  Use the generic transition endpoint or the board.

No raw database editing is ever needed to recover a blocked ticket.

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

## Merge stage: auto-rebase of stale PRs

When a PR sits `in_review` while other PRs merge onto the target branch,
it may become stale and develop merge conflicts. Rather than stranding
such PRs, the merge stage automatically invokes a **rebase agent**
(`agents/rebasing.py`) that resolves conflicts using the LLM.

- The forge's PR status now includes a `mergeable` flag.
- If a PR is open and **mergeable**, the existing no-op (re-poll) path
  is preserved exactly.
- If a PR is open and **conflicting**, the merge stage invokes
  `run_rebase_agent` on the ticket's workspace clone.
- On success the ticket branch is force-pushed (the ticket stays
  `in_review` for the next poll to observe the now-mergeable PR).
- On failure the ticket escalates to `BLOCKED` (resumable) — no
  half-rebased state is ever pushed.

| Variable | Default | Description |
|---|---|---|
| `MILL_REBASE_MAX_ATTEMPTS` | `2` | Max rebase attempts per ticket before escalating to BLOCKED. Each attempt is one LLM invocation. |

The rebase agent uses the same sandboxed shell + file tools as the
implement agent, scoped to the ticket's clone. It never pushes, opens
PRs, or interacts with the forge.

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

## Scout agent

The scout agent is a **drafts-only** model evaluator that queries the
OpenRouter REST API (`/api/v1/models` and `/api/v1/models/{id}/endpoints`)
and, for each agent role, evaluates the currently configured model
against better candidates. It reasons per role about:

- **Provider count & health** — number of serving providers,
  per-endpoint `status` and `uptime_last_30m`; strongly prefers
  multi-provider so a single provider's 429/latency has a fallback.
- **Preview/stability** — `*-preview` or dated ids likely to change
  or vanish.
- **Capability fit** — tool-calling support for coordinator/refine/
  retrospect/audit roles; price/latency for cheap explore/test/
  web-research roles.
- **Price & latency** — `prompt`/`completion` $/1M pricing.

It emits an improvement **draft** when, for any role, a materially
better option exists OR a configured model regressed (left preview /
id changed / lost providers / sole provider degraded). The draft
names the role, the candidate, the evidence, and the precise
`MILL_*_MODEL` / `.env.example` change — **never auto-switches**,
never edits config itself. All drafts flow through the normal pipeline
(refine → approval gate → implement → PR → human merge).

### How it works

1. **Reads memory:** Reads its Markdown memory ledger
   (`MILL_SCOUT_MEMORY_PATH`, default `<MILL_DATA_DIR>/scout_memory.md`).
   Missing file → empty ledger, never fails.

2. **Fetches model data:** Queries OpenRouter's `/api/v1/models` for
   pricing/context_length and `/api/v1/models/{id}/endpoints` for
   per-provider status/uptime/tool-call support.

3. **Evaluates per role:** Scores the current model and a pool of
   candidates on provider count, uptime, stability, capability fit,
   and price.

4. **Emits drafts:** When a materially better candidate exists (score
   delta > 10) or the current model regressed, creates one draft
   ticket per role (`source="scout"`).

5. **Updates memory:** Records proposals in the memory ledger so the
   same switch is never re-proposed.

### Usage

**CLI:**
```sh
robotsix-mill scout              # summary output
robotsix-mill scout --json      # full JSON result
```

**API:**
```sh
curl -X POST http://localhost:8077/scout
```

**Web board:** Click the "Run Scout" button on the board page.

**Periodic polling (opt-in):**
```sh
# In .env:
MILL_SCOUT_PERIODIC=true
MILL_SCOUT_INTERVAL_SECONDS=86400  # 1 day
```

### Configuration

| Variable | Default | Description |
|---|---|---|
| `MILL_SCOUT_PERIODIC` | `false` | Enable periodic scout passes |
| `MILL_SCOUT_INTERVAL_SECONDS` | `86400` | Seconds between automatic scouts |
| `MILL_SCOUT_MEMORY_PATH` | (empty) | Override path for the scout memory ledger; falls back to `<data_dir>/scout_memory.md` |

### Important notes

- The scout agent does **NOT** use an LLM — it makes direct REST calls
  to the OpenRouter API. No `:online` suffix, no pydantic-ai model.
- The scout does **NOT** auto-switch models or edit config — its only
  output is draft tickets (and its own memory ledger).
- A single-provider or preview-only candidate is flagged **Fragile** in
  the draft body.
- All drafts must go through the approval gate
  (`awaiting_approval` → `ready` → `implement`).

## Trace-health check

The trace-health check is a **deterministic, no-LLM** check that scans
Langfuse for traces in the last 24 hours that are missing a `sessionId`
(unsessioned). Sub-agent / coordinator traces can fail to inherit the
ticket root span's `session.id`, and those orphaned traces carry cost
and latency that cannot be attributed to any ticket. This check surfaces
them automatically.

### How it works

1. **Short-circuits** when tracing is disabled (`LANGFUSE_*` not set).

2. **Fetches all traces** from the last 24 hours via the Langfuse
   public API (paginated, with graceful error handling).

3. **Partitions** traces: those with a falsy `sessionId` (missing,
   `None`, or `""`) are "unsessioned."

4. **Skips silently** when there are zero unsessioned traces or zero
   total traces.

5. **Deduplicates:** queries the ticket table for any existing
   `source="trace-health"` ticket not in `CLOSED` state. If one
   exists, skips — an alert is already live.

6. **Files a single draft ticket** (`source="trace-health"`) with a
   structured body listing the window, the counts, up to 5 example
   trace IDs/names, and a note about the likely cause. The ticket
   flows through the normal pipeline (refine → approval gate →
   implement → PR → human merge).

The actual fix for session inheritance is a separate ticket the
pipeline will produce; this check is only the alert.

### Usage

**CLI:**
```sh
robotsix-mill trace-health              # summary output
robotsix-mill trace-health --json      # full JSON result
```

**API:**
```sh
curl -X POST http://localhost:8077/trace-health
```

**Web board:** Click the "Trace Health" button on the board page.

**Periodic polling (opt-in):**
```sh
# In .env:
MILL_TRACE_HEALTH_PERIODIC=true
MILL_TRACE_HEALTH_INTERVAL_SECONDS=86400  # 1 day
```

### Configuration

| Variable | Default | Description |
|---|---|---|
| `MILL_TRACE_HEALTH_PERIODIC` | `false` | Enable periodic trace-health checks |
| `MILL_TRACE_HEALTH_INTERVAL_SECONDS` | `86400` | Seconds between automatic checks (minimum 3600) |

### Important notes

- The trace-health check does **NOT** use an LLM — it is pure data
  inspection (HTTP fetch + SQL query).
- The check does **NOT** fix the root cause (sub-agent span
  inheritance) — its only output is a draft alert ticket.
- The 24-hour lookback window is **hard-coded**, not configurable.
- The minimum periodic interval is **3600s (1 hour)**, enforced in
  the worker to avoid hammering Langfuse.
- When `LANGFUSE_*` is not configured, the check is a zero-cost no-op.

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
| `runtime/worker.py` | event-driven queue + stage chaining (+ audit/scout/trace-health poll) |
| `runtime/api.py` | FastAPI app (API + worker lifespan + audit/scout/trace-health route) |
| `runtime/tracing.py` | Langfuse tracing + OpenRouter cost ✅ |
| `sandbox.py` | isolated command execution (always containerized) |
| `stages/` refine·implement·deliver·merge·retrospect | ✅ all real |
| `audit_runner.py` | audit pass orchestration |
| `scout_runner.py` | scout pass orchestration |
| `trace_health_runner.py` | trace-health check orchestration |
| `agents/auditing.py` | audit agent (meta-audit for gaps) |
| `agents/scouting.py` | scout agent (model evaluation against OpenRouter) |
| `forge/github.py` · `forge/auth.py` | GitHub PR/status + PAT/App-bot auth ✅ |
| `langfuse_client.py` | read-side session summary + trace listing (for retrospect + trace-health) |
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

**New:** The **scout agent** (`agents/scouting.py`) evaluates
OpenRouter models per agent role on provider count, health, stability,
capability, price, and latency. Enable with `MILL_SCOUT_PERIODIC=true`
for automatic periodic passes, or trigger on-demand via CLI
(`robotsix-mill scout`), API (`POST /scout`), or the web board button.

**New:** The **trace-health check** (`trace_health_runner.py`)
scans Langfuse for unsessioned traces and files an alert draft when
any are found. Enable with `MILL_TRACE_HEALTH_PERIODIC=true` for
automatic periodic checks, or trigger on-demand via CLI
(`robotsix-mill trace-health`), API (`POST /trace-health`), or the
web board button.
