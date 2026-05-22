# Approval gate

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
refinement. If the described change is "obviously safe" — cosmetic,
doc-only, formatting, single-file, no logic changes — the ticket skips
the human gate and transitions straight to `READY`. When the triage
returns `NEEDS_APPROVAL` (or on any error), the ticket proceeds to
`HUMAN_ISSUE_APPROVAL` as usual.

This gives operators a middle ground between approving every ticket
(toil) and disabling the gate entirely (risk). The triage is **biased
conservative**: when uncertain, it defers to the human.

The model used for triage is controlled by `MILL_AUTO_APPROVE_MODEL`
(default: `openai/gpt-4o-mini`). Only the refined spec text is
inspected — no git diff, no repo exploration.

Auto-approved tickets record `"auto-approved: <reason>"` in their
event trail so operators can audit which tickets were auto-approved
and why.

## See also

- [README.md](../README.md) — project overview and quickstart
- [docs/configuration.md](configuration.md) — full env-var reference
