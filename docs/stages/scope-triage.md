# Scope-triage: out-of-scope file classifier

The **scope-triage** agent is a cheap gate that runs during the implement stage when the agent's changes include files outside the ticket's declared scope (the `file_map`). It classifies out-of-scope additions as legitimate expansions, scope creep, or uncertain cases.

## Verdict types

| Verdict | Meaning | Action |
|---------|---------|--------|
| **EXPAND** | The out-of-scope changes are legitimate and necessary. | The ticket's `file_map` is expanded to include the new files; the implement loop continues normally with the broadened scope. |
| **REJECT** | The changes are scope creep and unrelated to the core ticket. | The rejected files are **removed from the working tree** (both unstaged and WIP-committed changes) before the next iteration. The ticket returns to READY, allowing the operator to adjust the spec or resume the agent with the polluted scope cleaned. |
| **ESCALATE** | The triage is uncertain or the agent encounters an error. | The ticket is blocked and escalated for human review. |

## On REJECT: cleanup behavior

When scope-triage returns **REJECT**, the implement stage removes the rejected files from the repository working tree and WIP-committed history **before committing and moving to the next state**. This cleanup ensures:

1. **Resumed runs start from the spec'd scope only** — if the ticket is moved back to READY and the agent resumes, it begins from a clean tree matching the original `file_map`. No pollution from prior out-of-scope attempts is carried forward.

2. **Both unstaged and WIP-committed pollution are cleaned** — it doesn't matter whether the rejected changes are unstaged edits or already committed to the WIP branch; both are reverted:
   - Tracked files modified: restored to their `origin/<target_branch>` version (via `git checkout origin/<target> -- path`)
   - New files: removed from the index (`git rm`) and deleted from disk

3. **No silent shipping of rejected scope creep** — prior versions of the implement stage had a dedup guard that, when the agent re-created a file a prior REJECT had already cleaned, would implicitly add it to `file_map` and ship it anyway. This is no longer permitted. If a file is re-created after rejection, it is cleaned again, and the ticket is re-blocked (not auto-promoted to READY with the rejected file added to scope).

## The dedup guard: preventing REJECT ping-pong

If the agent re-creates files that were already rejected in a prior run (detected by scanning history events for prior "scope-triage REJECT" verdicts), the implement stage does NOT emit another REJECT event (which would ping-pong forever). Instead, it:

1. Cleans the re-created files from the tree again
2. Skips the iteration and falls through to the test gate, allowing in-scope work to make progress
3. Does NOT add the re-created files to `file_map`

This prevents the loop from stalling while still refusing to ship previously-rejected scope creep without an explicit EXPAND verdict.

## Configuration

| Knob | Env var | Default | Purpose |
|------|---------|---------|---------|
| `core.models.scope_triage` | `MILL_SCOPE_TRIAGE_MODEL` | `deepseek/deepseek-v4-flash` | Model selection for the classifier |
| `gates.scope_triage_enabled` | `MILL_SCOPE_TRIAGE_ENABLED` | `true` | Enable/disable the scope-triage gate |

When `scope_triage_enabled` is **false**, any out-of-scope files immediately block the ticket; the LLM classifier does not run.

## See also

- [agents/index.md](agents/index.md) — Agent catalog and definitions
- [docs/config/configuration.md](config/configuration.md) — Full config reference
- [AGENT.md](../AGENT.md) — Conventions and guidelines for agents
