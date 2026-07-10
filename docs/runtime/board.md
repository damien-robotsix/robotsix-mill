# Board

The robotsix-mill runtime serves an interactive Kanban board at `/board`
that displays the ticket lifecycle across 22 automated pipeline columns.

## Column Automation

**Each column is an automated pipeline stage, not a manual category.**
Tickets move through columns automatically via agent workflows and the
`TicketService`, never via manual user action. The system enforces
workflow rules during state transitions to keep tickets in a consistent
state with their stage logic.

The board adapter (`runtime/board_adapter.py`) maps the internal
`State` enum to board columns, and the broadcaster
(`runtime/broadcaster.py`) pushes state-change events to connected
board WebSocket clients so the UI updates in real time without polling.

## Why No Manual Card Movement?

You cannot manually move tickets between columns. The "move to" dropdown
control is intentionally hidden. This is necessary because:

1. **Workflow gates** — stage transitions are gated on prerequisite
   conditions (e.g., approval before implement).
2. **Stage setup logic** — each stage runs hook scripts, manages
   conversation state, and handles resume-from-pause.
3. **Safety guarantees** — manual movement would bypass these
   protections and leave a ticket in an inconsistent state relative to
   its agents' expectations.

If you need to override a ticket's state, use the CLI:

```sh
robotsix-mill ticket state <id> <new-state>
```

The CLI respects all workflow rules and ensures the transition is safe.

## Board HTML

The board page is built from two layers:

- **`board_html.py`** — server-renders the skeleton HTML (column
  headers, empty lanes) on first load so the page is immediately
  legible even before the WebSocket delivers card data.
- **`static/board.js`** — the client-side JavaScript that connects via
  WebSocket, receives card broadcasts, and renders cards into the
  correct columns. All DOM manipulation lives here, not inline in
  Python strings.

The board is served as a static page via FastAPI's `StaticFiles` mount
at `/board/`, with the skeleton HTML delivered by a dedicated route
handler.

## Learning More

See [docs/agents/index.md](../agents/index.md) for the complete agent
catalog and stage-by-stage lifecycle breakdown.
