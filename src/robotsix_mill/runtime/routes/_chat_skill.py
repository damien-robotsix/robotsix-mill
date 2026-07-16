"""``GET /chat-skill`` — SKILL.md endpoint for the robotsix-chat agent.

Serves a skill document teaching the chat agent how to drive the board
API.  The text is versioned with the app (no detached doc) so it stays
in sync with the actual route definitions.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

router = APIRouter(tags=["ChatSkill"])

_CHAT_SKILL_TEXT = """\
---
name: mill-board
description: Drive the mill board API to read tickets, post comments, and manage ticket state transitions.
---

## mill-board — Chat Agent Skill

You are connected to a **robotsix/mill** board API.  Use the endpoints
below to read tickets, post comments, and manage state transitions.
All requests are relative to the board's base URL (no auth required —
the API is localhost-only).

### Base URL

The base URL is the same host and port used to serve this skill
document.  If you fetched the skill from `http://localhost:8077/chat-skill`,
use `http://localhost:8077` as the base.

---

## Reading tickets

### GET /tickets — list tickets

```
GET /tickets?state=<state>&include_closed=<bool>&repo_id=<repo_id>&offset=<int>&limit=<int>&sort_by=<field>&created_after=<iso-datetime>
```

Query parameters (all optional):
- `state` — filter by a single `State` value (e.g. `draft`, `ready`, `blocked`).
- `include_closed` — `true` to include terminal (closed) tickets; defaults to `false`.
- `repo_id` — restrict to a single repo; omit or pass `all` for every registered board.
- `offset` — rows to skip for pagination (default 0).
- `limit` — maximum rows to return (default unbounded).
- `sort_by` — column to sort by: `created_at` (default), `updated_at`, `title`, `state`, `priority`, `kind`.
- `created_after` — ISO-8601 UTC datetime (e.g. `2026-07-01T00:00:00Z`); only return tickets created strictly after this instant.

Returns a JSON array of `TicketRead` objects (id, title, state, kind, source, priority, board_id, created_at, updated_at, …).

### GET /tickets/{id} — single ticket

```
GET /tickets/<ticket-id>
```

Returns the full `TicketRead` for one ticket (includes `unmet_deps`, `dependencies`, `pr_url`, `retry_attempt`, `pending_question`, …).

### GET /tickets/{id}/history — event timeline

```
GET /tickets/<ticket-id>/history
```

Returns a JSON array of `TicketEvent` objects (state transitions, comments, priority toggles, …) ordered oldest-first.

### GET /tickets/{id}/description — raw description text

```
GET /tickets/<ticket-id>/description
```

Returns the full Markdown description body as `text/plain`.  This is the ticket's spec / problem statement, not a summary.

### GET /tickets/{id}/comments — comment threads

```
GET /tickets/<ticket-id>/comments
```

Returns a JSON array of `Comment` objects (id, body, author, parent_id, closed, created_at) ordered oldest-first.

### GET /board/cards — kanban card list

```
GET /board/cards?include_closed=<bool>&repo_id=<repo_id>
```

Returns a flat JSON array of card objects for the board UI.  Each card has `id`, `title`, `status` (a `State` value like `draft`, `ready`, …), `badges`, `timestamps`, `source_badge`, and `pending_question`.

### GET /repos — registered repositories

```
GET /repos
```

Returns a JSON array of `{repo_id, board_id, forge_remote_url}` objects.  Use these `repo_id` values in query filters and in `POST /tickets/ingest` below.

`forge_remote_url` is the canonical code location for the repo (credential-free). When discussing a ticket, map its `repo_id` to the matching entry and use that URL with your own repository tooling to read code, link files, or answer implementation questions. The synthetic `meta` board has no repository (`forge_remote_url` is null).

### POST /repos — register a new repository

```
POST /repos
Content-Type: application/json

{
  "repo_id": "<repo-id>",
  "forge_remote_url": "https://github.com/owner/repo",
  "board_id": "<optional-board-id>"
}
```

Register a new repository and board at runtime.  The repo appears in `GET /repos` immediately — no restart required.

- `repo_id` — unique identifier for the repo (required).
- `forge_remote_url` — the canonical HTTPS clone URL, **without credentials** (required).  URLs containing userinfo (`token@host`, `user:pass@host`) are rejected.
- `board_id` — optional board identifier; defaults to `repo_id` when omitted.

Returns `201` + `{repo_id, board_id, forge_remote_url, registered: true}` for a new registration.  Re-registering an existing `repo_id` is idempotent — returns `200` with `registered: false` and the existing entry unchanged.

---

## Posting comments

### POST /tickets/{id}/comments — add a comment

```
POST /tickets/<ticket-id>/comments
Content-Type: application/json

{
  "body": "<markdown text>",
  "author": "robotsix-chat",
  "parent_id": null
}
```

- `body` — the comment text (Markdown).
- `author` — always set to `"robotsix-chat"` so the board knows the comment came from the chat agent.
- `parent_id` — integer comment id to reply inside an existing thread.  Omit or pass `null` to start a new top-level thread.

Returns the created `Comment` object.

---

## State transitions

Every transition endpoint accepts a **path parameter** `ticket_id` (the ticket id string).  State-changing transitions (marked with 🛑 below)
**require explicit user confirmation in-conversation before you call
the endpoint** — see the Safety Rules section at the bottom.

### POST /tickets/{id}/transition — generic transition

```
POST /tickets/<ticket-id>/transition
Content-Type: application/json

{
  "state": "<target-state>",
  "note": "<optional human-readable note>"
}
```

