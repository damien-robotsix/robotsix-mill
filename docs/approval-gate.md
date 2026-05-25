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
rebase). The ticket waits for an explicit human go-ahead before the
merge stage polls CI and auto-merges. Approve via:

- **Web board:** click the "Approve" button on any card in the
  `human_mr_approval` column.
- **API:** `POST /tickets/{id}/approve-mr`

This bypasses auto-merge eligibility checks — the human is making the
call. The ticket moves directly to `waiting_auto_merge`, where the merge
stage picks it up on the next poll.

## See also

- [index.md](index.md) — documentation home
- [docs/configuration.md](configuration.md) — full env-var reference
