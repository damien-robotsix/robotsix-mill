# Board-cleanup agent

The **board-cleanup** agent is a periodic read-only proposer that reviews the live kanban board and suggests hygiene actions — close stale/obsolete tickets, transition mis-stated ones, add clarifying comments, or relabel — for human approval via the Proposals panel.

## How it works

Once per day (by default), the agent:

1. **Fetches a snapshot** of recent board tickets (across all sources)
2. **Inspects tickets** using the `read_ticket` tool when needed for context
3. **Identifies issues** — stale tickets, incorrect state, ambiguity, missing labels
4. **Proposes actions** — emitted as structured `ProposedActionItem` records
5. **Awaits human approval** — each action lands in the Proposals panel for review

The agent **never directly modifies** tickets; it only emits proposals. An operator must approve or reject each one via the board UI.

## Action types

Each proposed action has a specific target ticket and one of four types:

### `close`
The ticket is obsolete or superseded — the work shipped elsewhere, the premise no longer holds, or evidence shows it's no longer needed.

**Payload:** Optional (null).  
**Rationale:** Cites concrete evidence from the ticket history or content.

**Example:**
> Ticket `abc1234` is titled "Add X feature" but a recent commit demonstrates the feature is already deployed. Close as superseded.

### `transition`
The ticket's recorded state doesn't match reality — it's marked active but its work merged long ago, or it's a draft but is actually blocked.

**Payload:** JSON string naming the correct target state (e.g. `"done"` or `"blocked"`).  
**Rationale:** Explains why the current state is wrong.

**Example:**
> Ticket `def5678` (titled "Refactor auth") is marked ACTIVE but its PR merged 2 weeks ago. Transition to DONE.

### `comment`
The ticket is too vague, ambiguous, or missing context to action — the agent wants clarity before proceeding.

**Payload:** JSON string with the clarifying question or note (e.g. `"Which database backend?"` or `"Can you add a test case example?"`).  
**Rationale:** Explains what's missing or unclear.

**Example:**
> Ticket `ghi9012` (titled "Optimize X") lacks performance metrics. Comment asking for baseline numbers.

### `relabel`
The ticket's labels don't match its content — mislabeled category, missing relevant label.

**Payload:** JSON string with a label list (e.g. `["bug", "performance"]`).  
**Rationale:** Explains what labels are wrong or missing.

**Example:**
> Ticket `jkl3456` is tagged `feature` but describes a security fix. Relabel to `security`.

## Configuration

### Enable / disable

The board-cleanup agent is **enabled by default**. To disable it:

```yaml
# config/mill.local.yaml
periodic:
  board_cleanup:
    enabled: false
```

Or via environment variable:

```sh
export MILL_BOARD_CLEANUP_PERIODIC=false
```

### Change the model

The agent defaults to a cheap flash model. To override:

```yaml
# config/mill.local.yaml
periodic:
  board_cleanup:
    model: deepseek/deepseek-v4-pro
```

Or via environment variable:

```sh
export MILL_BOARD_CLEANUP_MODEL=deepseek/deepseek-v4-pro
```

### Change the interval

The agent runs daily by default. To change to weekly or hourly:

```yaml
# config/mill.local.yaml
periodic:
  board_cleanup:
    interval_seconds: 604800  # 1 week
```

Or via environment variable:

```sh
export MILL_BOARD_CLEANUP_INTERVAL_SECONDS=604800
```

### Custom memory ledger path

By default, the agent stores its memory ledger at `<data_dir>/<repo_id>/board_cleanup_memory.md`. To use a custom path:

```yaml
# config/mill.local.yaml
periodic:
  board_cleanup:
    memory_path: /path/to/custom/memory.md
```

Or via environment variable:

```sh
export MILL_BOARD_CLEANUP_MEMORY_PATH=/path/to/custom/memory.md
```

## De-duplication

The agent respects prior human decisions — when it proposes an action on a ticket and the human approves or rejects it, the agent will not re-propose the same action on the same ticket until the situation materially changes.

## Proposals panel

Proposed actions surface in the **Proposals panel** (📝 icon in the board toolbar) where you can:

- **Review** each action's rationale and evidence
- **Approve** to let the executor apply the mutation
- **Reject** to dismiss it (prevents re-proposal until circumstances change)

Approved actions are applied deterministically: closes post a note citing the agent rationale, transitions update state, comments post under the ticket, and relabels update the label list.

## Implementation notes

The agent uses two skills:

- **`board-read`** — read-only ticket inspection via `read_ticket` tool
- **`board-propose`** — structured-output contract for `ProposedActionItem`

It does NOT use repository tools (`read_file`, `explore`, etc.) because it operates on the board, not the code tree. A future variant could inspect changed files to spot drift, but the current pass is board-only.

## Troubleshooting

**Agent never runs:**
- Check that `MILL_BOARD_CLEANUP_PERIODIC` is not set to `false`
- Verify the interval is ≥ 60 seconds
- Check worker logs for permission errors

**Proposals never appear:**
- The agent may not be finding issues worth proposing (check memory ledger)
- Review the agent's memory at `<data_dir>/<repo_id>/board_cleanup_memory.md`
- Try manually triggering via CLI (when available)

**Same action re-proposed:**
- The situation changed enough that the agent considers it a new proposal
- Check the rationale — if different evidence is cited, it's legitimate
- You can still reject to reset the proposal counter
