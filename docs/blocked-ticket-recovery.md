# Blocked ticket recovery

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

- **Mark as done** (abandon the ticket from any non-terminal state):
  ```sh
  robotsix-mill ticket mark-done <id> --note "abandoned: no longer needed"
  ```
  or via API:
  ```
  POST /tickets/{id}/mark-done  {"note": "abandoned: no longer needed"}
  ```
  Transitions *any* non-terminal ticket directly to `DONE`, bypassing
  the state machine's `can_transition()` rules.  This is an escape
  hatch for stuck tickets (BLOCKED, ERRORED, etc.) or tickets that
  don't need the full pipeline.  Terminal states (DONE, CLOSED,
  ANSWERED, EPIC_CLOSED, EPIC_OPEN) are rejected with 409.

  Use the CLI or API — the board no longer exposes a dedicated button.

No raw database editing is ever needed to recover a blocked ticket.

Implemented in `service.py:resume_blocked`, `service.py:mark_done`,
and `states.py:TRANSITIONS`.

## See also

- [index.md](index.md) — documentation home
