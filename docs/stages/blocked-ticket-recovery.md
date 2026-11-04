# Blocked ticket recovery

When a ticket is blocked (e.g. a fatal agent failure, a transient
error that exhausted all retries, a stage that timed out, or a
missing/empty `file_map.json` from the refine stage), the state it
was blocked *from* is recorded. You can recover in three ways:

- **Resume to the originating state** (re-runs only the failed stage):
  ```sh
  robotsix-mill ticket resume-blocked <id>
  ```
  This transitions `BLOCKED → <blocked_from>` (e.g. `BLOCKED → DONE`
  to re-run retrospect, skipping implement and refine).

  When resuming back to `READY` (the implement stage), `resume-blocked`
  performs additional cleanup:
  - **Clears the stale-spec guard** (`artifacts/implement.md`) so the
    preflight fingerprint check does not instantly re-block the ticket
    on an unchanged spec.
  - **Clears cached artifacts** (`implement_summary.md`,
    `reference_files.json`, `implement_conversation_state.json`) so the
    next implement cycle starts with a blank context — the agent does
    **not** receive its own prior summary as the `<previous_attempt>`
    block, which was the root cause of byte-identical replay across
    consecutive blocked cycles.
  - **Preserves stall-detection state** in
    `artifacts/implement_stall_state.json` so the cross-spawn stall
    guard (which blocks after N consecutive byte-identical summaries)
    survives the artifact clear-out.  Without this continuity,
    resuming a ticket that hit the stall guard would silently reset
    the counter, allowing another N identical cycles before re-blocking.
  - **Resets the spawn counter** only when the ticket was blocked at
    the spawn limit (counter ≥ `implement_max_spawns_per_ticket`),
    giving the ticket a fresh budget.  Other blocked-from-READY tickets
    keep their counter intact.

- **Manual override** (re-runs the full downstream chain):
  - `BLOCKED → READY` (re-runs implement → deliver → merge → retrospect)
  - `BLOCKED → DRAFT` (re-runs refine → implement → ...)

  Use the generic transition endpoint or the board.

- **Answer that requires a code change** (ticket paused in
  `awaiting_user_reply` by an `[ASK_USER]` question): transition
  `AWAITING_USER_REPLY → READY` directly, or answer with
  `POST /tickets/{id}/answer` and `"requires_change": true`. The
  transition note (or the answer body) becomes the implementer's top
  feedback item, so the change is made instead of the review re-judging
  the unchanged diff.

- **Mark as done** (abandon the ticket from any non-terminal state):
  ```sh
  robotsix-mill ticket mark-done <id> --note "abandoned: no longer needed"
  ```
  or via API:
  ```
  POST /tickets/{id}/mark-done  {"note": "abandoned: no longer needed"}
  ```
  Transitions eligible non-terminal tickets directly to `DONE`,
  bypassing the state machine's `can_transition()` rules.  This is
  an escape hatch for stuck tickets (ERRORED, etc.) or tickets that
  don't need the full pipeline.  Terminal states (DONE, CLOSED,
  ANSWERED, EPIC_CLOSED, EPIC_OPEN) and BLOCKED are rejected with
  409 — a BLOCKED ticket must be resumed first (see **Resume to the
  originating state** above).

  Use the CLI or API — the board no longer exposes a dedicated button.

- **Migrate to another board** (the ticket was filed on the wrong
  board — its fix targets a different repo):
  ```
  POST /tickets/{id}/migrate  {"repo_id": "robotsix-llmio", "note": "fix targets the llmio wrapper"}
  ```
  Moves the ticket — body, history, comments, workspace — to the target
  board and lands it in `DRAFT` there, so that board's refine stage
  re-triages it with the right repo context. Repo-specific state is
  reset (branch, `repo/` clone, cached `baseline_check.json`).
  Allowed from `draft`/`ready`/`blocked`/`errored`;
  epics and parent-linked tickets are rejected.

