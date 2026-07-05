# Board Operations

## Column Automation

The robotsix-mill board displays 22 columns representing the ticket lifecycle — from draft through delivery and merge. **Each column is an automated pipeline stage, not a manual category.**

Tickets move through columns automatically via agent workflows and the `TicketService`, never via manual user action. The system enforces workflow rules during state transitions to keep tickets in a consistent state with their stage logic.

## Why No Manual Card Movement?

You cannot manually move tickets between columns. The "move to" dropdown control is intentionally hidden. This is necessary because:

1. **Workflow gates** — stage transitions are gated on prerequisite conditions (e.g., approval before implement).
2. **Stage setup logic** — each stage runs hook scripts, manages conversation state, and handles resume-from-pause.
3. **Safety guarantees** — manual movement would bypass these protections and leave a ticket in an inconsistent state relative to its agents' expectations.

If you need to override a ticket's state, use the CLI:

```sh
robotsix-mill ticket state <id> <new-state>
```

The CLI respects all workflow rules and ensures the transition is safe.

## Learning More

See [docs/agents/index.md](agents/index.md) for the complete agent catalog and stage-by-stage lifecycle breakdown.
