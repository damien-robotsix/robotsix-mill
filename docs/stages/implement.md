# Implement stage: agent-driven code generation

The implement stage is the core code-generation pipeline stage.  It
clones the target repository, creates a feature branch, and runs a
deterministic, Python-enforced fix loop: an LLM agent edits source
files, the test gate runs the suite, and the routing (proceed / retry /
escalate) is decided in code — not by the model.  The loop is bounded
by `max_fix_iterations`; the agent never owns the loop or the bound.

When the test suite passes, the ticket transitions to `DELIVERABLE`
so the deliver stage can open a PR.  When every retry is exhausted,
the ticket escalates to `BLOCKED` (resumable).

## Lifecycle: `READY` → `DELIVERABLE` (or `BLOCKED`)

```
READY
  │
  ├── preflight gates (spawn limit, cycle cap, stale-spec guard,
  │                    stall guard, tool/skill integrity)
  │   └── fail → BLOCKED
  │
  ├── prerequisite gate (unmet deps → spawn dep-fix ticket, BLOCK)
  │
  ├── clone repo + create feature branch (skip if resuming)
  │
  ├── scope guardrail (out-of-scope file detection)
  │   └── triggers scope-triage classifier (see below)
  │
  ├── implement fix loop ────────────┐
  │   ├── agent edit pass            │
  │   ├── test gate                  │
  │   ├── routing: proceed / retry ──┘ (up to max_fix_iterations)
  │   └── routing: escalate → BLOCKED
  │
  ├── pass → DELIVERABLE
  │
  └── BLOCKED (resumable)
```

### Preflight gates

Before a Langfuse trace opens — and therefore before any model is
invoked or any spawn slot is consumed — cheap deterministic checks
block obviously no-op tickets:

1. **Epic guard** — epics routed to implement are blocked; they must
   be broken into child tasks first.
2. **Deploy-freshness gate** — if the running worker image is stale
   (a newer image is available from the deploy server), the ticket is
   blocked to avoid reproducing bugs already fixed upstream.
3. **Implement spawn counter** (`implement_max_spawns_per_ticket`) —
   caps total implement-stage invocations per ticket to prevent
   unbounded LLM quota burn across re-spawns.  Transient environment
   errors (sandbox EOF, OOM) do not count toward the limit.
4. **Implement-review cycle cap** (`max_implement_review_cycles`) —
   catches runaway implement↔review loops before a trace opens.
5. **Stale re-spawn guard** — if the ticket's effective spec (direct
   description + epic context) hasn't changed since a prior
   `BLOCKED — resumable` outcome, re-spawning is blocked.  Operators
   can reset this via the `reset-fingerprint` endpoint or by updating
   the specification.
6. **Cross-spawn stall guard** — if the stall detector already tripped
   across consecutive BLOCKED cycles, the ticket is blocked before
   another spawn slot is consumed (see below).
7. **Tool-definition integrity** — the loaded agent definition must
   declare at least one tool.
8. **Skill-file integrity** — every skill referenced by the agent
   definition must exist on disk.

## The fix iteration loop

The implement agent does **one edit pass per iteration**; the test
gate runs the suite once and produces a distilled diagnosis;
`ValidationResult.decide` routes deterministically.  On `retry`
the diagnosis is fed back into the next pass; on `escalate`
(test failures persist after `max_fix_iterations`) the ticket is
`BLOCKED` (resumable).  No LLM owns the loop or the bound — both
are enforced in Python (`phase_coordinator.py:_implement_loop`).

### Stuck-loop detection

Three independent counters run inside the loop to catch the agent
spinning without making progress:

| Detector | Threshold | Behavior |
|---|---|---|
| Consecutive passes with zero file edits | `_STUCK_NO_DIFF_PASSES` (3) | Abort the loop after N passes that produce no `git diff` |
| Cumulative tool calls across zero-diff passes | `_STUCK_MAX_TOOL_CALLS_NO_DIFF` (50) | Abort when the agent burns calls without producing a single file change |
| Identical tail-call repetition | `_STUCK_SAME_TOOL_WINDOW` (5) | Detect when the last N tool calls in a pass are the same non-progress call (e.g. repeated `read_ticket`) |

A pass that produces at least one file mutation resets all counters.

### Agent-level selection

Before each edit pass, the stage may select a cheaper model tier:

- **Level 0** (no LLM at all) — rename-only changes.
- **Level -1** (no LLM at all) — spec-exact-code tickets (fenced code
  blocks with file paths matching existing files; edits applied
  deterministically).
- **Level 1** (flash model) — no-change-needed re-checks, or
  config/docs-only tickets.
- **Level 2** (default, full model) — all other tickets.

## The resume path

When a previously-blocked ticket re-enters `READY`:

1. **Skip re-clone** — if the ticket workspace already holds the
   clone and its feature branch, the stage checks out the branch and
   continues from the committed WIP rather than re-cloning.