No raw database editing is ever needed to recover a blocked ticket.

Implemented in `service.py:resume_blocked`, `service.py:mark_done`,
`service.py:migrate`, and `states.py:TRANSITIONS`.

## Common block reasons

### Refine-stage block: gitignored file_map paths

When a refine agent produces a spec whose `file_map` targets paths that
are gitignored in the repo (e.g. a manifest board whose `.gitignore`
carries `/src/*` for vcs-imported sub-repos), the refine stage blocks
the ticket with a note like:

```
refine produced a spec targeting gitignored path(s): `src/ros2/pkg/msg/Status.msg`.
This board cannot deliver changes there — the paths are vcs-imported / vendored
sub-trees (e.g. `/src/*` managed via repos.yaml), invisible to git. Re-scope the
spec to target git-tracked files in this repo (e.g. the manifest / repos.yaml and
the board's own sources), not the cloned workspace sources.
```

**Why it happens:** On manifest-style boards (e.g. ROS 2 workspace repos),
the `.gitignore` lists rules like `/src/*` to hide vcs-imported sub-repos
that are cloned and managed via `repos.yaml` at runtime. Writing to those
paths produces real files on disk, but they're invisible to git — so a
spec targeting them would land as untracked files, never enter the commit,
and surface as an opaque "no changes produced" failure at implement.

**How to fix it:**
1. Re-edit the ticket in `DRAFT` (use `robotsix-mill ticket transition <id> --to=DRAFT`
   or the board's transition button).
2. Edit the draft to remove vcs-imported scope — target git-tracked files instead
   (e.g. the manifest YAML, the board's own source code, configuration files).
3. Let refine run again — the new spec will be checked against the gitignore rules.

**Recovery option:** If re-drafting is not feasible, resume the blocked ticket
first (`resume-blocked`), then use `robotsix-mill ticket mark-done <id> --note
"abandoned: target paths are vcs-imported"` to close the ticket and start fresh.

### Infra account block on a private repo (App lacks `checks: read`)

GitHub refusing to start hosted jobs (failed payment / spending limit)
parks the ticket `BLOCKED` with a note starting `Infra account blocked`,
and the ci_fix agent is not run. Normally the billing annotation on the
check-run is the evidence. On a private repo, the robotsix-mill GitHub
App needs `Checks: read` and `Commit statuses: read` to read check-runs,
statuses and annotations. Without them those endpoints return 403. The
forge then logs one WARNING per repo naming the missing permission, and
the ticket still parks as an account block when every failing workflow
run has jobs that never started (no steps).

**What to do:** the mill raises a single operator escalation asking you
to add `Checks: read` and `Commit statuses: read` to the App and to accept
the new permissions on the installation. Until you do, a real code failure
on that private repo can be misread as an account block, so check the
WARNING and the run's jobs before resuming.

### Out-of-scope CI failure parked on a dependency fix

When the ci-fix agent classifies a red check as out of scope for the
ticket, the ticket parks `BLOCKED` on a `ci_fix_dependency` fix ticket
(auto-resumes when that fix reaches DONE). Before spawning a new fix, the
stage checks whether the most recent fix with the same failure fingerprint
already merged after the failing run started. If so, it does not spawn a
duplicate: it refreshes the branch when it is behind, otherwise re-runs
the failed workflows, and records a history note such as `out-of-scope fix
<id> already merged; re-running CI instead of re-spawning`. The ticket
returns to the CI poll. This re-check fires once per merged fix. A failure
that still recurs after a merge that predates the failing run spawns a new
fix ticket as usual.

### Recurring CI failure: prior fixes linked, prevent-recurrence required

A `ci_fix_dependency` fix ticket is keyed on a log-derived failure
signature in addition to the check-name fingerprint. Right now only npm
`ERESOLVE` peer-dependency conflicts are recognised, keyed as the failing
step plus the conflicting packages (e.g. `npm ci ERESOLVE
typescript/typescript-eslint`, with versions stripped). The signature is
stored as a `ci_sig:` label. When a new fix ticket carries a signature
that matches a `ci_fix_dependency` ticket on the same board that is closed
or done and was created within the last 90 days, the new ticket gets a
`## Recurrence` section. It links the prior ticket ids (and their PR
branches) and adds the requirement to prevent recurrence (e.g. a Dependabot
ignore/group or a commented version pin) in addition to fixing the
immediate failure. Failures with a different check name but the same
signature are still matched. Failures without a recognised signature
fall back to the check-name fingerprint only, as before.

### Gate added by the ticket's own diff is fixed in scope

When a ticket adds a CI gate (a lint, format or analyzer step under
`.github/workflows/`) and that gate fails on pre-existing debt, the
debt is part of the ticket: the gate cannot merge without it. Before
accepting an OUT_OF_SCOPE verdict, the ci-fix stage compares the failing
step's `run:` command with the lines the branch adds to
`.github/workflows/**` (`git diff origin/<target>...HEAD`). If the
command was added by this branch, the verdict is overridden to IN_SCOPE:
the agent is told the gate is in scope, the debt is fixed on the same
branch, and no `ci_fix_dependency` ticket is spawned. The history note
reads `ci-fix overrode OUT_OF_SCOPE → IN_SCOPE` and lists the command.

Any dependency fix that is spawned also records the toolchain versions the
workflow pins (for example `flutter-version` or `python-version`) in its
description under **CI toolchain pins**, so the fix uses the same
formatter or linter version as CI.

### ci_fix progress reports are notes, not tickets

Progress or completion reports from the ci-fix agent (titles such as
`Agent report: … pushed` or `DONE: … fixed`) are not filed as board
tickets. They are appended to the ticket being fixed as a history note
(`ci_fix status note: …`). Titles that describe a real problem, even if
they mention a status word (e.g. "lint still not fixed"), are still filed
as tickets. A genuine ci-fix follow-up whose normalised title repeats one
the same session filed within the last hour returns the existing ticket
instead of filing a duplicate.

### ci_fix completion and retract reports are notes, whatever the title

A ci-fix report is also treated as a note, not a ticket, when its body
reports work the run already pushed (e.g. `PUSH_OK`, "committed and
pushed", "pushed commit `651eb1c`") or retracts an earlier draft
(e.g. "previously filed", "inadvertently filed", "closing the previously
filed draft", "no action needed on this draft"). A retract-style title
(`Close …`, `Ignore …`, `Retract …`) also counts. Such a report is never
filed as a new draft, so the run does not create tickets describing
work it has already finished.

The note lands on the ticket ci_fix was invoked for. If the body names a
`mill/<ticket-id>` branch whose ticket exists on the board, the note goes
to that ticket instead, so its verification checks the cited commit
against its own branch rather than `origin/main`. If neither ticket can
be found, nothing is filed and the agent is told to return its structured
result instead.

### CI failure on a deleted workflow is not filed or is auto-closed

A failing run whose workflow file no longer exists at the target branch
HEAD (for example a `release.yml` replaced by release-please) cannot
reproduce, so the CI monitor does not file a `CI failure: <workflow> on
<branch>` ticket for it. The run is marked seen and skipped. The same
check runs in the ci auto-close pass and the stale-CI gate: an open
`source=ci` ticket whose workflow file is absent at branch HEAD is
closed to `DONE` with a note such as `workflow file <path> removed from
<branch> HEAD; failure no longer reproducible`, without a refine or
implement pass. The check only fires when the branch tree was read and
lacks the path; dynamic (non-file) workflow names and forge errors leave
the ticket untouched.

### Completion wording with a remaining problem is filed, not swallowed

A ci-fix report that uses completion wording (`committed and pushed`,
`pushed the fix`, a commit SHA) is a status note only when its body names
no remaining problem. Words such as `still`, `remaining`, `needs to be`,
`not fixed`, `but … fail`, a `startup_failure`, or a failure cause outside
the repo (a spending limit, or a failure in `robotsix-github-workflows`)
mark it as an escalation. Escalations go through the normal routing:
GitHub-side account/infra causes are not filed (the infra-block path owns
them), github-workflows failures go to that board, and repo failures are
filed as a draft. Retract and close-out wording is always a status note.

### Operator-only prerequisites go to the human gate, not auto-implement

The refine fast-path does not auto-approve a draft that asks for an
operator action or an operator-supplied fact. Implement cannot perform
these, so it would only ask the operator and block. A draft is held at
`HUMAN_ISSUE_APPROVAL` when any line matches an operator-only marker,
such as `Operator confirms`, `Repo owner`, `Do not guess`, `Settings →`,
`in the … control panel`, `(a settings change, not a file change)` or
`checked in repo settings`. The history note quotes the matched lines
with the prefix `operator prerequisite detected`.

A separate fail-closed rule applies when the fast-path classifier returns
`NEEDS_APPROVAL` and cites a path matching `auto_merge_sensitive_globs`
(for example `.github/workflows/**`). The ticket is held at
`HUMAN_ISSUE_APPROVAL` with the note `sensitive path cited`, and the later
auto-approve pass cannot downgrade it to `APPROVE`. Drafts with neither
signal keep the normal fast-path behaviour.

### Work already present at base is reported as "already satisfied"

When a ticket's goal is already met on the target branch, it closes
`DONE` with an `already satisfied` note instead of being implemented.
Two paths produce this close:

- **Refine deliverable-exists gate.** A remediation draft that names a
  file to create (e.g. `Create new test file: src/config-panel/collect.test.ts`)
  closes at refine when every such path already exists at the target
  branch HEAD. The note cites the base SHA and the commit that added
  each file. Implement is not run. User-authored drafts, meta-board
  tickets, and tickets that already carry a branch are never closed by
  this gate.
- **Implement empty-diff close.** An implement pass that finds nothing
  to change, with no lost work, closes `DONE` and records the base SHA
  (`no change needed (base <sha12>) — …` or `already satisfied — no
  changes needed (empty diff vs base) (base <sha12>)`).

In epic status and the board's delivery summary, these children are shown
as `satisfied — already met at base <sha12>`, not as
`closed (not delivered)`. Their goal is treated as met, so the epic does
not file a follow-up that re-asserts the same gap.

## Retrying tickets

Transient infrastructure errors (git outages, provider 503s, connection
refused) are retried automatically with exponential backoff — the ticket
stays in its current workflow state and the worker polls it after the
backoff delay.

### Identifying retrying tickets on the board

A retrying ticket displays an amber **retry chip** on its board card:

```
┌──────────────────────────────────────────┐
│  Fix login redirect                      │
│  abc123de                                │
│  user                      $0.0123      │
│  retry 3 · next in 2m                    │  ← amber chip
│  ⏺ implementing…                         │
└──────────────────────────────────────────┘
```

The chip shows the retry attempt count and the time until the next
automatic retry (computed from `next_retry_at`). Hovering over the chip
reveals the `last_transient_error` detail in a tooltip.

### Retrying immediately

To cancel the backoff and retry immediately, either:

- **Board drawer:** open the ticket and click the **Retry now** button.
- **CLI:**
  ```sh
  robotsix-mill ticket resume-blocked <id>
  ```

Both call the same `POST /tickets/{id}/resume-blocked` endpoint, which
clears the retry state and re-enqueues the ticket. This endpoint handles
both BLOCKED and retrying tickets.

When `retry_attempt` is 0 the retry chip and "Retry now" button are
absent — the board looks exactly as it does for a non-retrying ticket.

## See also

- [index.md](../index.md) — documentation home
- [cli/usage.md](../cli/usage.md) — full CLI command reference
- [docs/config/configuration.md](../config/configuration.md) — `MILL_STAGE_RETRY_*` settings
