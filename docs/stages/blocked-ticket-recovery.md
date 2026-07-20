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

  `mark_done` also refuses to close a ticket whose branch HEAD
  carries duplicate towncrier changelog fragments (more than one
  `changelog.d/<ticket-id>.xxx.md` file).  Remove the extra fragment
  and re-push, or resume the ticket first.

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

- [index.md](index.md) — documentation home
- [cli/usage.md](cli/usage.md) — full CLI command reference
- [docs/config/configuration.md](../config/configuration.md) — `MILL_STAGE_RETRY_*` settings
