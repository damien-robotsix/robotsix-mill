# Implement stage: agent edit → test gate → deliver

The implement stage is the core pipeline stage — the robot reads the
ticket spec, clones the repo, runs an LLM coding agent to make changes,
runs the test gate, and either delivers the result or escalates to a
human. It is the most complex stage in the mill: 9 source files
(~5,100 lines) and 8 test files backing a multi-pass, multi-guardrail
fix loop.

## Overall lifecycle

```
READY
  │
  ├── Validation gates (prerequisite, baseline, scope)
  │
  ▼
Implement fix loop
  │
  ├── Agent edit pass (LLM makes changes)
  ├── Test gate (run test command + smoke checks)
  ├── Routing:
  │     ├── Pass → DELIVERABLE
  │     ├── Retry (under max_fix_iterations) → loop
  │     └── Exhausted / stuck / failed → BLOCKED (resumable)
  │
  ▼
DELIVERABLE  (or BLOCKED)
```

The stage accepts tickets in `READY` state and outputs `DELIVERABLE`
on success or `BLOCKED` on failure. Every `BLOCKED` exit is resumable:
the ticket can re-enter `READY` and the stage will pick up where it
left off without re-cloning.

## Pre-agent validation gates

Before the expensive LLM agent fires, several cheap gates run in order:

### Prerequisite gate

Parses the spec's `` ```prereq`` `` fenced blocks, imports them in a
sandboxed Python subprocess, and verifies every declared symbol is
resolvable. If a required module, class, or function is absent (e.g. an
unmerged external port), the ticket is short-circuited to `BLOCKED`
without ever invoking the LLM.

Controlled by `gates.prerequisite_gate_enabled` (default `true`).
Degrades gracefully on internal errors — always proceeds, never blocks
on checker bugs.

### Baseline check

Runs the test gate on the **base branch** (target) before the agent
loop starts. Results are cached at `artifacts/baseline_check.json`
keyed by the base-branch commit SHA.

- If the base branch itself has failing tests, the agent's work would be
  blocked by pre-existing failures it didn't introduce. The stage spawns
  a dedicated dependency-fix ticket and the current ticket escalates to
  `BLOCKED`.
- An idempotency guard prevents spawning duplicate fix tickets for the
  same pre-existing failure.

### Scope guardrail

Checks every file the agent touches against the ticket's declared
`file_map`. Two sub-paths:

- **Binary artifacts** are auto-cleaned from the tree.
- **Vendored/excluded paths** (gitignored files, known noise) are silently
  excluded.
- **Flood guard**: if out-of-scope text file count exceeds
  `scope_triage_max_files` (default 50), the ticket is blocked immediately.

When `scope_triage_enabled` (default `true`), the remaining
out-of-scope files are sent to the **scope-triage** LLM classifier,
which returns one of `EXPAND`, `REJECT`, or `ESCALATE`. See
[Scope triage](scope-triage.md) for the full classifier contract and
dedup guard against REJECT ping-pong.

### Preflight checks

Before the main agent trace opens (to avoid wasted Langfuse costs):

- **Epic guard**: ticket with no spec and a parent epic short-circuits.
- **Empty-spec guard**: ticket with an empty spec body blocks.
- **Spawn-limit guard**: prevents re-spawning subtasks from the same
  ticket beyond a threshold.
- **Cycle-limit guard**: caps total implement passes across all review
  rounds (see `max_implement_review_cycles`).
- **Cross-spawn stall guard** (see below).

## The fix iteration loop

Once the pre-agent gates pass, the stage enters a bounded fix loop:

```
┌─────────────────────────────┐
│  Edit pass                  │
│  (LLM agent makes changes)  │
└──────────┬──────────────────┘
           ▼
┌─────────────────────────────┐
│  Test gate                  │
│  (run test command +        │
│   path-scoped smoke gate)   │
└──────────┬──────────────────┘
           ▼
┌─────────────────────────────┐
│  ValidationResult.decide    │
│  ├── Pass → exit loop       │
│  ├── Retry → next iteration │
│  └── Escalate → BLOCKED     │
└─────────────────────────────┘
```

