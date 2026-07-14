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

## Default-on GC: pruning terminal-ticket clones

`prune_clone_on_close` is best-effort and only fires on the retrospect
path, so clones leak: tickets that end terminal without that path,
multi-repo `repos/` trees (meta tickets), and workspaces orphaned by
restarts. The data-dir GC pass closes the gap with a backstop GC
that runs at the start of each pass.

| Variable | Default | Description |
|---|---|---|
| `MILL_DATA_DIR_GC_PRUNE_TERMINAL_CLONES` | `true` | Prune `repo/` + `repos/` inside terminal-ticket workspaces during the GC pass |
| `MILL_DATA_DIR_GC_PRUNE_TERMINAL_CLONES_AGE_SECONDS` | `86400` (1 day) | Minimum age (since terminal state) before clones are pruned |

Only the reproducible git clones are removed; `description.md`,
`artifacts/` and `screenshots/` are always preserved for post-mortems.
Eligibility mirrors the prune-closed GC below: the ticket must exist,
be in a terminal state (`CLOSED`, `EPIC_CLOSED`, `ANSWERED`), and have
been terminal for at least the configured age. Best-effort per board —
failures are logged, never raised.

## Opt-in GC: pruning closed-ticket workspaces

The per-ticket `prune_clone_on_close` above only removes the `repo/`
clone; the rest of a closed ticket's workspace (`description.md`,
`artifacts/`, …) accumulates on disk. Over time the bulk of `.data/`
bytes belong to **terminal-state** tickets whose workspaces are no
longer needed. The periodic data-dir GC pass can garbage-collect
them.

| Variable | Default | Description |
|---|---|---|
| `MILL_DATA_DIR_GC_PRUNE_CLOSED` | `false` | Prune workspaces of terminal-state tickets during the data-dir GC pass |
| `MILL_DATA_DIR_GC_PRUNE_CLOSED_AGE_SECONDS` | `604800` (7 days) | Minimum age (since terminal state) before a workspace is eligible |

When `MILL_DATA_DIR_GC_PRUNE_CLOSED` is `true`, the data-dir GC
pass runs a GC step at the start of each pass. It removes the workspace
directories of tickets in a terminal state (`CLOSED`, `EPIC_CLOSED`,
`ANSWERED`) whose close time is older than
`MILL_DATA_DIR_GC_PRUNE_CLOSED_AGE_SECONDS`. Close time is derived from
the most recent terminal `TicketEvent` (falling back to the ticket-ID
timestamp). Recent closures are kept so they remain available for
post-mortems.

Pruning is **opt-in** (default `false` for one release cycle) and
**best-effort**: a failed delete is logged and never aborts the pass,
and a board whose DB is unreachable is skipped without failing the rest
of the pass. Non-terminal tickets (including `DONE`) and orphan
directories (no matching ticket row) are never touched — orphans are
handled by the dedicated orphan pruning step.

## See also

- [index.md](index.md) — documentation home
- [docs/config/configuration.md](../config/configuration.md) — full env-var reference
