# Blocked ticket recovery

When a ticket is blocked (e.g. a fatal agent failure, or a transient
error that exhausted all retries), the state it was blocked *from* is
recorded. You can recover in two ways:

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

## Retrying tickets

Transient infrastructure errors (git outages, provider 503s, connection
refused) are retried automatically with exponential backoff — the ticket
stays in its current workflow state and the worker polls it after the
backoff delay.

### Identifying retrying tickets on the board

A retrying ticket displays an amber **retry chip** on its board card:

```
┌──────────────────────────────────────────┐
│  ✕                                       │
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
- [configuration.md](configuration.md) — `MILL_STAGE_RETRY_*` settings