### Edit pass

The LLM coding agent (`run_implement_agent`) receives the ticket spec,
full filesystem access to the cloned repo, and optional feedback from
prior iterations. It uses a coordinator pattern: a top-level agent
delegates to sub-agents (`explore`, `spawn_subtask`) to understand the
codebase and make changes.

The agent runs inside an isolated sandbox container. It has no network
access to the internet, no forge credentials, and no ability to push
or open PRs. Changes are made to the local clone only.

**Agent-level bypass:** for tickets whose spec is purely mechanical
(rename-only, config/docs-only, `no_change_needed` re-checks), the
stage skips the full LLM coordinator and uses a cheaper direct path:
- `level=0`: rename-only changes
- `level=-1`: spec-exact-code tickets (fenced code blocks with paths)
- `level=1`: no-change-needed re-checks, config/docs-only

### Test gate

After the agent produces a diff, the stage runs the project's test
command inside the sandbox. The test command is resolved with
precedence: per-repo `.robotsix-mill/config.yaml` →
`repos.yaml` per-repo entry → global `test_command` setting.

When `test_command` is empty (or missing at all three layers), the
test gate is skipped and the agent pass is treated as a passing test
run. This supports repos without an automated test suite while keeping
the implement loop structure intact.

After the full test run, a **path-scoped smoke gate** runs: it checks
that every modified Python file parses and passes `ruff` format
checks. This catches syntax errors and formatting regressions cheaply
before the full test suite runs.

### Routing

`ValidationResult.decide(passed, iterations, max_iters, feedback)`
determines the next action:

| Condition | Action |
|---|---|
| Tests pass | Exit loop → DELIVERABLE |
| Tests fail, iterations remaining | Retry with feedback (diff, test output, diagnostics) |
| Tests fail, iterations exhausted | BLOCKED (resumable) |
| Agent exhausted budget (AgentBudgetError) | BLOCKED (resumable, conversation state persisted) |
| Agent error (AgentRunError) | Retry with backoff (transient) or BLOCKED (persistent) |

The loop is bounded by `max_fix_iterations` (default 8). Each iteration
gets a fresh request budget (`coordinator_requests`, default 500) and
tool-call cap (`coordinator_max_tool_calls`, default 300). A per-pass
wall-clock timeout (`coordinator_timeout_seconds`, default 600 s) caps
worst-case stuck-loop burn.

## Stuck-loop detection

Before each retry, the stage checks three cross-pass signals:

1. **Zero-change passes** (`_STUCK_NO_DIFF_PASSES = 3`): three
   consecutive passes with an empty git diff → the agent is producing
   no work. The stage emits a "zero-change stuck loop" diagnosis and
   blocks.

2. **Cumulative tool calls without diff** (`_STUCK_MAX_TOOL_CALLS_NO_DIFF
   = 50`): the agent has consumed 50 tool calls across passes without
   a single file change. This catches agents that are busy reading and
   reasoning but never editing.

3. **Identical non-progress tool calls** (`_STUCK_SAME_TOOL_WINDOW = 5`):
   the tail of the tool-call log is a repeating pattern of the same
   non-progress operation (e.g. re-reading the same file).

Additionally, a **diagnostic-history circuit breaker** watches for the
same diagnosis being emitted across multiple iterations. If the agent
produces the same diagnosis (e.g. "test failure in module X") across
multiple passes without resolution, the loop is terminated early even
if `max_fix_iterations` hasn't been reached.

## Resume from BLOCKED

When a ticket re-enters `READY` from `BLOCKED`, the implement stage
detects the existing clone and skips re-cloning. It:

1. **Checks the clone is still viable** (repo exists, branch is
   checkoutable).
