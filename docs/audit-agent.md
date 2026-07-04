# Audit agent

The audit agent is a **meta-audit** agent that proactively identifies
gaps in the repository's quality and security tooling coverage. It
reviews the repo against current web-sourced best practices, compares
findings against an agent-owned memory ledger, and emits concrete
improvement draft tickets — one per gap — that flow through the
existing pipeline.

## How it works

1. **Reads memory:** The agent reads its Markdown memory ledger
   (fixed path, not overridable: `<MILL_DATA_DIR>/<repo_id>/audit_memory.md`
   in multi-repo mode, `<MILL_DATA_DIR>/audit_memory.md` otherwise).
   Missing file → empty ledger, never fail.

2. **Web research:** Uses `web_research` to identify current best
   practices for repo quality/security coverage.

3. **Gap analysis:** Compares findings against the memory ledger to
   identify gaps NOT already recorded as proposed or done.

4. **Emits drafts:** For each specific, worthwhile gap, emits one
   improvement draft ticket (`source="audit"`) via the normal ticket
   pipeline.

5. **Updates memory:** Returns an updated memory ledger that the runner
   writes back verbatim.

Deduplication is the agent's responsibility via the memory ledger: it
will NOT re-emit a draft for a gap already recorded as proposed or done.

## Usage

**CLI:**
```sh
robotsix-mill audit              # summary output
robotsix-mill audit --json      # full JSON result
```

**API:**
```sh
curl -X POST http://localhost:8077/audit
```

**Web board:** Click the "Run Audit" button on the board page.

**Periodic polling (opt-in):**
```yaml
# In config/config.json:
periodic:
  audit:
    enabled: true
    interval_seconds: 86400  # 1 day
```

## Configuration

| Variable | Default | Description |
|---|---|---|
| `MILL_AUDIT_PERIODIC` | `false` | Enable periodic audit passes |
| `MILL_AUDIT_INTERVAL_SECONDS` | `86400` | Seconds between automatic audits |

The audit memory ledger path is fixed (not overridable): in multi-repo
mode, per-repo memory lives at `<data_dir>/<repo_id>/audit_memory.md`;
otherwise `<data_dir>/audit_memory.md`.

## Important notes

- The audit agent does **NOT** scan code itself — it's a meta-coverage
  agent that proposes tools/agents/checks.
- The audit agent does **NOT** edit the repo directly — its only output
  is draft tickets (and its own memory ledger).
- The agent does **NOT** hard-code a fixed list of dimensions — it
  chooses targeted scopes dynamically based on web research and repo
  analysis.
- All repo-side output is draft tickets that must go through the
  approval gate (`human_issue_approval` → `ready` → `implement`).
- In multi-repo mode, the audit agent runs independently for each
  registered repo — each with its own board, memory file, and Langfuse
  project.

## See also

- [index.md](index.md) — documentation home
- [docs/agents.md](agents.md) — agent catalog
- [docs/configuration.md](configuration.md) — full env-var reference
