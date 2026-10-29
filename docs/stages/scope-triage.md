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

## The EXPAND cap: split the overflow and keep the parent moving

Repeated EXPAND verdicts are the costliest churn pattern, so each ticket is allowed at most **2** EXPAND events. A further EXPAND hits the cap, and rather than blocking the parent, the implement stage:

1. Spawns a `scope split from <parent>` child ticket for the overflow (would-be-expanded) files.
2. Records a machine-parsable `scope-triage SPLIT` history event naming the overflow files (backticked) and the child id.
3. **Reverts the overflow changes from the parent's working tree** (the full out-of-scope set, via `git_ops.restore_paths`), leaving only the in-scope changes.
4. Falls through to the **test gate** — the parent is **NOT blocked**, so its in-scope work proceeds to PR delivery.

If the parent is later resumed and the agent re-creates the same overflow files, a repeat cap hit whose files were **all already covered by a prior `scope-triage SPLIT` event** restores the tree and proceeds **without spawning a duplicate child** (a dedup guard mirroring the REJECT dedup above). This makes `resume-blocked` productive for tickets that hit the cap before this behavior existed.

The only case that still blocks on the cap is a rare infrastructure failure where the child ticket could not be created: the parent is then gracefully blocked with a clear note, since its overflow work has nowhere to go.

## Operator resume notes after ESCALATE

The gate judges the spec and the diff only, so an ESCALATE used to repeat word for word after the operator resumed the ticket with a note confirming the file. Now, before triaging, the gate reads the operator's answer to the latest ESCALATE:

1. **Operator notes** are the `blocked → ready` resume notes (the `; override:` part of a `resumed from blocked` note counts) and operator/user comments newer than that ESCALATE. Automatic `[auto-resume …]` retries, and READY notes from other stages (review, rebase, ci-fix), are ignored.
2. **Accepted files:** a note that names an escalated file (full path or basename) accepts it, unless the note reads as a rejection (e.g. "revert", "scope creep"). A note that names no file but says "in scope", "accepted" or "confirmed" accepts every escalated file.
3. **Accepted files are expanded deterministically** and recorded as `scope-triage: \`<file>\` accepted by operator note <ts>`. They do not count against the EXPAND cap and do not reach the LLM.
4. **Anything else** is passed to the classifier as operator context. If the gate ESCALATEs again, the block note includes the operator's resume note (`re-escalated despite operator resume note …`), so the loop is visible. To proceed, put the decision in the spec.

## Missing-context ESCALATE: full-diff re-ask

An ESCALATE that blames missing context (for example, "truncated summary" or "cannot tell from the diff") is treated as a context gap, not a scope ambiguity. Before blocking, the gate re-asks the classifier **once** with each out-of-scope file's full diff against the branch base, capped at 400 lines per file. The re-ask is rendered as a `full-diffs` section in place of `diff-summaries`.

If the re-ask still ESCALATEs, the block note never repeats the "truncated" wording. It reads `scope still ambiguous after reviewing the full per-file diff`, so the operator sees the real reason.

## Spec-implied lockfiles

A file the spec names through a mechanical command is auto-EXPANDed, the same way a file the spec names verbatim is. For example, "run `uv lock`" implies `uv.lock`, and "npm install" implies `package-lock.json`. Other recognised commands are `poetry lock`/`add`/`remove` (`poetry.lock`), `cargo update` (`Cargo.lock`), `yarn` (`yarn.lock`), `pnpm install` (`pnpm-lock.yaml`) and `go mod tidy` (`go.sum`). Matching is on the file's basename, so `web/package-lock.json` counts too. These files never reach the classifier.

## Configuration

| Knob | Env var | Default | Purpose |
|------|---------|---------|---------|
| `core.models.scope_triage` | `MILL_SCOPE_TRIAGE_MODEL` | *(level 1)* | Model selection for the classifier |
| `gates.scope_triage_enabled` | `MILL_SCOPE_TRIAGE_ENABLED` | `true` | Enable/disable the scope-triage gate |

When `scope_triage_enabled` is **false**, any out-of-scope files immediately block the ticket; the LLM classifier does not run.

## See also

- [agents/index.md](../agents/index.md) — Agent catalog and definitions
- [docs/config/configuration.md](../config/configuration.md) — Full config reference
- [AGENT.md](https://github.com/damien-robotsix/robotsix-mill/blob/main/AGENT.md) — Conventions and guidelines for agents