2. **Clears stale-spec guards**: the `implement.md` artifact is
   re-read and any stale blocker metadata is cleared.
3. **Persists stall state**: the `_transition_mixin` writes
   `implement_stall_state.json` from the `implement.md` metadata so
   the cross-spawn stall guard survives the resume-blocked cycle.
4. Re-enters the fix loop with the existing working tree and WIP
   commits intact.

This means a human-operator unblock (e.g. "the prerequisite port was
merged, try again") can resume from exactly where the agent left off
without re-doing work.

## Cross-spawn stall guard

A hidden guard checks for **no-progress BLOCKED→resume→BLOCKED cycles**.
It hashes the agent's summary fingerprint (SHA-256 truncated to 16 hex
chars) and compares it across consecutive blocked attempts.

- When the fingerprint is unchanged (the agent produced the same
  summary / same diagnosis), a `stall_count` counter increments.
- When `stall_count ≥ implement_stall_threshold` (default 2), the third
  attempt is blocked **before** opening a Langfuse trace — the ticket
  never reaches the LLM, saving cost.

The stall state persists to `artifacts/implement_stall_state.json` so
it survives the resume-blocked clears of `implement.md`. On a genuine
change (different summary fingerprint or actual file changes), the
counter resets.

| Setting | Env var | Default | Description |
|---|---|---|---|
| `stages.implement_stall_threshold` | `MILL_IMPLEMENT_STALL_THRESHOLD` | `2` | Consecutive no-progress BLOCKED cycles before stall guard fires. 0 disables. |

## Submodule breakdown

The implement stage is structured as a single `ImplementStage` class
assembled from four mixins plus a top-level core module:

| Module | Lines | Role |
|---|---|---|
| `phase_coordinator.py` | ~1,250 | Run-loop orchestration: the fix iteration loop, stuck-loop detection, cross-spawn stall guard, and the `_implement_loop` entry point. The largest single module. |
| `validation.py` | ~750 | Pre-agent gating: prerequisite check, baseline test-gate run, scope guardrail (including scope-triage invocation), flood guard, and binary-artifact cleanup. |
| `implementation_logic.py` | ~600 | Agent passes and test gate: invokes `run_implement_agent`, handles `AgentBudgetError`/`AgentRunError`, runs the test command, runs the smoke gate, and feeds results into `ValidationResult.decide`. Also contains `_select_agent_level` for the level-bypass logic. |
| `file_operations.py` | ~550 | Clone/branch setup and repo-change inspection: `_clone_and_branch`, `_any_repo_has_changes`, path-escape guards, formatter-reversion detection, and edit-claim diagnostics. |
| `core.py` | ~200 | Top-level entry point: `ImplementStage` class (`name="implement"`, `input_state=READY`) composed from the four mixins. Delegates orchestration to `phase_coordinator`; the core module is thin glue. |
| `_shared.py` | ~100 | Shared constants and types used across the mixins. |

The mixin pattern allows each concern (looping, validating, implementing,
file-ops) to be tested and read independently while the `core.py` façade
presents a single `Stage` subclass to the pipeline runner.

### Agent seam

The stage invokes agents through `coding.run_implement_agent()`, a
patchable seam that delegates to `coordinating.run_coordinator`
internally. Three additional seams (`run_test_agent`, `run_smoke_agent`,
`load_repo_smoke_paths`) are re-exported from the `__init__.py` module
for test mocking.

## Configuration knobs

