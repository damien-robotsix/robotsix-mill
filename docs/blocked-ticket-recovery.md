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

No raw database editing is ever needed to recover a blocked ticket.

Implemented in `service.py:resume_blocked` and `states.py:TRANSITIONS`.

## See also

- [README.md](../README.md) — project overview and quickstart
