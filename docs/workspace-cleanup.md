# Workspace cleanup on close

When a ticket reaches the terminal `closed` state, its workspace's
`repo/` clone has served its purpose and can be deleted to reclaim
disk space. This happens automatically by default — configure with:

| Variable | Default | Description |
|---|---|---|
| `MILL_PRUNE_CLONE_ON_CLOSE` | `true` | Delete `repo/` when ticket closes |

When `true` (the default), the `repo/` directory is removed right before
the ticket transitions to `closed`. The `description.md` and the entire
`artifacts/` tree (including `retrospect.md`, `implement.md`, etc.) are
left intact.

This is a **best-effort** operation — if deletion fails (e.g. permission
error), the ticket still reaches `closed` and the error is logged but
never raised.

Set to `false` if you need to inspect the final repository state after a
ticket is finished (for post-mortem debugging).

## Opt-in GC: pruning closed-ticket workspaces during the data-dir audit

The per-ticket `prune_clone_on_close` above only removes the `repo/`
clone; the rest of a closed ticket's workspace (`description.md`,
`artifacts/`, …) accumulates on disk. Over time the bulk of `.data/`
bytes belong to **terminal-state** tickets whose workspaces are no
longer needed. The periodic data-dir audit pass can garbage-collect
them.

| Variable | Default | Description |
|---|---|---|
| `MILL_DATA_DIR_AUDIT_PRUNE_CLOSED` | `false` | Prune workspaces of terminal-state tickets during the data-dir audit pass |
| `MILL_DATA_DIR_AUDIT_PRUNE_CLOSED_AGE_SECONDS` | `604800` (7 days) | Minimum age (since terminal state) before a workspace is eligible |

When `MILL_DATA_DIR_AUDIT_PRUNE_CLOSED` is `true`, the data-dir audit
pass runs a GC step **at the start of each pass, before size
measurement**. It removes the workspace directories of tickets in a
terminal state (`CLOSED`, `EPIC_CLOSED`, `ANSWERED`) whose close time is
older than `MILL_DATA_DIR_AUDIT_PRUNE_CLOSED_AGE_SECONDS`. Close time is
derived from the most recent terminal `TicketEvent` (falling back to the
ticket-ID timestamp). Recent closures are kept so they remain available
for post-mortems.

Pruning is **opt-in** (default `false` for one release cycle) and
**best-effort**: a failed delete is logged and never aborts the pass,
and a board whose DB is unreachable is skipped without failing the rest
of the pass. Non-terminal tickets (including `DONE`) and orphan
directories (no matching ticket row) are never touched — orphans are
left to the existing orphan detector.

Because the GC runs before measurement, every oversized/growth alert the
pass files reflects the **post-GC** state. **Operator playbook:** with
pruning enabled, an oversized/growth data-dir alert now means **real
residual growth** — live tickets, large artifacts, or non-terminal
accumulation — rather than churn residue from already-closed tickets, so
it warrants investigation instead of a routine acknowledge.

## See also

- [index.md](index.md) — documentation home
- [docs/configuration.md](configuration.md) — full env-var reference
