# Proposed actions

Background agents (e.g. `health`, `audit`, `trace-review`) can *propose*
a mutation to a ticket instead of performing it directly. Each proposal
is recorded as a `ProposedAction` row with status `PENDING` and surfaces
in the board's **Proposals** panel, where an operator reviews it and
either approves (which runs it immediately) or rejects it (which runs
nothing). The point is human sign-off: an agent that wants to close,
transition, comment on, or relabel a ticket — but doesn't want to act
unilaterally — files a proposal and waits.

## The data model

A `ProposedAction` (`src/robotsix_mill/core/models.py`) carries the
fields an operator sees in the panel:

- `source` — the agent that proposed it (e.g. `"health"`, `"audit"`,
  `"trace-review"`).
- `target_ticket_id` — the ticket the mutation applies to.
- `action_type` — what kind of mutation (see `ActionType` below).
- `payload` — a JSON string whose schema varies by `action_type`
  (`None` for actions that need no extra data).
- `rationale` — free-text explanation from the agent.
- `status` — lifecycle state (see `ProposedActionStatus` below).
- `created_at` — when the proposal was filed.
- `decided_at` — when a human approved/rejected it (`None` while
  pending).
- `decided_by` — who decided (defaults to `"human"`).
- `failure_reason` — the execution error message. When an approved
  action fails it lands in `FAILED` with `failure_reason` set to the
  underlying error (surfaced in the Proposals panel / `mill action
  list`).

### `ActionType` — what each does on execution

- `close` (`CLOSE`) — transitions the target ticket to `CLOSED`.
- `transition` (`TRANSITION`) — moves the ticket to the state named in
  `payload["state"]`.
- `comment` (`COMMENT`) — adds `payload["body"]` as a comment on the
  ticket.
- `relabel` (`RELABEL`) — placeholder for retitling/labelling. Label
  infrastructure is not yet built, so on the operator approve path a
  RELABEL deterministically lands in `FAILED` with
  `failure_reason="label infrastructure not yet available"`.

### `ProposedActionStatus` — lifecycle

```
PENDING ──approve──> APPROVED ──execute──> EXECUTED
   │                                  └──> FAILED
   └──reject──> REJECTED
```

A proposal starts `PENDING`. Approving stamps it `APPROVED` and then
**immediately executes** it, ending in `EXECUTED` on success or `FAILED`
if the mutation could not be applied (with `failure_reason` set to the
underlying error). Rejecting moves it to `REJECTED` and runs nothing.

## The Proposals panel (board UI)

A `📝 Pending` button sits in the board toolbar header, next to the
`💰 Cost` button. Clicking it (`toggleProposals()`) opens the right-hand
drawer; it is mutually exclusive with the Runs and Cost panels (opening
one closes the others).

The panel shows **pending** proposals for the **currently selected
repo** only — it fetches
`GET /proposed-actions?status=pending&repo_id=<current>`. Proposals are
per-board, so if the repo selector is on "all repos" the panel asks you
to pick a single repo instead of aggregating.

Each pending proposal renders as a card showing the `source` badge, the
`action_type`, the clickable `target_ticket_id` (opens that ticket), the
`rationale`, the `created_at` timestamp, and the `status`, with
**Approve** and **Reject** buttons. The panel auto-refreshes about once
a second while open, so decisions made elsewhere disappear from the list
on the next tick.

## Approval / rejection workflow

Approving a proposal (`POST /proposed-actions/{id}/approve`) transitions
it `PENDING → APPROVED`, stamps `decided_at` / `decided_by`, and then
runs the action against the target ticket, ending in `EXECUTED` (or
`FAILED`, in which case the row's `failure_reason` carries the
underlying error message). Rejecting (`POST /proposed-actions/{id}/reject`)
transitions `PENDING → REJECTED` and executes nothing.

### Idempotency / re-decision guard

Only `PENDING` actions can be approved or rejected. Approving or
rejecting an already-decided action returns HTTP `400`, and execution
itself no-ops if the action is already `EXECUTED`. A double-click or a
repeated approve therefore cannot run the mutation twice.

## API endpoint reference

Routes live in
`src/robotsix_mill/runtime/routes/_proposed_actions.py`. All return the
full `ProposedAction` JSON (the list route returns an array); an unknown
id yields `404`, and approving/rejecting a non-`PENDING` action yields
`400`.

- `GET /proposed-actions` — list proposals, newest first. Optional
  `?status=` (e.g. `pending`, `approved`, `rejected`, `executed`,
  `failed`) and `?repo_id=` filters. When `repo_id` is omitted or
  `all`, results from every registered board (plus `meta`) are
  aggregated and re-sorted by `created_at` DESC.
- `GET /proposed-actions/{action_id}` — a single action by id. Optional
  `?repo_id=` disambiguates the board; when omitted the lead board is
  tried first, then the others. `404` on miss.
- `POST /proposed-actions/{action_id}/approve` — approve and execute.
  `404` on unknown id, `400` if not `PENDING`.
- `POST /proposed-actions/{action_id}/reject` — reject (no execution).
  `404` on unknown id, `400` if not `PENDING`.

## Common scenarios / troubleshooting

- **A proposal is stuck in `FAILED`.** Execution hit an error (e.g. the
  target ticket's state machine refused the transition). The row's
  `failure_reason` holds the underlying error message — read it in the
  Proposals panel or via `mill action list`.
- **The panel is empty.** It only shows `pending` proposals for the
  *current* repo. Switch to the right repo in the top-left selector, and
  remember that already-decided proposals don't appear here.
- **Approve / Reject returns `400`.** The action was already decided —
  most likely by another operator or browser tab. Refresh the panel
  (it auto-refreshes about once a second) to pick up the new status.

## See also

- [index.md](index.md) — documentation home
- [docs/approval-gate.md](approval-gate.md) — human approval gates after
  refine and before merge
- [docs/agents.md](agents.md) — agent catalog