| Setting | Env var | Default | Description |
|---|---|---|---|
| `core.limits.max_fix_iterations` | `MILL_MAX_FIX_ITERATIONS` | `8` | Max implement→test fix loop iterations |
| `core.limits.coordinator_requests` | `MILL_PER_PASS_REQUEST_BUDGET` | `500` | Per-pass request budget (resets each iteration) |
| `core.limits.coordinator_max_tool_calls` | `MILL_COORDINATOR_MAX_TOOL_CALLS` | `300` | Hard cap on total tool calls per pass |
| `core.limits.coordinator_timeout_seconds` | `MILL_COORDINATOR_TIMEOUT_SECONDS` | `600` | Wall-clock timeout per pass (seconds) |
| `core.limits.scope_triage_max_files` | `MILL_SCOPE_TRIAGE_MAX_FILES` | `50` | Max out-of-scope files before flood guard blocks |
| `core.limits.max_stuck_cycles` | `MILL_MAX_STUCK_CYCLES` | `3` | Re-entries without progress before BLOCK |
| `core.limits.stage_timeout_seconds` | `MILL_STAGE_TIMEOUT_SECONDS` | `2400` | Per-stage wall-clock timeout |
| `gates.max_implement_review_cycles` | `MILL_MAX_IMPLEMENT_REVIEW_CYCLES` | `10` | Ceiling on total implement passes across review rounds (0 disables) |
| `gates.scope_triage_enabled` | `MILL_SCOPE_TRIAGE_ENABLED` | `true` | Enable scope-triage LLM classifier |
| `gates.prerequisite_gate_enabled` | `MILL_PREREQUISITE_GATE_ENABLED` | `true` | Enable prerequisite symbol checker |
| `stages.implement_stall_threshold` | `MILL_IMPLEMENT_STALL_THRESHOLD` | `2` | Consecutive no-progress cycles before stall guard fires (0 disables) |
| `sandbox.test_command` | `MILL_TEST_COMMAND` | `""` | Test command (empty = skip, global fallback) |
| `pipeline.implement_memory_path` | `MILL_IMPLEMENT_MEMORY_PATH` | `None` | Override path for implement memory |

See [Configuration reference](../config/configuration.md) for the full
settings catalog, loading order, and precedence rules.

## State flow summary

```
READY
    │
    ├── Prerequisite gate: unmet symbol → BLOCKED (resumable)
    ├── Baseline check:   base-branch fails → BLOCKED (dependency ticket spawned)
    ├── Scope guardrail:   out-of-scope flood → BLOCKED
    ├── Scope triage:      REJECT → clean + retry
    │                      EXPAND → expand file_map + continue
    │                      ESCALATE → BLOCKED
    ├── Preflight:         epic/empty/spawn/cycle/stall → BLOCKED
    │
    ▼
Implement fix loop  ◄──────────────────────────────┐
    │ (per iteration)                               │
    ├── Agent edit pass                             │
    │     ├── AgentBudgetError → BLOCKED (resume)   │
    │     └── AgentRunError  → retry / BLOCKED      │
    ├── Test gate + smoke gate                      │
    ├── ValidationResult.decide                     │
    │     ├── Pass → DELIVERABLE                    │
    │     ├── Retry (remaining iters) → loop ───────┘
    │     └── Exhausted → BLOCKED (resumable)       │
    │                                               │
    ├── Stuck-loop detection (before each retry)    │
    │     ├── Zero-change passes ≥ 3 → BLOCKED      │
    │     ├── Tool calls no diff ≥ 50 → BLOCKED     │
    │     └── Diagnostic circuit breaker → BLOCKED  │
    │                                               │
    └── Cross-spawn stall guard                     │
          └── stall_count ≥ threshold → BLOCKED     │
                                                    │
BLOCKED (resumable)                                 │
    │ (operator clears blocker)                     │
    └── READY → resume (no re-clone) ───────────────┘
```

## See also

- [Configuration reference](../config/configuration.md) — full env-var reference
- [Scope triage](scope-triage.md) — out-of-scope file classifier
- [Merge stage](merge-stage.md) — CI-fix, rebase, review-revision
- [Agent catalog](../agents/index.md) — agent definitions and toolchains
- [Blocked ticket recovery](blocked-ticket-recovery.md) — how blocked tickets are recovered
