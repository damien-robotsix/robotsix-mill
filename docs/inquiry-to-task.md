# Inquiry-to-task conversion

Convert an answered inquiry (the "ask") into an actionable task ticket
via an LLM helper agent.  Available as a board action button on
`kind="inquiry"` tickets.

## Overview

The ask-to-ticket feature lets an operator turn a Q&A exchange into a
concrete engineering task.  The inquiry's question + answer, along with
an optional operator comment, are fed to a lightweight LLM agent that
drafts a title and Markdown body for a new `kind="task"` ticket on the
same board.  A backlink comment is posted on the source inquiry for
traceability.

## User workflow

1. **Open the inquiry** on the board — the inquiry must be answered (the
   drawer shows the answer; the original question is preserved as the
   `question-original.md` artifact).
2. **Click "Convert to task"** in the action buttons row.  The button is
   gated to `kind="inquiry"` tickets by the frontend.
3. **Optionally add a comment** — free-form guidance for the LLM agent
   (e.g. "focus on the rate-limit aspect").
4. **The LLM drafts a task ticket** — a new ticket is created in `DRAFT`
   state and enters the normal pipeline (refine → implement → …).
5. **A backlink comment** is posted on the source inquiry (best-effort).

## API endpoint

### `POST /tickets/{id}/convert-to-task`

**Request body** (JSON, optional):

```json
{
  "comment": "string (optional operator guidance for the LLM agent)"
}
```

**Response**: `201 Created` — a `TicketRead` object for the newly
created task ticket.

**Error codes**:

| Code | Meaning |
|------|---------|
| 404 | Ticket not found |
| 409 | Ticket is not `kind="inquiry"` — only inquiries can be converted |
| 503 | LLM agent unavailable (no OpenRouter key configured, or agent raised `RuntimeError`) |

## Example

**Source inquiry** (`kind="inquiry"`):

> **Title**: Is the cost tracker thread-safe?
>
> **Question** (artifact): We track per-ticket cost in a SQLite table
> with concurrent writes from the implement, deliver, and merge stages.
> Does the current design handle concurrent access correctly?
>
> **Answer** (description): No — the cost tracker uses a single
> connection across threads without WAL mode.  Two concurrent writes to
> `cost_events` can trigger `SQLITE_BUSY`.  Fix: enable WAL mode on the
> cost DB, add a retry loop with exponential backoff, and wrap all
> writes in a `with lock:` context manager keyed by ticket id.

**Operator comment**: "include a regression test that exercises two
concurrent writes"

**Resulting task ticket** (`kind="task"`):

> **Title**: Make cost tracker thread-safe with WAL mode and per-ticket locking
>
> **Body**:
> The cost tracker (`src/robotsix_mill/cost_tracker.py`) uses a single
> SQLite connection across threads without WAL mode, causing
> `SQLITE_BUSY` on concurrent writes from the implement, deliver, and
> merge stages.
>
> **What to change:**
> - Enable WAL mode on the cost DB connection.
> - Add a retry loop with exponential backoff on write operations.
> - Wrap all cost-event writes in a per-ticket-id lock (threading.Lock
>   keyed by ticket id).
> - Add a regression test that fires two concurrent writes and asserts
>   both succeed without SQLITE_BUSY.
>
> **Constraints:** The fix must not change the cost table schema or the
> public query API.

## Limitations & edge cases

- **Inquiry-only.**  Only tickets with `kind="inquiry"` can be
  converted; any other kind returns 409.  The route does not gate on
  ticket state — the frontend button controls when conversion is
  available.
- **No repo grounding (web route).**  The `POST` route calls
  `run_ask_to_ticket_agent(repo_dir=None)` — the agent runs without
  `explore` / `read_file` / `list_dir` / `run_command` tools.  The draft
  is purely from the Q&A text and operator comment, not grounded in a
  codebase clone.  The agent definition YAML lists repo tools, but they
  are only attached when a `repo_dir` is provided (currently only the
  web route calls this function, and it passes `None`).
- **Answer required.**  The inquiry must be answered — the route reads
  the answer from the ticket description.  If the artifact
  `question-original.md` is absent, the ticket title is used as a
  fallback question, which produces a weaker draft.
- **Backlink is best-effort.**  A comment is posted on the source
  inquiry linking to the new task ticket.  If this comment write fails,
  the failure is logged but not surfaced to the caller — the conversion
  still succeeds.
- **No duplicate prevention.**  An inquiry can be converted multiple
  times, each producing a new task ticket.

## Related configuration

- **`MILL_ASK_TO_TICKET_MODEL`** — environment variable controlling the
  LLM model.  Default: `deepseek/deepseek-v4-pro`.  Overrides the
  `model` field in `agent_definitions/ask_to_ticket.yaml`.
- **`agent_definitions/ask_to_ticket.yaml`** — agent definition
  (system prompt, tools list, retries, web-knowledge toggle).

## Related files

- Route: `src/robotsix_mill/runtime/routes/_tickets.py` —
  `convert_to_task`
- Agent runner: `src/robotsix_mill/agents/ask_to_ticket.py` —
  `run_ask_to_ticket_agent`
- Agent definition: `agent_definitions/ask_to_ticket.yaml`