2. **Conversation state replay** — the agent's conversation state
   (from the prior pass) is loaded and replayed so the agent resumes
   where it left off rather than restarting from scratch.  If the
   pause was a user-facing `ask_user` question, replies are collected
   and a compact resume history is constructed.

3. **Stale-spec guard** — the `implement.md` artifacts file carries a
   spec fingerprint; if the spec hasn't changed since the last
   `BLOCKED — resumable` outcome, the preflight rejects the re-spawn.

## Submodule breakdown

The implement stage source lives at `src/robotsix_mill/stages/implement/`
and is assembled from five responsibility-focused mixins:

| File | Lines | Responsibility |
|---|---|---|
| `_shared.py` | 732 | Module-level constants, regexes, the `_ImplementContext` and `_SinglePassResult` dataclasses, the package `log`, and stateless helpers |
| `_base.py` | 48 | Common base class `_ImplementStageBase` |
| `phase_coordinator.py` | 1,395 | Run-loop orchestration: preflight gates, `_implement_loop`, context loading, `_finalize` (stall detection + artifact persistence), pause logic |
| `validation.py` | 927 | Prerequisite gate, baseline CI check, scope guardrail (out-of-scope file classification via scope-triage), module-registration validation |
| `implementation_logic.py` | 903 | Agent invocation, single-pass execution, test/result evaluation, agent-level selection |
| `implementation_editing.py` | 588 | Special-case edit handlers: rename-only changes, spec-exact-code edits, repo-change verification, gitignore checks |
| `file_operations.py` | 411 | Clone/branch, multi-repo change detection, WIP commit |
| `core.py` | 36 | The assembled `ImplementStage` class — multiple-inherits the mixins and sets `name = "implement"` and `input_state = State.READY` |

The mixins never import each other or `core` — cross-responsibility
calls go through `cls`/`self` on the assembled class, keeping the
import graph a strict acyclic DAG.

## Configuration knobs

| Variable | Default | Description |
|---|---|---|
| `max_fix_iterations` | `8` | Maximum implement→test fix iterations before escalating to BLOCKED |
| `coordinator_max_tool_calls` | `300` | Hard cap on total tool calls per implement trace |
| `coordinator_timeout_seconds` | `600` | Wall-clock timeout (seconds) for a single implement pass |
| `coordinator_timeout_overrides` | `{}` | Per-stage timeout overrides (dict of stage → seconds) |
| `subtask_request_limit` | `30` | Per-subtask budget when the coordinator delegates via `spawn_subtask` |
| `test_request_limit` | `30` | Request budget for the test agent when diagnosing failures |
| `implement_max_spawns_per_ticket` | variable | Hard cap on total implement invocations per ticket; 0 = unlimited |
| `max_implement_review_cycles` | variable | Hard cap on implement↔review cycles; 0 = unlimited |

For the full config reference including env-var names and YAML paths,
see [docs/config/configuration.md](../config/configuration.md).

## Scope-triage sub-gate

When the agent's changes include files outside the ticket's declared
`file_map`, the **scope-triage** agent classifies each out-of-scope
addition as a legitimate expansion (`EXPAND`), scope creep (`REJECT`),
or an ambiguous case (`ESCALATE`).  This gate runs inside the
implement loop and can broaden the ticket's scope, strip rogue files
from the working tree, or escalate for human review.

When `scope_triage_enabled` is `false`, any out-of-scope file
immediately blocks the ticket.

See [docs/stages/scope-triage.md](scope-triage.md) for the full
classifier behavior, verdict table, REJECT cleanup logic, and
dedup guard.

## Cross-spawn stall guard

The stall guard detects when the implement agent's output is
byte-identical to the previous blocked attempt — a sign that
corrective review feedback is being ignored and the agent is stuck
in a convergence loop.

It works via two artifacts:

1. **`implement.md`** — the per-attempt outcome file, carrying
   `summary-fingerprint`, `stall-count`, and `spec-fingerprint`
   lines.  Written by `_finalize()` at the end of every attempt.

2. **`implement_stall_state.json`** — a standalone JSON file that
   mirrors the stall metadata and **survives `resume-blocked`**
   (which clears `implement.md` to reset the stale-spec guard).
   Without this file, an operator clearing the stale-spec guard
   would also zero the stall counter, letting a stuck ticket burn
   another round.

The guard is checked in **preflight** (before a trace opens) so a
stalled ticket never wastes a spawn slot.  The stall diagnostic
includes unanswered review-comment IDs and a recommended remedy
(re-scope, split, or hand-apply).

## See also

- [Scope triage](scope-triage.md) — out-of-scope file classifier
- [Merge stage](merge-stage.md) — downstream PR gating and auto-fix
- [Blocked ticket recovery](blocked-ticket-recovery.md) — resume-blocked workflow
- [docs/config/configuration.md](../config/configuration.md) — full config reference
- [docs/agents/index.md](../agents/index.md) — agent catalog
