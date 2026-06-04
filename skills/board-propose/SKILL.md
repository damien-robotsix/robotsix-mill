---
name: board-propose
---

## Board: proposing actions on existing tickets

You are a **read-only** agent: you never mutate tickets during a pass.
When you believe an existing ticket should be closed, transitioned,
commented on, or relabeled, you do not perform the change — you
**propose** it by emitting an entry in the `proposed_actions` list of
your structured output. Each proposal is recorded as a PENDING row and
is applied later **only after a human reviews and approves it**. There
is no `propose_action` tool to call; the mechanism is purely the
`proposed_actions` output field.

### Read-only contract

Emitting a proposal **never mutates a ticket**. The actual mutation is
gated behind human approval plus a deterministic executor that re-runs
the change through the normal ticket service. Proposing is a suggestion,
not an action.

### `proposed_actions` — one entry per suggested mutation

Each entry has these fields:

- `target_ticket_id` — the ID of the ticket the action applies to.
- `action_type` — one of exactly these four lowercase string values:
  - `close` — close the target ticket.
  - `transition` — move the target ticket to another state.
  - `comment` — add a comment to the target ticket.
  - `relabel` — change the target ticket's labels. **Known
    placeholder:** the executor currently fails `relabel` proposals
    with a clear message until label infrastructure lands. You may
    still propose it, but it will not execute yet.
- `payload` — a JSON string whose schema varies by `action_type`:
  - `transition` → the target state, e.g. `{"to_state": "closed"}`.
  - `comment` → the comment body, e.g. `{"body": "..."}`.
  - `close` → an optional reason, e.g. `{"reason": "..."}`.
  - `relabel` → the intended label change (placeholder; not yet
    executed).
- `rationale` — a short explanation of why the action is warranted, so
  the human reviewer can decide quickly.

`source` and `status` are set by the runner, not by you — do not try to
populate them.
