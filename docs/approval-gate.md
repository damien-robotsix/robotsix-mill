# Approval gates

Robotsix-mill has two human approval gates: one for the refined spec
(before implementation) and one for the merge decision (before the
merge stage takes over).

## Spec approval (after refine)

By default (`MILL_REQUIRE_APPROVAL=true`), the refine stage transitions
tickets to `awaiting_approval` instead of `ready`. The pipeline pauses
until a human approves, giving you a chance to review the refined spec
before the implement stage starts. Approve via:

- **Web board:** click the "Approve" button on any card in the
  `awaiting_approval` column.
- **CLI:** `robotsix-mill ticket approve <id>`
- **API:** `POST /tickets/{id}/approve`

To run fully autonomous (refine → implement with no pause), set
`MILL_REQUIRE_APPROVAL=false`.

## Auto-approve triage

When `MILL_REQUIRE_APPROVAL=true` and `MILL_AUTO_APPROVE_ENABLED=true`,
a cheap, conservative LLM check inspects the refined spec **after**
refinement. If the spec is precise, unambiguous, and free of genuine
design or architecture decisions — regardless of how many files are
touched or whether logic changes — the ticket skips the human gate
and transitions straight to `READY`. When the triage returns
`NEEDS_APPROVAL` (or on any error), the ticket proceeds to
`HUMAN_ISSUE_APPROVAL` as usual.

This gives operators a middle ground between approving every ticket
(toil) and disabling the gate entirely (risk). The triage is **biased
conservative**: when unsure whether a genuine design decision exists,
it defers to the human.

The model used for triage is controlled by `MILL_AUTO_APPROVE_MODEL`
(default: `openai/gpt-4o-mini`). Only the refined spec text is
inspected — no git diff, no repo exploration.

Auto-approved tickets record `"auto-approved: <reason>"` in their
event trail so operators can audit which tickets were auto-approved
and why.

## MR approval (before merge)

After the implement stage completes and a PR exists, the merge stage
may return the ticket to `human_mr_approval` (e.g. after a successful
rebase). The ticket waits for an explicit human go-ahead. Two paths are
available on the board and via the API:

### Approve (delegate to auto-merge)

Use when auto-merge is eligible (CI is green, `auto_merge_enabled` is
true, and the merge stage has written a `review.md` approving the PR).

- **Web board:** click the green **Approve** button on the card.
- **API:** `POST /tickets/{id}/approve-mr`

The ticket moves to `waiting_auto_merge`, where the merge stage picks it
up on the next poll and calls the forge's merge endpoint automatically.

### Merge (merge directly via forge)

Use when auto-merge is **not** eligible — for example because
`auto_merge_enabled` is `false`, or because the ticket bypassed code
review and has no `review.md`. This calls the forge's merge API
immediately, identical to clicking "Merge pull request" on GitHub.

- **Web board:** click the green **Merge** button on the card, or in the
  ticket-detail drawer.
- **API:** `POST /tickets/{id}/merge-now`

On success the ticket transitions directly to `done` and retrospect runs.
If the forge rejects the merge (branch protection, conflicts, etc.), the
endpoint returns 409 and the ticket remains in `human_mr_approval`.

The drawer also calls `GET /tickets/{id}/merge-reason` to display an
amber annotation explaining *why* auto-merge is ineligible when it is.

## See also

- [index.md](index.md) — documentation home
- [docs/configuration.md](configuration.md) — full env-var reference
