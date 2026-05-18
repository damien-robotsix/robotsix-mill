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
  retrospect_memory.md          # agent-maintained issue ledger

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
- **Event-driven:** ticket emission / state change enqueues; the
  in-process worker picks it up at once and **chains** stages until a
  terminal state or a stub. No cron, no polling.
- **Delivery:** pluggable forge adapter (GitHub / GitLab), invoked only
  by the `deliver` stage.
- **Tracing:** optional Langfuse; a no-op unless `LANGFUSE_*` is set.
- **Retrospect memory:** the retrospect agent maintains a Markdown ledger
  (`MILL_RETROSPECT_MEMORY_PATH`, default `<data_dir>/retrospect_memory.md`)
  that accumulates evidence across tickets and only files an improvement
  draft once it judges the evidence sufficient.

## Ticket provenance (`source` field)

Every ticket records which actor created it — a human user, the
retrospect agent, or a future emitter — in a free-form `source` string
field (default `"user"`):

| Source value | Set by | Board badge |
|---|---|---|
| `"user"` | `POST /tickets` (CLI `ticket new`, API, web) | blue **user** |
| `"retrospect"` | Retrospect stage when spawning an improvement draft | amber **retrospect** |
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

- **Cheap driver, strong author (refine + implement).** A ~$3 ticket
  traced to the expensive model running the whole agentic loop. Now a
  cheap, small-context **driver** (`MILL_MODEL`, default
  `tencent/hy3-preview`) orchestrates and delegates to bounded,
  context-isolated sub-agents — it never reads the repo or authors
  code itself:
  - `explore(question)` — fresh-context read-only sub-agent on the
    driver model (`MILL_EXPLORE_REQUEST_LIMIT`) navigates the repo and
    returns only what was asked, so the driver's context stays small.
  - `web_research(query)` — cheap sub-agent
    (`MILL_WEB_RESEARCH_MODEL`, `MILL_WEB_RESEARCH_REQUEST_LIMIT`),
    returns a conclusion only.
  - `deep_refine` / `deep_implement` — the **strong** model
    (`MILL_DEEP_MODEL`, default `deepseek/deepseek-v4-pro`,
    `MILL_DEEP_MODEL_REQUEST_LIMIT`) is handed one complete curated
    context and returns the finished spec / the full changed-file
    contents. No tools, ~one-shot, never `:online` — invoked
    deliberately, not for the whole loop.
  The driver applies `deep_implement`'s output with `write_file` and
  verifies with `run_tests` (trimmed result), re-calling the strong
  model with failures. The expensive model is thus paid once per
  authored artifact, on full context — not per exploration step.
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
| `runtime/worker.py` | event-driven queue + stage chaining |
| `runtime/api.py` | FastAPI app (API + worker lifespan) |
| `runtime/tracing.py` | Langfuse tracing + OpenRouter cost ✅ |
| `sandbox.py` | isolated command execution (always containerized) |
| `stages/` refine·implement·deliver·merge·retrospect | ✅ all real |
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
forge adapter (`forge/gitlab.py` is still a stub).
