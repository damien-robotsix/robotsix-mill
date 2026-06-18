# Ticket provenance (`source` field)

Every ticket records which actor created it — a human user, the
retrospect agent, the audit agent, or a future emitter — in a free-form
`source` string field (default `"user"`).

The `SourceKind` enum (`robotsix_mill.core.models.SourceKind`) provides
convenience constants for the known source labels. The field itself
remains free-form so unknown future sources are accepted without changes.

| Source value | `SourceKind` member | Set by | Board badge |
|---|---|---|---|
| `"user"` | `SourceKind.USER` | `POST /tickets` (CLI `ticket new`, API, web) | blue **user** |
| `"retrospect"` | `SourceKind.RETROSPECT` | Retrospect stage when spawning an improvement draft | amber **retrospect** |
| `"audit"` | `SourceKind.AUDIT` | Audit agent when emitting a gap improvement draft | green **audit** |
| `"survey"` | `SourceKind.SURVEY` | Survey agent when filing a discovered-project draft | cyan **survey** |
| `"agent"` | `SourceKind.AGENT` | Agent-check or other meta-agents when emitting tickets | grey **agent** |
| `"ci"` | `SourceKind.CI` | (planned) Future CI monitor feature | grey |
| `"trace-health"` | — | Trace-health check when unsessioned traces detected | cyan **trace-health** |
| (any other) | — | Any future agent or emitter | grey |

The board renders a small coloured badge on every card. Fallback: if
`source` is missing or empty, the board treats it as `"user"`.

Stored in the `ticket` table as `source TEXT NOT NULL DEFAULT 'user'`.

## See also

- [index.md](index.md) — documentation home
