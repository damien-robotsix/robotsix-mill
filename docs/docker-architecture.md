# Docker architecture

## Conceptual architecture

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
  draft ─refine▶ human_issue_approval ─approve▶ ready ─implement▶ deliverable
        ─deliver▶ human_mr_approval ─(PR merged; merge-poll)▶ done ─retrospect▶ closed
  human_mr_approval = PR open (the PR is the review); merge poll flips it.
  retrospect audits the run + Langfuse and may spawn an improvement draft.
  closed = terminal. errored = worker-level crash (rare); blocked = needs a human.
  Transient stage failures (git outage, provider 5xx) are retried automatically
  with exponential backoff (see MILL_STAGE_RETRY_*). Fatal stage errors and
  exhausted retries go straight to BLOCKED.

  BLOCKED / retrying recovery (no raw-DB editing ever needed):
    BLOCKED ─resume-blocked▶ <blocked_from>   (re-run only the failed stage)
    BLOCKED → READY | DRAFT                    (manual override: full re-run)
    retrying ticket → resume-blocked           (clears retry state, re-enqueues)
  human_issue_approval is a human gate (configurable via MILL_REQUIRE_APPROVAL).
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
- **Tracing:** optional Langfuse; a no-op unless per-repo Langfuse credentials are configured in `config/repos.yaml`. On SIGTERM/SIGINT the worker flushes any pending trace spans before exiting so cost/latency data is not lost.

## Container topology

How command execution is isolated when the pipeline runs. The confusing
part: the sandbox is a **sibling** container, not a nested one.

```
┌─ HOST ────────────────────────────────────────────────────────────┐
│  • Docker daemon (socket owned by some gid — see DOCKER_GID)       │
│  • <repo>/                ← your LIVE source. NEVER mounted.       │
│  • <repo>/.data           ← bind-mounted into mill (host-visible)  │
│                                                                   │
│  ┌─ mill container (long-lived) ───────────────────────────────┐  │
│  │  built from this repo's Dockerfile (COPY . /app)            │  │
│  │  runs: HTTP API + event-driven worker (uvicorn)             │  │
│  │  mounts:                                                    │  │
│  │    ./.data              → /data       (tickets + clones)    │  │
│  │    /var/run/docker.sock → talks to the HOST daemon          │  │
│  │  implement stage: git clone <FORGE_REMOTE_URL>              │  │
│  │     → /data/workspaces/<id>/repo   (a SEPARATE copy)        │  │
│  │                                                             │  │
│  │  to run any command it asks the HOST daemon (via socket):   │  │
│  └──────────────────────────│──────────────────────────────────┘  │
│                              │  docker run ...                     │
│                              ▼                                     │
│  ┌─ sandbox container (one per command, --rm) ─────────────────┐   │
│  │  SIBLING of mill (both children of the host daemon)         │   │
│  │  --network none, non-root, read-only root, tmpfs /tmp,      │   │
│  │  pids/memory capped                                         │   │
│  │  /tmp mounted exec (not noexec) so pip --user console        │   │
│  │  scripts under $HOME/.local/bin can execute                 │   │
│  │  mounts only:  ./.data → /data                              │   │
│  │  runs the single shell command, then is destroyed           │   │
│  └─────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────┘
```

## The three layers

1. **mill container ≠ sandbox container.** `mill` is one long-lived
   container (the app: API + worker). The sandbox is a *fresh,
   disposable* container created **per command** and destroyed after.
   They are **siblings** — both started by the host daemon. mill creates
   the sandbox by sending `docker run` to the host socket it mounts.

2. **This is "docker-*beside*-docker", not true nested DinD.** mill does
   not run its own daemon; it borrows the host's via the mounted socket.
   Consequence: `-v` source paths resolve on the **host**, not inside
   mill — so the sandbox must mount the host path of `./.data`
   (`MILL_SANDBOX_DATA_MOUNT`), since a volume *name* known inside mill
   is meaningless to the host daemon.

3. **Three distinct copies of the code — never conflate them:**
   - **Live source** (`<repo>/`): never mounted anywhere; the agent has
     no path to it.
   - **mill image code**: baked in at build (`COPY . /app`); runs the
     orchestrator only.
   - **Per-ticket clone** (`./.data/workspaces/<id>/repo`): a fresh
     `git clone` of `FORGE_REMOTE_URL`; the *only* code the agent edits.
     It is the same bytes as host `./.data/workspaces/<id>/repo`, so you
     can inspect it directly.

## Why a separate sandbox

The agent's commands are LLM-chosen and prompt-injectable (ticket text
and cloned-repo content steer the model). Running them in a
`--network none`, throwaway, non-root container means they cannot touch
the host, the network, or recurse. There is intentionally **no
in-process / "local" mode** — that was a foot-gun that let the agent
edit the host and recursively re-invoke the pipeline.

## web_fetch — a deliberate, narrowed exception

The refine/implement agents need to read real docs (web search is the
model's native OpenRouter `:online`; `web_fetch` reads a specific URL).
`web_fetch` runs in its **own** container that, unlike the command
sandbox, **has network**. To bound the trade-off it is locked down the
other way:

- **no repo/data mount** — nothing local to exfiltrate;
- non-root, `--read-only`, `--cap-drop ALL`, `--security-opt
  no-new-privileges`, pids/memory capped, `--rm`;
- a **fixed `curl`** (the dedicated `curlimages/curl` image), not a
  shell — the URL is a plain argv item (no command injection);
- http(s) only, size- and time-capped.

**Residual risk (accepted):** the agent chooses the URL, so it could in
principle encode data it already holds into a fetched URL. There is no
local data in that container to steal, but the URL itself is an egress
channel. This is a conscious trade for letting the agent learn unfamiliar
libraries instead of guessing.

## Trust boundary / residual risk

Mounting `/var/run/docker.sock` into mill gives mill (and any code that
breaks out of its orchestration logic) **root-equivalent control of the
host Docker daemon**. The agent itself only ever gets the sandboxed
tools, but the socket is the trust boundary you accept by running the
pipeline. Run it on a host you would trust the agent with, or on a
disposable VM.

## Operational notes (durable gotchas)

- **`DOCKER_GID` must match the host socket group.** Find it with
  `stat -c %g /var/run/docker.sock` and set it as an environment variable
  (e.g. `export DOCKER_GID=…` before `docker compose up`). The non-root
  `mill` user is added to that gid (`group_add`) so it can use the
  socket. Wrong gid → mill can't reach the socket → sandbox fails.
- **`MILL_SANDBOX_DATA_MOUNT`** must be the **host** absolute path of
  the data dir (compose sets `${PWD}/.data`). Run `docker compose` from
  the repo root so `$PWD` is correct.
- **The mill image needs a working `docker` *client* binary** to issue
  `docker run` to the host socket. (Debian's `docker.io` package does
  not reliably put one on `PATH` on slim bases — install a real CLI,
  e.g. `docker-ce-cli` from Docker's apt repo.)
- **The unit test suite never uses Docker.** It fakes the sandbox seam
  (`tests/conftest.py::fake_sandbox`), so `make test` runs anywhere.
  Only *running the pipeline* needs Docker.
