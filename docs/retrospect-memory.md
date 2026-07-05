# Retrospect memory

The retrospect agent maintains a single Markdown file — a living ledger
of issues observed across tickets. Each retrospect run:

1. Reads the current memory (empty if missing).
2. Passes it to the agent, which analyses the ticket in light of the
   memory, updates the ledger, and decides whether any tracked issue now
   has enough corroboration to file an improvement draft.
3. Writes the agent's updated memory back verbatim (skipped when the memory
   is unchanged — the agent returns an empty `updated_memory`).

Deduplication is the agent's responsibility: it records when it has
already filed a draft for an issue and does not re-file.

Configure via `MILL_RETROSPECT_MEMORY_PATH` (defaults to
`<MILL_DATA_DIR>/retrospect_memory.md`).

## See also

- [index.md](index.md) — documentation home
- [docs/configuration.md](configuration.md) — full env-var reference
- [docs/agents/index.md](agents/index.md) — agent catalog