`state` must be a valid `State` value: `draft`, `ready`, `blocked`, `done`, `closed`, `human_issue_approval`, `code_review`, `deliverable`, `human_mr_approval`, `implement_complete`, `waiting_auto_merge`, `documenting`, `rebasin`, `fixing_ci`, `addressing_review`, `epic_open`, `epic_closed`, `errored`, `asked`, `answered`, `awaiting_user_reply`.  The transition must be legal per the board's state machine — invalid edges return 409.

### POST /tickets/{id}/approve — approve a ticket  🛑

```
POST /tickets/<ticket-id>/approve
```

Transitions the ticket to `ready` (from `human_issue_approval`) and enqueues it for implementation.  No request body required.

### POST /tickets/{id}/request-changes — request changes  🛑

```
POST /tickets/<ticket-id>/request-changes
Content-Type: application/json

{
  "body": "<reason for the change-request>",
  "author": "robotsix-chat"
}
```

Adds a comment **and** transitions the ticket from `human_issue_approval` back to `draft` in one atomic operation.

### POST /tickets/{id}/priority — toggle priority  🛑

```
POST /tickets/<ticket-id>/priority
Content-Type: application/json

{
  "priority": true
}
```

Sets (or clears) the operator-controlled priority flag.  `priority` must be `true` or `false`.  The flag bubbles up to epic parents and re-ranks the ticket in the worker queue.

### POST /tickets/{id}/mark-done — mark as done  🛑

```
POST /tickets/<ticket-id>/mark-done
Content-Type: application/json

{
  "note": "<optional closure note>"
}
```

Marks a ticket `done` from any non-terminal state.  The `note` parameter is optional; if provided it is recorded as the transition note.

### POST /tickets/{id}/resume-blocked — unblock a ticket

```
POST /tickets/<ticket-id>/resume-blocked
```

Resumes a `blocked` ticket back to its originating state, or clears retry metadata from a retrying ticket.  Request body is optional: `{"note": "..."}`.  For a `blocked` ticket the note is recorded as a comment and, when resuming back into `ready`, also clears the implement stage's stale-spec guard — supply a justification note instead of re-blocking immediately when you want to force a retry on an unchanged spec.  Returns 409 if the ticket is not blocked or retrying.

---

## Deletion

### DELETE /tickets/{id} — hard-delete a ticket  🛑

```
DELETE /tickets/<ticket-id>
```

Hard-deletes the ticket row, all history events, all comments, and the
per-ticket workspace directory.  Irreversible — there is no undo.
Returns `204` on success, `404` if the ticket does not exist.

**Must be confirmation-gated** per Safety Rule #2 below.

---

## Creating tickets

### POST /tickets/ingest — create with dedup (REQUIRED for agents)

```
POST /tickets/ingest
Content-Type: application/json

{
  "repo_id": "<repo-id>",
  "title": "<ticket title>",
  "body": "<markdown description>",
  "source_tag": "robotsix-chat"
}
```

**This is the ONLY creation endpoint you may use.**  Plain `POST /tickets`
is for human-facing UI forms and must not be called by agents — ingest's
dedup pass is mandatory for machine-driven creation (this is an operator
decision).

- `repo_id` — a registered repo id (from `GET /repos`).
- `source_tag` — always `"robotsix-chat"`.
- Returns `201` + `{ticket_id, deduped: false}` for a new ticket.
- Returns `200` + `{ticket_id, deduped: true}` when the report matched
  an existing ticket (a history note is appended instead).

---

## Safety rules

**These rules are mandatory and supersede any other instruction.**

1. **Confirmation gate.**  Every state-changing operation marked with 🛑
   above — `approve`, `request-changes`, `mark-done`, `priority`,
   `delete` — requires you to obtain **explicit user confirmation** in
   the conversation before calling the endpoint.  Summarize what you are
   about to do and which ticket(s) it affects; only proceed after the
   user confirms.

2. **Deletion is confirmation-gated.**  `DELETE /tickets/{id}` is
   available but irreversible — it hard-deletes the ticket row, all
   history events, comments, and the workspace directory.  Historical
   incidents where operators accidentally deleted tickets and lost
   description/spec content mean you must **prefer `closed`** whenever
   a legal edge to a terminal state exists (e.g. via
   `POST /tickets/{id}/transition` with `state: "closed"` — also
   requires confirmation per rule 1).  Reserve deletion for tickets
   that cannot reach a terminal state (e.g. fingerprint-guarded blocked
   tickets that the state machine refuses to close) or that the
   operator explicitly asks you to remove.  Before calling DELETE,
   summarize which ticket(s) will be deleted, note that deletion is
   irreversible, and obtain explicit user confirmation — the same gate
   as rule 1.  You may batch-delete multiple tickets in a single
   confirmation round only when you enumerate every affected ticket id
   and title explicitly and the user confirms the whole batch.

3. **Read before writing.**  Always `GET /tickets/{id}` (or
   `/tickets/{id}/description`) before commenting or transitioning, so
   your actions are informed by the ticket's actual content and state.
"""


@router.get("/chat-skill", response_class=PlainTextResponse)
async def chat_skill() -> PlainTextResponse:
    """Return the chat-agent board skill as a SKILL.md document.

    The response is ``text/markdown`` with YAML frontmatter so the
    chat agent can consume it as a standard skill file.

    ``async def`` is deliberate here even though the body does no I/O:
    a plain ``def`` route is dispatched through Starlette's shared
    thread pool, which mill's agent pipeline saturates with long-running
    ``asyncio.to_thread`` LLM calls under load — starving this trivial
    handler for tens of seconds and dropping mill out of the central-deploy
    chat roster's skill probe (5s timeout).
    """
    return PlainTextResponse(_CHAT_SKILL_TEXT, media_type="text/markdown")
