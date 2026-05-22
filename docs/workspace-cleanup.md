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

## See also

- [README.md](../README.md) — project overview and quickstart
- [docs/configuration.md](configuration.md) — full env-var reference
