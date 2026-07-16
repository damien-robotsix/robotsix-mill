# Audit agent

The audit agent is a **frontier orchestrator** that runs at level 2
and coordinates a team of sub-agent explorers to perform a deep,
structured audit of the repository. It supersedes the v1 single-pass
meta-audit and the v2 web-research-only model.

## How it works

1. **Frontier planning:** The orchestrator surveys the repository,
   reads AGENT.md (for standards opt-in), and decomposes the repo into
   3–7 audit subparts (source modules, tests, CI/workflows, docs,
   packaging/deploy, security posture, standards conformance).

2. **Shared run memory:** Creates a per-run memory artifact at
   `artifacts/audit-run-<run-id>/memory.md` that every sub-agent reads
   before starting and appends findings to after finishing. This is the
   single source of cross-subpart context for the whole run.

3. **Sub-agent fan-out:** For each subpart, spawns a dedicated
   sub-agent via `explore` or `parallel_explore`. Each sub-agent
   applies TWO lenses:
   - **Lens A — General health / maintainability:** oversized files,
     poor structure, low readability, dead code, copy-paste duplication,
     documentation gaps, test gaps, sync fragility.
   - **Lens B — Standards conformance:** verifies the subpart against
     robotsix-standards as declared via AGENT.md.

4. **Final synthesis:** After all sub-agents complete, the orchestrator
   reads the full shared memory, cross-references findings across
   subparts, cross-checks against the recent-proposals block (to avoid
   re-proposing), and emits concrete improvement draft tickets.

5. **Updates memory:** Merges durable observations back into the
   persistent memory ledger (`updated_memory`). The shared memory
   artifact is a working document; only general patterns and
   observations carry forward to future passes.

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
# In config/config.yaml:
periodic:
  audit:
    enabled: true
    interval_seconds: 604800  # 7 days (weekly)
```

## Configuration

| Variable | Default | Description |
|---|---|---|
| `MILL_AUDIT_PERIODIC` | `false` | Enable periodic audit passes |
| `MILL_AUDIT_INTERVAL_SECONDS` | `604800` | Seconds between automatic audits (7 days) |

The audit memory ledger path is fixed (not overridable): in multi-repo
mode, per-repo memory lives at `<data_dir>/<repo_id>/audit_memory.md`;
otherwise `<data_dir>/audit_memory.md`.

## Important notes

- The audit orchestrator runs at level 2 and spawns sub-agents at
  levels 1–2 depending on subpart complexity.
- Sub-agent runs are each traceable in Langfuse (named spans/traces per
  subpart).
- The orchestrator uses `write_file` to maintain the shared run memory
  artifact — one of the few periodic agents with filesystem write access.
- All repo-side output is draft tickets that must go through the
  approval gate (`human_issue_approval` → `ready` → `implement`).
- In multi-repo mode, the audit agent runs independently for each
  registered repo — each with its own board, memory file, and Langfuse
  project.

## See also

- [index.md](../index.md) — documentation home
- [agent catalog](index.md) — agent catalog
- [docs/config/configuration.md](../config/configuration.md) — full env-var reference
