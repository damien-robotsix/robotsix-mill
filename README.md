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

emit ticket ─▶ API inserts row + enqueues ─▶ worker chains stages ─▶ done
   draft ─refine▶ ready ─implement▶ in_review ─review▶ deliverable ─deliver▶ done
   (any active state ─▶ failed / blocked; a human transition re-enqueues)
```

- **Engine:** `pydantic-ai` over OpenRouter.
- **Event-driven:** ticket emission / state change enqueues; the
  in-process worker picks it up at once and **chains** stages until a
  terminal state or a stub. No cron, no polling.
- **Delivery:** pluggable forge adapter (GitHub / GitLab), invoked only
  by the `deliver` stage.
- **Tracing:** optional Langfuse; a no-op unless `LANGFUSE_*` is set.

## Run

```sh
cp .env.example .env      # set OPENROUTER_API_KEY (+ FORGE_* later)
docker compose up -d --build
```

```sh
docker compose exec mill robotsix-mill ticket new --title "Add X" --description-file -
docker compose exec mill robotsix-mill ticket list
docker compose exec mill robotsix-mill ticket show <id>
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
make test                   # run the suite
```

`make serve` runs it without reload (as in Docker); `make docker` builds
and runs the container instead. Nothing host-specific differs between
local and Docker except the data dir (`./.mill-data` vs `/data`), set
purely by `MILL_DATA_DIR`.

Locally there is no Docker socket, so set `MILL_SANDBOX_MODE=local` in
`./.env` (the `implement` agent's shell + test command then run
in-process — fine for trusted dev, see **Security model**).

## Security model

The `implement` agent runs LLM-chosen shell commands, and ticket text /
cloned repo content can steer that LLM (prompt injection). So command
execution is isolated from the mill process:

- **File tools** (`read_file`/`write_file`/`list_dir`) run in-process
  but are **path-confined** to the ticket's clone (`..`/symlink/abs
  escapes are rejected).
- **Command execution** (`run_command` and the test command) goes
  through the sandbox:
  - `MILL_SANDBOX_MODE=docker` (default): a fresh, disposable sibling
    container per command — `--network none`, `--rm`, non-root,
    read-only root + tmpfs `/tmp`, pids/memory capped, only the
    ticket's repo reachable. Needs the host Docker socket
    (root-equivalent on the host — see `docker-compose.yml`).
  - `MILL_SANDBOX_MODE=local`: in-process shell with a process-group
    timeout kill. **Not isolated** — trusted dev/CI only.

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
| `runtime/tracing.py` | optional Langfuse tracing |
| `sandbox.py` | isolated command execution (docker / local) |
| `stages/implement.py` | clone → branch → agent → test/fix loop ✅ |
| `stages/` (refine, review, deliver) | still stubs |
| `forge/` | GitHub/GitLab delivery adapters (stubs) |
| `agents/coding.py` · `agents/fs_tools.py` | implement agent + sandboxed tools |
| `vcs/git_ops.py` | clone / branch / commit helpers |

## Next steps

`implement` is done. Remaining stages, one at a time: `refine`
(draft→ready), `review` (gate in_review), `deliver` (push branch + open
MR via the forge adapter).
