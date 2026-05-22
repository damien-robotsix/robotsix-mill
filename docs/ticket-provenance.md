# Ticket provenance (`source` field)

Every ticket records which actor created it — a human user, the
retrospect agent, the audit agent, or a future emitter — in a free-form
`source` string field (default `"user"`):

| Source value | Set by | Board badge |
|---|---|---|
| `"user"` | `POST /tickets` (CLI `ticket new`, API, web) | blue **user** |
| `"retrospect"` | Retrospect stage when spawning an improvement draft | amber **retrospect** |
| `"audit"` | Audit agent when emitting a gap improvement draft | green **audit** |
| `"trace-health"` | Trace-health check when unsessioned traces detected | cyan **trace-health** |
| (future) | Any future agent or emitter | grey |

The board renders a small coloured badge on every card. Fallback: if
`source` is missing or empty, the board treats it as `"user"`.

Stored in the `ticket` table as `source TEXT NOT NULL DEFAULT 'user'`.
An idempotent migration in `db.init_db` adds the column to existing
databases that lack it.

## See also

- [README.md](../README.md) — project overview and quickstart
