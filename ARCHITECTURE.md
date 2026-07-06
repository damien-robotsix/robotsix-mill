# Architecture

A single-page map of **robotsix-mill** — read this first, then dive into
the detailed docs under [`docs/`](docs/) for any component you need to
understand deeply.

robotsix-mill is a **self-contained, LLM-driven ticket-to-merge-request
pipeline**. A ticket goes in one end; an opened (and eventually merged)
pull request comes out the other. The mill *is* the orchestrator — it is
not a CI plugin, a webhook handler, or a job scheduler.

## System overview

Emit a ticket and the mill runs the full autonomous pipeline. State
lives in a single SQLite database (the *management plane*); each ticket
also gets a filesystem *workspace* (the *work plane*) where agents edit a
canonical `description.md` and a per-ticket git clone of the target repo.

The end-to-end flow is:

```
ticket ─refine▶ approve (human gate) ─implement▶ document ─review▶
  deliver (open PR) ─merge (CI + mergeability gates)▶ done ─retrospect▶ closed
```

Dispatch is **event-driven**: creating a ticket or changing its state
enqueues it, and an in-process worker pool picks it up immediately and
**chains** stages until the ticket reaches a terminal or human-wait
state. There is no cron and no polling, with the single exception of the
merge stage, which polls a PR for CI/mergeability status. Distinct
tickets run in parallel (bounded by `MILL_MAX_CONCURRENCY`); a single
ticket's stages stay strictly ordered, and a dedup set prevents the same
ticket from running twice at once.

The only time the mill touches GitHub/GitLab is the `deliver` stage
(open a PR) and the `merge` stage (poll/merge it). Everything else is
local.

## Core components

| Component | Where | Responsibility |
|---|---|---|
| **Management plane** | `core/` | SQLModel/SQLite domain model: `Ticket`, `Comment`, `State`, `TicketService` business logic, workspace paths, datetime/text helpers. |
| **Runtime / worker** | `runtime/` | FastAPI HTTP API (`api.py`), the event-driven worker pool and stage-chain loop (`worker/`), run registry, tracing, board HTML/static assets, and API routes. |
| **Stages** | `stages/` | One orchestrator per lifecycle step. Each stage binds a `State` to its agent(s), manages the workspace and pause/resume state, and decides the next transition. `registry.py` maps states → stages. |
| **Agents** | `agents/` | All LLM-based agents (coding, reviewing, documenting, refining, auditing, …) plus the agent-builder infra (`base.py`), the tool registry, and LLM-callable tool factories. |
| **Forge** | `forge/` | Pluggable git-forge adapter (`base.py` contract; `github.py`, `gitlab.py` backends). Opens PRs/MRs, comments, reads PR status, handles forge auth. |
| **VCS** | `vcs/` | Thin `git` CLI wrappers (`git_ops.py`) for per-ticket clone / branch / commit / push. |
| **Runners** | `runners/` | Glue layer above agents: periodic and one-shot passes (audit, health, trace-health, copy-paste, …) plus shared `pass_runner` / `periodic_runner` infrastructure. |
| **Sandbox** | `sandbox.py` | Containerized command execution — every agent command runs in a disposable, network-less sibling Docker container. |
| **Config / CLI** | `config/`, `cli.py` | `Settings`/`RepoConfig` loaded from YAML + env; `robotsix-mill` is a thin HTTP client CLI over the management API. |
| **Tracing** | `langfuse/` | Optional Langfuse client for session cost tracking and trace fetching; a no-op unless per-repo credentials are configured. |
| **Notifications** | `notify.py` | Best-effort ntfy push on human-attention states. |
| **Meta-pass** | `meta/` | Cross-repo survey agent that clones every registered repo and files extraction / alignment proposals. |

## Data models

- **Ticket lifecycle / state machine** — `core/states.py` defines the
  `State` enum and the `TRANSITIONS` map. Each *active* state is owned by
  exactly one stage (`STAGE_FOR_STATE`); `ERRORED` and `BLOCKED` are side
  states reachable from any active state and require a human.
  `HUMAN_ISSUE_APPROVAL` and `HUMAN_MR_APPROVAL` are human-wait gates.
  The merge-related states (`IMPLEMENT_COMPLETE`, `WAITING_AUTO_MERGE`,
  `REBASING`, `FIXING_CI`, `ADDRESSING_REVIEW`) model the
  poll-and-recover behaviour around an open PR.
- **Ticket / Comment** — `core/models.py`. The DB row holds metadata,
  state, history, and a pointer + content hash for the file-canonical
  `description.md`.
- **Run registry** — `runtime/run_registry.py` tracks in-flight stage
  runs for observability on the board.

## Entrypoints

- **Runtime service** — `entrypoint.sh` execs `robotsix-mill serve`,
  which starts the FastAPI app (`runtime/api.py::create_app`) and the
  event-driven `Worker` (`runtime/worker/`) under uvicorn. This is the
  long-lived process and the primary interface (board at `:8077`).
- **CLI** — `cli.py` (`robotsix-mill`), a thin HTTP client for ticket /
  epic / repo / audit / trace-health operations.
- **Periodic runners** — `runners/` (and `meta/runner.py`) host
  scheduled maintenance passes (audit, health, module-curator,
  meta-pass, …) dispatched by the worker's periodic supervisor.

## Design principles

- **Self-contained.** No forge webhooks, no CI plugins, no external
  scheduler. The mill drives its own pipeline end to end.
- **No webhooks / event-driven.** State changes enqueue work directly;
  only the merge stage polls (a PR's CI status).
- **SQLite-only management plane.** All ticket state, history, and cost
  tracking live in one SQLite DB — zero external DB dependencies.
- **Containerized isolation.** Every agent command runs in a disposable
  `--network none`, non-root, read-only sibling container; the host
  filesystem is protected by path confinement and only the per-ticket
  clone is editable.
- **File-canonical work plane.** `description.md` and the cloned repo on
  the workspace volume are the source of truth that agents edit; the DB
  only points at them.

## Deeper dives

This page is the map; the territory lives in [`docs/`](docs/):

- [docs/docker-architecture.md](docs/docker-architecture.md) — container
  topology, the sibling-sandbox model, and the trust boundary.
- [docs/configuration.md](docs/configuration.md) — full settings
  reference and YAML loading order.
- [docs/agents/index.md](docs/agents/index.md) — the agent catalog.
- [docs/approval-gate.md](docs/approval-gate.md) — the human approval
  gate after refine.
- [docs/merge-stage.md](docs/merge-stage.md) — gate-check, auto-rebase,
  and auto-fix for merge-ready PRs.
- [docs/sandbox/security.md](docs/sandbox/security.md) — the security model.
- [docs/design/](docs/design/) — focused design notes (e.g.
  [cost-indicators.md](docs/design/cost-indicators.md)).
- [docs/modules.yaml](docs/modules.yaml) — the canonical module taxonomy
  (every tracked file mapped to a module).
