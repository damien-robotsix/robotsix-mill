"""Snapshot-test the auto-generated OpenAPI 3.1 schema."""

from __future__ import annotations

import pytest
pytest.importorskip("inline_snapshot")
from inline_snapshot import snapshot


def test_openapi_schema(client) -> None:
    """The full OpenAPI schema matches the snapshot.

    Run `pytest --inline-snapshot=fix` to update the snapshot
    when the API surface intentionally changes.
    """
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert response.json() == snapshot(
        {
            "openapi": "3.1.0",
            "info": {
                "title": "robotsix-mill",
                "description": "Self-contained LLM-driven ticket solver. SQLite-backed management plane + file workspaces, event-driven worker, delivers merge requests to GitHub/GitLab.",
                "contact": {
                    "name": "Damien Robotsix",
                    "url": "https://github.com/damien-robotsix/robotsix-mill",
                },
                "license": {"name": "MIT", "url": "https://spdx.org/licenses/MIT.html"},
                "version": "0.0.1",
            },
            "servers": [
                {"url": "http://127.0.0.1:8077", "description": "Local development"}
            ],
            "paths": {
                "/health": {
                    "get": {
                        "tags": ["Health"],
                        "summary": "Health",
                        "operationId": "health_health_get",
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "additionalProperties": True,
                                            "type": "object",
                                            "title": "Response Health Health Get",
                                        }
                                    }
                                },
                            }
                        },
                    }
                },
                "/health/ready": {
                    "get": {
                        "tags": ["Health"],
                        "summary": "Health Ready",
                        "operationId": "health_ready_health_ready_get",
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {"application/json": {"schema": {}}},
                            }
                        },
                    }
                },
                "/langfuse-status": {
                    "get": {
                        "tags": ["Health"],
                        "summary": "Langfuse Status",
                        "description": """\
Return recent Langfuse export failures so the UI can surface
"tracing broken" without the operator having to grep worker logs.

Empty ``failures`` list means everything is shipping fine.\
""",
                        "operationId": "langfuse_status_langfuse_status_get",
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "additionalProperties": True,
                                            "type": "object",
                                            "title": "Response Langfuse Status Langfuse Status Get",
                                        }
                                    }
                                },
                            }
                        },
                    }
                },
                "/credit-status": {
                    "get": {
                        "tags": ["Health"],
                        "summary": "Credit Status",
                        "description": """\
Return the current low-OpenRouter-credit warning state.

Polled by the board UI's ``fetchCreditStatus()`` every refresh
cycle.  ``low`` is ``true`` when the balance is below the
configured threshold OR a 402 insufficient-credit error was seen.\
""",
                        "operationId": "credit_status_credit_status_get",
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "additionalProperties": True,
                                            "type": "object",
                                            "title": "Response Credit Status Credit Status Get",
                                        }
                                    }
                                },
                            }
                        },
                    }
                },
                "/credit-status/clear": {
                    "post": {
                        "tags": ["Health"],
                        "summary": "Credit Status Clear",
                        "description": "Dismiss the low-credit warning after the operator acknowledges it.",
                        "operationId": "credit_status_clear_credit_status_clear_post",
                        "responses": {"204": {"description": "Successful Response"}},
                    }
                },
                "/worker-status": {
                    "get": {
                        "tags": ["Health"],
                        "summary": "Worker Status",
                        "description": """\
Live worker introspection for diagnosing stuck tickets.

Reports per-board queue depth, the in-flight ``_pending`` set, and
consumer-task health (incl. the exception of any task that died — a
dead per-board consumer is why a ``ready`` ticket on that board would
never be popped). Read-only.\
""",
                        "operationId": "worker_status_worker_status_get",
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "additionalProperties": True,
                                            "type": "object",
                                            "title": "Response Worker Status Worker Status Get",
                                        }
                                    }
                                },
                            }
                        },
                    }
                },
                "/langfuse-status/clear": {
                    "post": {
                        "tags": ["Health"],
                        "summary": "Langfuse Status Clear",
                        "description": "Drop the failure log after the operator acknowledges.",
                        "operationId": "langfuse_status_clear_langfuse_status_clear_post",
                        "responses": {"204": {"description": "Successful Response"}},
                    }
                },
                "/repos": {
                    "get": {
                        "tags": ["Health"],
                        "summary": "List Repos",
                        "description": """\
Return the registered repos for the UI repo selector.

No secrets (Langfuse keys) are included — ``repo_id``, ``board_id``
and a credential-stripped ``forge_remote_url`` (so agent consumers
like robotsix-chat can locate the code).  In single-repo mode
(``--repo-id`` passed) only that repo is returned.\
""",
                        "operationId": "list_repos_repos_get",
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "items": {
                                                "additionalProperties": True,
                                                "type": "object",
                                            },
                                            "type": "array",
                                            "title": "Response List Repos Repos Get",
                                        }
                                    }
                                },
                            }
                        },
                    },
                    "post": {
                        "tags": ["Repos"],
                        "summary": "Register Repo",
                        "description": """\
Register a repository at runtime by writing its entry to the
machine-owned overlay (``registered_repos.yaml``) and hot-reloading
the in-process :class:`ReposRegistry`.

Idempotent: re-registering an existing ``repo_id`` returns 200 with
the effective entry and does not touch the overlay file — operator
config entries are never modified.

New registrations return 201 and the repo is immediately visible via
``request.app.state.repos`` without a container restart.\
""",
                        "operationId": "register_repo_repos_post",
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/RepoRegistration"
                                    }
                                }
                            },
                            "required": True,
                        },
                        "responses": {
                            "201": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/RepoRegistrationResult"
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    },
                },
                "/gates": {
                    "get": {
                        "tags": ["Health"],
                        "summary": "Gates",
                        "description": """\
Return the four pipeline gate flags from the live configuration.

Same open access model as ``/health`` — no auth.  The board polls
these every refresh cycle and renders them as header pills so the
operator always sees which behavioural gates are active.\
""",
                        "operationId": "gates_gates_get",
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "additionalProperties": True,
                                            "type": "object",
                                            "title": "Response Gates Gates Get",
                                        }
                                    }
                                },
                            }
                        },
                    }
                },
                "/tickets/{ticket_id}/comments": {
                    "post": {
                        "tags": ["Comments"],
                        "summary": "Add Comment",
                        "description": """\
Add a comment to a ticket (any state).

Set *parent_id* to reply to an existing comment, forming a
threaded discussion.  Omit it (or pass ``null``) to start a new
top-level thread.

For epic tickets, the comment triggers a background re-processing:
the epic is re-broken-down by the breakdown agent with the full
comment history as operator direction, and net-new children are
created.  Non-epic tickets are unaffected — the comment is simply
persisted.\
""",
                        "operationId": "add_comment_tickets__ticket_id__comments_post",
                        "parameters": [
                            {
                                "name": "ticket_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string", "title": "Ticket Id"},
                            }
                        ],
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/CommentCreate"
                                    }
                                }
                            },
                        },
                        "responses": {
                            "201": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/Comment"
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    },
                    "get": {
                        "tags": ["Comments"],
                        "summary": "List Comments",
                        "description": "List all comments for a ticket, ordered oldest-first.",
                        "operationId": "list_comments_tickets__ticket_id__comments_get",
                        "parameters": [
                            {
                                "name": "ticket_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string", "title": "Ticket Id"},
                            }
                        ],
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "array",
                                            "items": {
                                                "$ref": "#/components/schemas/Comment"
                                            },
                                            "title": "Response List Comments Tickets  Ticket Id  Comments Get",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    },
                },
                "/comments/{comment_id}/close": {
                    "post": {
                        "tags": ["Comments"],
                        "summary": "Close Thread",
                        "description": """\
Close a top-level comment thread to mark it as resolved.

Pass ``ticket_id`` so the service resolves the correct per-board
DB — Comment.id is per-board (not globally unique), so a bare
``comment_id`` lookup is ambiguous across repos.\
""",
                        "operationId": "close_thread_comments__comment_id__close_post",
                        "parameters": [
                            {
                                "name": "comment_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "integer", "title": "Comment Id"},
                            },
                            {
                                "name": "ticket_id",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                    "title": "Ticket Id",
                                },
                            },
                        ],
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/Comment"
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/comments/{comment_id}/reopen": {
                    "post": {
                        "tags": ["Comments"],
                        "summary": "Reopen Thread",
                        "description": """\
Reopen a previously-closed comment thread.

Pass ``ticket_id`` so the service resolves the correct per-board
DB — Comment.id is per-board (not globally unique).\
""",
                        "operationId": "reopen_thread_comments__comment_id__reopen_post",
                        "parameters": [
                            {
                                "name": "comment_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "integer", "title": "Comment Id"},
                            },
                            {
                                "name": "ticket_id",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                    "title": "Ticket Id",
                                },
                            },
                        ],
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/Comment"
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/tickets": {
                    "post": {
                        "tags": ["Tickets"],
                        "summary": "Create Ticket",
                        "description": """\
Create a new ticket (``POST /tickets``).

Resolves the board from *body.repo_id*, creates the ticket row
plus workspace, enqueues it for the pipeline, and returns the
enriched ``TicketRead``.  Returns 400 when the board cannot be
resolved or the ticket data is invalid.\
""",
                        "operationId": "create_ticket_tickets_post",
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/TicketCreate"
                                    }
                                }
                            },
                        },
                        "responses": {
                            "201": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/TicketRead"
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    },
                    "get": {
                        "tags": ["Tickets"],
                        "summary": "List Tickets",
                        "description": """\
List tickets (``GET /tickets``).

Returns the active tickets, optionally filtered by *state* and
*repo_id*.  ``include_closed`` **defaults to False** — terminal
states (CLOSED, EPIC_CLOSED, ANSWERED) are hidden; DONE stays
visible (the transient retrospect window).  Closed/terminal
tickets are the overwhelming majority of rows (>90 % on a mature
board) and are not useful for board operation, so loading +
enriching them on every poll is the dominant cost behind an
unresponsive board; callers that genuinely need them must opt in
with ``include_closed=true``.  Enrichment is downgraded for
performance — cost is cache-only and PR URLs are skipped —
because the board polls this every few seconds.  A background
cost-warming task refreshes the rows on each poll so subsequent
requests show real values.

An explicit *state* filter (e.g. ``state=closed``) takes
precedence over the default exclusion — the terminal state is
removed from the exclusion set so the explicit filter works as
expected.\
""",
                        "operationId": "list_tickets_tickets_get",
                        "parameters": [
                            {
                                "name": "state",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "anyOf": [
                                        {"$ref": "#/components/schemas/State"},
                                        {"type": "null"},
                                    ],
                                    "title": "State",
                                },
                            },
                            {
                                "name": "include_closed",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "type": "boolean",
                                    "default": False,
                                    "title": "Include Closed",
                                },
                            },
                            {
                                "name": "repo_id",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                    "title": "Repo Id",
                                },
                            },
                        ],
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "array",
                                            "items": {
                                                "$ref": "#/components/schemas/TicketRead"
                                            },
                                            "title": "Response List Tickets Tickets Get",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    },
                },
                "/tickets/{ticket_id}": {
                    "get": {
                        "tags": ["Tickets"],
                        "summary": "Get Ticket",
                        "description": """\
Return a single ticket (``GET /tickets/{ticket_id}``).

Returns the fully enriched ``TicketRead`` (with cost and PR link).
Raises 404 when the ticket does not exist.\
""",
                        "operationId": "get_ticket_tickets__ticket_id__get",
                        "parameters": [
                            {
                                "name": "ticket_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string", "title": "Ticket Id"},
                            }
                        ],
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/TicketRead"
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    },
                    "delete": {
                        "tags": ["Tickets"],
                        "summary": "Delete Ticket",
                        "description": """\
Hard-delete a ticket (row + history + workspace). Irreversible.
404 if it doesn't exist.\
""",
                        "operationId": "delete_ticket_tickets__ticket_id__delete",
                        "parameters": [
                            {
                                "name": "ticket_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string", "title": "Ticket Id"},
                            }
                        ],
                        "responses": {
                            "204": {"description": "Successful Response"},
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    },
                },
                "/tickets/{ticket_id}/history": {
                    "get": {
                        "tags": ["Tickets"],
                        "summary": "Get History",
                        "description": """\
Return event history for a ticket (``GET /tickets/{ticket_id}/history``).

Returns the ordered list of ``TicketEvent`` rows.  Raises 404 when
the ticket does not exist.\
""",
                        "operationId": "get_history_tickets__ticket_id__history_get",
                        "parameters": [
                            {
                                "name": "ticket_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string", "title": "Ticket Id"},
                            }
                        ],
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "array",
                                            "items": {
                                                "$ref": "#/components/schemas/TicketEvent"
                                            },
                                            "title": "Response Get History Tickets  Ticket Id  History Get",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/tickets/{ticket_id}/description": {
                    "get": {
                        "tags": ["Tickets"],
                        "summary": "Get Description",
                        "description": """\
Return the current description for a ticket (``GET /tickets/{ticket_id}/description``).

Reads the description from the ticket's workspace on disk.
Returns ``{"description": "..."}``.  Raises 404 when the ticket
does not exist.\
""",
                        "operationId": "get_description_tickets__ticket_id__description_get",
                        "parameters": [
                            {
                                "name": "ticket_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string", "title": "Ticket Id"},
                            }
                        ],
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "title": "Response Get Description Tickets  Ticket Id  Description Get",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/tickets/{ticket_id}/screenshots": {
                    "post": {
                        "tags": ["Tickets"],
                        "summary": "Upload Screenshot",
                        "description": """\
Attach an image screenshot to a ticket for the refine agent to view.

Stores the bytes under the ticket's ``screenshots/`` directory (a
sibling of ``artifacts/`` so a refine reset does not wipe user
input). Rejects non-image uploads with 400 and unknown tickets with
404. The filename is reduced to its basename to prevent traversal.\
""",
                        "operationId": "upload_screenshot_tickets__ticket_id__screenshots_post",
                        "parameters": [
                            {
                                "name": "ticket_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string", "title": "Ticket Id"},
                            }
                        ],
                        "requestBody": {
                            "required": True,
                            "content": {
                                "multipart/form-data": {
                                    "schema": {
                                        "$ref": "#/components/schemas/Body_upload_screenshot_tickets__ticket_id__screenshots_post"
                                    }
                                }
                            },
                        },
                        "responses": {
                            "201": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "title": "Response Upload Screenshot Tickets  Ticket Id  Screenshots Post",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/tickets/{ticket_id}/retrospect": {
                    "get": {
                        "tags": ["Tickets"],
                        "summary": "Get Retrospect",
                        "description": """\
Return the retrospect.md artifact for a ticket, or empty if
retrospect has not run yet (or the artifact was lost). Lets the
board surface what retrospect actually wrote — without this the
DONE -> CLOSED transition looks like it happened with no
reflection, even when retrospect did run and write real analysis.\
""",
                        "operationId": "get_retrospect_tickets__ticket_id__retrospect_get",
                        "parameters": [
                            {
                                "name": "ticket_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string", "title": "Ticket Id"},
                            }
                        ],
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "title": "Response Get Retrospect Tickets  Ticket Id  Retrospect Get",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/tickets/{ticket_id}/artifacts": {
                    "get": {
                        "tags": ["Tickets"],
                        "summary": "List Artifacts",
                        "description": """\
List artifact files in this ticket's workspace.

Returns ``{"artifacts": [{"name": str, "size": int, "mtime": str},
...]}`` sorted by mtime ascending. Used by the board UI's drawer
to surface each agent's output — pre-v1 the implement / refine /
retrospect markdowns only existed on disk.\
""",
                        "operationId": "list_artifacts_tickets__ticket_id__artifacts_get",
                        "parameters": [
                            {
                                "name": "ticket_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string", "title": "Ticket Id"},
                            }
                        ],
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "title": "Response List Artifacts Tickets  Ticket Id  Artifacts Get",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/tickets/{ticket_id}/artifacts/{name}": {
                    "get": {
                        "tags": ["Tickets"],
                        "summary": "Get Artifact",
                        "description": """\
Return the text content of a single artifact file.

Refuses path-traversal (``..``, ``/``) so the route only serves
files directly under the ticket's ``artifacts_dir``. Binary files
return decoded-with-replace text since the drawer renders
markdown / JSON; a hex viewer can be added later if needed.\
""",
                        "operationId": "get_artifact_tickets__ticket_id__artifacts__name__get",
                        "parameters": [
                            {
                                "name": "ticket_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string", "title": "Ticket Id"},
                            },
                            {
                                "name": "name",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string", "title": "Name"},
                            },
                        ],
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "title": "Response Get Artifact Tickets  Ticket Id  Artifacts  Name  Get",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/tickets/{ticket_id}/transition": {
                    "post": {
                        "tags": ["Tickets"],
                        "summary": "Transition",
                        "description": """\
Transition a ticket to a new state (``POST /tickets/{ticket_id}/transition``).

Body: ``{"state": "<state>", "note": "<optional note>"}``.
Enqueues the ticket after transition so the pipeline picks it up.
Returns the enriched ``TicketRead``.  Raises 404 when the ticket
does not exist.\
""",
                        "operationId": "transition_tickets__ticket_id__transition_post",
                        "parameters": [
                            {
                                "name": "ticket_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string", "title": "Ticket Id"},
                            }
                        ],
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/TicketTransition"
                                    }
                                }
                            },
                        },
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/TicketRead"
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/tickets/{ticket_id}/migrate": {
                    "post": {
                        "tags": ["Tickets"],
                        "summary": "Migrate Ticket",
                        "description": """\
Move a ticket to another board (row, history, comments, workspace).

For tickets filed on the wrong board (the fix belongs to a
different repo). The migrated ticket lands in DRAFT on the target
board so its refine stage re-triages it there.\
""",
                        "operationId": "migrate_ticket_tickets__ticket_id__migrate_post",
                        "parameters": [
                            {
                                "name": "ticket_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string", "title": "Ticket Id"},
                            }
                        ],
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/TicketMigrate"
                                    }
                                }
                            },
                        },
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/TicketRead"
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/tickets/{ticket_id}/unblocks": {
                    "post": {
                        "tags": ["Tickets"],
                        "summary": "Set Unblocks",
                        "description": """\
Set the list of ticket IDs that *ticket_id* auto-unblocks when it
completes (DONE/CLOSED/EPIC_CLOSED). Body: ``{"ticket_ids": [...]}``.

Each listed ticket that is BLOCKED at that point is transitioned
BLOCKED -> DRAFT. Cross-board safe. Returns the updated solver ticket.\
""",
                        "operationId": "set_unblocks_tickets__ticket_id__unblocks_post",
                        "parameters": [
                            {
                                "name": "ticket_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string", "title": "Ticket Id"},
                            }
                        ],
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "additionalProperties": True,
                                        "title": "Body",
                                    }
                                }
                            },
                        },
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/TicketRead"
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/tickets/{ticket_id}/approve": {
                    "post": {
                        "tags": ["Tickets"],
                        "summary": "Approve Ticket",
                        "description": """\
Human approval for a ticket (``POST /tickets/{ticket_id}/approve``).

Transitions the ticket to READY and enqueues it so implement picks
it up.  If the ticket has an epic parent and a proposed epic body
artifact exists (``epic-body-proposed.md``), that body is applied
to the epic as a best-effort side effect.  Returns 404 when the
ticket does not exist.\
""",
                        "operationId": "approve_ticket_tickets__ticket_id__approve_post",
                        "parameters": [
                            {
                                "name": "ticket_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string", "title": "Ticket Id"},
                            }
                        ],
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/TicketRead"
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/tickets/{ticket_id}/merge-now": {
                    "post": {
                        "tags": ["Tickets"],
                        "summary": "Merge Now",
                        "description": """\
Merge the PR for a ticket in human_mr_approval directly via the
forge API, then transition to done.  This is the explicit human
merge path — it bypasses auto-merge eligibility and calls the
forge's merge endpoint immediately.

For multi-repo (meta-board) tickets — those whose deliver stage
wrote ``pr_urls.json`` — this merges the PR of *every* repo listed
in the manifest, each via that repo's own ``RepoConfig``. Already-
merged repos are skipped so a re-press after a partial failure is
idempotent; only when every repo is merged does the ticket advance
to done.

Returns 409 when the ticket is not in human_mr_approval, when the
manifest is corrupt, or when the forge rejects a merge (branch
protection, conflict, etc.).\
""",
                        "operationId": "merge_now_tickets__ticket_id__merge_now_post",
                        "parameters": [
                            {
                                "name": "ticket_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string", "title": "Ticket Id"},
                            }
                        ],
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/TicketRead"
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/tickets/{ticket_id}/merge-info": {
                    "get": {
                        "tags": ["Tickets"],
                        "summary": "Get Merge Info",
                        "description": """\
Return CI status, mergeable flag, and changed files for the PR/MR
backing *ticket_id*.  Each forge call is individually resilient —
a failure in one field does not crash the whole response.\
""",
                        "operationId": "get_merge_info_tickets__ticket_id__merge_info_get",
                        "parameters": [
                            {
                                "name": "ticket_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string", "title": "Ticket Id"},
                            }
                        ],
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "title": "Response Get Merge Info Tickets  Ticket Id  Merge Info Get",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/tickets/{ticket_id}/merge-reason": {
                    "get": {
                        "tags": ["Tickets"],
                        "summary": "Get Merge Reason",
                        "description": """\
Return the auto-merge blocking reason written by the merge
stage, or an empty string when no reason has been recorded.\
""",
                        "operationId": "get_merge_reason_tickets__ticket_id__merge_reason_get",
                        "parameters": [
                            {
                                "name": "ticket_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string", "title": "Ticket Id"},
                            }
                        ],
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "title": "Response Get Merge Reason Tickets  Ticket Id  Merge Reason Get",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/tickets/{ticket_id}/merge-status": {
                    "get": {
                        "tags": ["Tickets"],
                        "summary": "Get Merge Status",
                        "description": """\
Return live merge-readiness for a ticket's PR.

Called by the ticket drawer before rendering the Merge button so
the user sees *why* they can't merge right now (conflicts, failing
CI, pending checks) instead of hitting a bare 409 from
``/merge-now``.  Returns ``can_merge: true`` on transient forge
errors so the Merge button stays active — the actual merge
endpoint handles the real rejection.\
""",
                        "operationId": "get_merge_status_tickets__ticket_id__merge_status_get",
                        "parameters": [
                            {
                                "name": "ticket_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string", "title": "Ticket Id"},
                            }
                        ],
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "title": "Response Get Merge Status Tickets  Ticket Id  Merge Status Get",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/tickets/{ticket_id}/request-changes": {
                    "post": {
                        "tags": ["Tickets"],
                        "summary": "Request Changes",
                        "description": """\
Add a comment AND transition from human_issue_approval back to draft
in one atomic operation.\
""",
                        "operationId": "request_changes_tickets__ticket_id__request_changes_post",
                        "parameters": [
                            {
                                "name": "ticket_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string", "title": "Ticket Id"},
                            }
                        ],
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/CommentCreate"
                                    }
                                }
                            },
                        },
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "title": "Response Request Changes Tickets  Ticket Id  Request Changes Post",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/tickets/{ticket_id}/priority": {
                    "post": {
                        "tags": ["Tickets"],
                        "summary": "Set Priority",
                        "description": """\
Toggle the operator-controlled priority flag on a ticket.

Body: ``{"priority": true|false}``.  Re-enqueues the ticket so the
priority change is reflected in the next consumer pop.\
""",
                        "operationId": "set_priority_tickets__ticket_id__priority_post",
                        "parameters": [
                            {
                                "name": "ticket_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string", "title": "Ticket Id"},
                            }
                        ],
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "additionalProperties": True,
                                        "title": "Body",
                                    }
                                }
                            },
                        },
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/TicketRead"
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/tickets/{ticket_id}/redraft": {
                    "post": {
                        "tags": ["Tickets"],
                        "summary": "Redraft",
                        "description": """\
Redraft a ticket from any active state back to DRAFT with an
optional comment.\
""",
                        "operationId": "redraft_tickets__ticket_id__redraft_post",
                        "parameters": [
                            {
                                "name": "ticket_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string", "title": "Ticket Id"},
                            }
                        ],
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/CommentCreate"
                                    }
                                }
                            },
                        },
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "title": "Response Redraft Tickets  Ticket Id  Redraft Post",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/tickets/{ticket_id}/mark-done": {
                    "post": {
                        "tags": ["Tickets"],
                        "summary": "Mark Done",
                        "description": """\
Mark a ticket as DONE from any non-terminal state.

Accepts an optional ``note`` in the JSON body that is recorded
as the event note.  Returns the updated ticket on success, 404
when the ticket is unknown, and 409 when the ticket is already in
a terminal state or an epic.\
""",
                        "operationId": "mark_done_tickets__ticket_id__mark_done_post",
                        "parameters": [
                            {
                                "name": "ticket_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string", "title": "Ticket Id"},
                            }
                        ],
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "additionalProperties": True,
                                        "default": {},
                                        "title": "Body",
                                    }
                                }
                            }
                        },
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/TicketRead"
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/tickets/{ticket_id}/resume-blocked": {
                    "post": {
                        "tags": ["Tickets"],
                        "summary": "Resume Blocked",
                        "description": """\
Resume a blocked or retrying ticket.

For BLOCKED tickets, transitions back to the originating state.
For retrying tickets (retry_attempt > 0 in any non-BLOCKED state),
clears the retry metadata and re-enqueues immediately.\
""",
                        "operationId": "resume_blocked_tickets__ticket_id__resume_blocked_post",
                        "parameters": [
                            {
                                "name": "ticket_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string", "title": "Ticket Id"},
                            }
                        ],
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/TicketRead"
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/tickets/{ticket_id}/cost-breakdown": {
                    "get": {
                        "tags": ["Tickets"],
                        "summary": "Cost Breakdown",
                        "description": """\
Per-trace cost breakdown for a ticket, used by the drawer to
overlay agent-step costs on history rows.

The Langfuse sessionId is the repo-qualified ticket id
(``<repo> · <ticket>``, applied inside ``session_traces``), so a
single `/api/public/traces?sessionId=…` query returns every agent
invocation tied to the ticket. Each entry carries
``{name, cost, at, trace_id}`` ordered by timestamp; the drawer's
renderHistoryHtml matches the entries to history events by inferred
agent name + nearest-in-time-≤ pairing.\
""",
                        "operationId": "cost_breakdown_tickets__ticket_id__cost_breakdown_get",
                        "parameters": [
                            {
                                "name": "ticket_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string", "title": "Ticket Id"},
                            }
                        ],
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "title": "Response Cost Breakdown Tickets  Ticket Id  Cost Breakdown Get",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/epics": {
                    "post": {
                        "tags": ["Epics"],
                        "summary": "Create Epic",
                        "description": """\
Create a new epic — accepts ``{"title": str, "description": str}``.

An optional ``repo_id`` field scopes the epic to a specific repo's
board.  When omitted in single-repo mode the sole repo is used;
in multi-repo mode ``repo_id`` is required and a 400 is returned
if it is missing.\
""",
                        "operationId": "create_epic_epics_post",
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "additionalProperties": True,
                                        "type": "object",
                                        "title": "Body",
                                    }
                                }
                            },
                            "required": True,
                        },
                        "responses": {
                            "201": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/TicketRead"
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/tickets/{ticket_id}/abandon-epic": {
                    "post": {
                        "tags": ["Epics"],
                        "summary": "Abandon Epic",
                        "description": """\
Abandon an ``EPIC_OPEN`` epic by transitioning it to ``EPIC_CLOSED``.

The epic stops spawning children once abandoned (the re-eval worker
skips ``EPIC_CLOSED`` epics).  Accepts an optional ``actor`` field
in the JSON body (defaults to ``"operator"``).

Returns 422 when the ticket is not in ``EPIC_OPEN`` state.\
""",
                        "operationId": "abandon_epic_tickets__ticket_id__abandon_epic_post",
                        "parameters": [
                            {
                                "name": "ticket_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string", "title": "Ticket Id"},
                            }
                        ],
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "additionalProperties": True,
                                        "title": "Body",
                                    }
                                }
                            },
                        },
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/TicketRead"
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/tickets/{ticket_id}/children": {
                    "get": {
                        "tags": ["Epics"],
                        "summary": "List Children",
                        "description": "Return all tickets whose ``parent_id`` equals *ticket_id*.",
                        "operationId": "list_children_tickets__ticket_id__children_get",
                        "parameters": [
                            {
                                "name": "ticket_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string", "title": "Ticket Id"},
                            }
                        ],
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "array",
                                            "items": {
                                                "$ref": "#/components/schemas/TicketRead"
                                            },
                                            "title": "Response List Children Tickets  Ticket Id  Children Get",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/tickets/{ticket_id}/generate-children": {
                    "post": {
                        "tags": ["Epics"],
                        "summary": "Generate Children",
                        "description": """\
Generate child tickets from an epic description using the LLM
epic-breakdown agent.  Returns ``202 Accepted`` immediately — the
agent runs in a background thread.

Returns ``400`` if the ticket is not an epic.  Returns ``404`` if
the ticket does not exist.\
""",
                        "operationId": "generate_children_tickets__ticket_id__generate_children_post",
                        "parameters": [
                            {
                                "name": "ticket_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string", "title": "Ticket Id"},
                            },
                            {
                                "name": "repo_id",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                    "title": "Repo Id",
                                },
                            },
                        ],
                        "responses": {
                            "202": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "title": "Response Generate Children Tickets  Ticket Id  Generate Children Post",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/audit": {
                    "post": {
                        "tags": ["Passes"],
                        "summary": "Audit Pass",
                        "description": """\
Kick off an audit pass in the BACKGROUND and return at once.

The audit runs the LLM agent for minutes — blocking the HTTP
response made the browser fetch drop ("NetworkError"). New draft
tickets appear on the board when it finishes.\
""",
                        "operationId": "audit_pass_audit_post",
                        "parameters": [
                            {
                                "name": "repo_id",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                    "title": "Repo Id",
                                },
                            }
                        ],
                        "responses": {
                            "202": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "title": "Response Audit Pass Audit Post",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/bc-check": {
                    "post": {
                        "tags": ["Passes"],
                        "summary": "Bc Check Pass",
                        "description": """\
Kick off a bc-check pass in the BACKGROUND and return at once.

The bc-check agent inspects the codebase for backward-compat shims
and dead-code branches that are ripe for removal, drafting tickets
when it finds candidates. New drafts appear on the board when it
finishes.\
""",
                        "operationId": "bc_check_pass_bc_check_post",
                        "parameters": [
                            {
                                "name": "repo_id",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                    "title": "Repo Id",
                                },
                            }
                        ],
                        "responses": {
                            "202": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "title": "Response Bc Check Pass Bc Check Post",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/completeness-check": {
                    "post": {
                        "tags": ["Passes"],
                        "summary": "Completeness Check Pass",
                        "description": "Kick off a completeness-check pass in the BACKGROUND and return at once.",
                        "operationId": "completeness_check_pass_completeness_check_post",
                        "parameters": [
                            {
                                "name": "repo_id",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                    "title": "Repo Id",
                                },
                            }
                        ],
                        "responses": {
                            "202": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "title": "Response Completeness Check Pass Completeness Check Post",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/agent-check": {
                    "post": {
                        "tags": ["Passes"],
                        "summary": "Agent Check Pass",
                        "description": """\
Kick off an agent-check pass in the BACKGROUND and return at
once. The agent inspects every agent's prompt, tools, and
structured output, looking for coherence gaps (e.g. an agent
promising behaviour its tools can't deliver). New draft tickets
appear on the board when it finishes.\
""",
                        "operationId": "agent_check_pass_agent_check_post",
                        "parameters": [
                            {
                                "name": "repo_id",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                    "title": "Repo Id",
                                },
                            }
                        ],
                        "responses": {
                            "202": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "title": "Response Agent Check Pass Agent Check Post",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/health-check": {
                    "post": {
                        "tags": ["Passes"],
                        "summary": "Health Pass",
                        "description": """\
Kick off a codebase-health pass in the BACKGROUND and return at
once.

The health pass runs the LLM agent for minutes — blocking the HTTP
response made the browser fetch drop ("NetworkError"). New draft
tickets appear on the board when it finishes.

Mirrors the audit/trace-health pattern: registers the run on
start so the /runs panel shows it in-flight, and on finish so it
flips to ok/error with a summary. Without this the run is silently
happening behind the scenes — the Langfuse trace exists but the
board reports nothing.\
""",
                        "operationId": "health_pass_health_check_post",
                        "parameters": [
                            {
                                "name": "repo_id",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                    "title": "Repo Id",
                                },
                            }
                        ],
                        "responses": {
                            "202": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "title": "Response Health Pass Health Check Post",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/test-gap": {
                    "post": {
                        "tags": ["Passes"],
                        "summary": "Test Gap Pass",
                        "description": "Kick off a test-gap inspection pass in the BACKGROUND.",
                        "operationId": "test_gap_pass_test_gap_post",
                        "parameters": [
                            {
                                "name": "repo_id",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                    "title": "Repo Id",
                                },
                            }
                        ],
                        "responses": {
                            "202": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "title": "Response Test Gap Pass Test Gap Post",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/survey": {
                    "post": {
                        "tags": ["Passes"],
                        "summary": "Survey Pass",
                        "description": """\
Kick off a survey pass in the BACKGROUND and return at once.

The survey agent discovers similar open-source projects, studies
their approaches, and proposes concrete improvements as draft
tickets. New drafts appear on the board when it finishes.\
""",
                        "operationId": "survey_pass_survey_post",
                        "parameters": [
                            {
                                "name": "repo_id",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                    "title": "Repo Id",
                                },
                            }
                        ],
                        "responses": {
                            "202": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "title": "Response Survey Pass Survey Post",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/copy-paste": {
                    "post": {
                        "tags": ["Passes"],
                        "summary": "Copy Paste Pass",
                        "description": """\
Kick off a copy-paste pass in the BACKGROUND and return at once.

The copy-paste agent detects clone/duplication clusters across the
codebase, triages the worst offenders, and proposes consolidation
as draft tickets. New drafts appear on the board when it finishes.\
""",
                        "operationId": "copy_paste_pass_copy_paste_post",
                        "parameters": [
                            {
                                "name": "repo_id",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                    "title": "Repo Id",
                                },
                            }
                        ],
                        "responses": {
                            "202": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "title": "Response Copy Paste Pass Copy Paste Post",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/module-curator": {
                    "post": {
                        "tags": ["Passes"],
                        "summary": "Module Curator Pass",
                        "description": """\
Kick off a module-curator pass in the BACKGROUND and return at once.

The module-curator agent compares the live directory tree against
``docs/modules.yaml`` and files draft tickets for unclassified files,
stale paths, and new module proposals. New drafts appear on the board
when it finishes.\
""",
                        "operationId": "module_curator_pass_module_curator_post",
                        "parameters": [
                            {
                                "name": "repo_id",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                    "title": "Repo Id",
                                },
                            }
                        ],
                        "responses": {
                            "202": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "title": "Response Module Curator Pass Module Curator Post",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/forge-parity": {
                    "post": {
                        "tags": ["Passes"],
                        "summary": "Forge Parity Pass",
                        "description": """\
Kick off a forge-parity pass in the BACKGROUND and return at once.

The forge-parity agent compares forge adapter implementations (GitHub vs GitLab)
against the Forge ABC, flags drift (single-adapter overrides, divergent
implementations, extra methods), and files at most 3 draft tickets per pass.
New drafts appear on the board when it finishes.\
""",
                        "operationId": "forge_parity_pass_forge_parity_post",
                        "parameters": [
                            {
                                "name": "repo_id",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                    "title": "Repo Id",
                                },
                            }
                        ],
                        "responses": {
                            "202": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "title": "Response Forge Parity Pass Forge Parity Post",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/config-sync": {
                    "post": {
                        "tags": ["Passes"],
                        "summary": "Config Sync Pass",
                        "description": "Kick off a config-sync drift detection pass in the BACKGROUND.",
                        "operationId": "config_sync_pass_config_sync_post",
                        "parameters": [
                            {
                                "name": "repo_id",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                    "title": "Repo Id",
                                },
                            }
                        ],
                        "responses": {
                            "202": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "title": "Response Config Sync Pass Config Sync Post",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/member-sync": {
                    "post": {
                        "tags": ["Passes"],
                        "summary": "Member Sync Pass",
                        "description": """\
Kick off a workspace member-sync pass in the BACKGROUND.

The deterministic member-sync pass clones the managed repo, detects
its vcs2l workspace members from ``repos.yaml``, and upserts them into
``config/repos.yaml`` (registering new members, refreshing existing
ones, flagging vanished ones for removal).\
""",
                        "operationId": "member_sync_pass_member_sync_post",
                        "parameters": [
                            {
                                "name": "repo_id",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                    "title": "Repo Id",
                                },
                            }
                        ],
                        "responses": {
                            "202": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "title": "Response Member Sync Pass Member Sync Post",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/state-sync": {
                    "post": {
                        "tags": ["Passes"],
                        "summary": "State Sync Pass",
                        "description": """\
Kick off a state-sync pass in the BACKGROUND and return at once.

The state-sync agent inspects the board's state consistency, checking for
stale state values, typos, and missing transitions. New draft tickets
appear on the board when it finishes.\
""",
                        "operationId": "state_sync_pass_state_sync_post",
                        "parameters": [
                            {
                                "name": "repo_id",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                    "title": "Repo Id",
                                },
                            }
                        ],
                        "responses": {
                            "202": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "title": "Response State Sync Pass State Sync Post",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/env-doc-sync": {
                    "post": {
                        "tags": ["Passes"],
                        "summary": "Env Doc Sync Pass",
                        "description": """\
Kick off an env-doc-sync pass in the BACKGROUND and return at once.

The env-doc-sync agent cross-references env-var declarations in the
Settings mixins against docs/configuration.md. New draft tickets appear
on the board when it finishes.\
""",
                        "operationId": "env_doc_sync_pass_env_doc_sync_post",
                        "parameters": [
                            {
                                "name": "repo_id",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                    "title": "Repo Id",
                                },
                            }
                        ],
                        "responses": {
                            "202": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "title": "Response Env Doc Sync Pass Env Doc Sync Post",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/frontend-sync": {
                    "post": {
                        "tags": ["Passes"],
                        "summary": "Frontend Sync Pass",
                        "description": """\
Kick off a frontend-sync pass in the BACKGROUND and return at once.

The frontend-sync agent keeps the front-end codebase aligned with
backend API definitions — route signatures, type bindings, and
shared constants. New draft tickets appear on the board when it
finishes.\
""",
                        "operationId": "frontend_sync_pass_frontend_sync_post",
                        "parameters": [
                            {
                                "name": "repo_id",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                    "title": "Repo Id",
                                },
                            }
                        ],
                        "responses": {
                            "202": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "title": "Response Frontend Sync Pass Frontend Sync Post",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/security-posture": {
                    "post": {
                        "tags": ["Passes"],
                        "summary": "Security Posture Pass",
                        "description": """\
Kick off a security-posture pass in the BACKGROUND and return at once.

The security-posture agent reviews the codebase for security
weaknesses, dependency vulnerabilities, and configuration gaps,
filing draft tickets for each finding. New drafts appear on the
board when it finishes.\
""",
                        "operationId": "security_posture_pass_security_posture_post",
                        "parameters": [
                            {
                                "name": "repo_id",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                    "title": "Repo Id",
                                },
                            }
                        ],
                        "responses": {
                            "202": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "title": "Response Security Posture Pass Security Posture Post",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/triage-boilerplate": {
                    "post": {
                        "tags": ["Passes"],
                        "summary": "Triage Boilerplate Pass",
                        "description": """\
Kick off a triage-boilerplate pass in the BACKGROUND and return at once.

The triage-boilerplate agent scans recent triage tickets for recurring
patterns and proposes boilerplate response templates, filing draft
tickets for each finding. New drafts appear on the board when it finishes.\
""",
                        "operationId": "triage_boilerplate_pass_triage_boilerplate_post",
                        "parameters": [
                            {
                                "name": "repo_id",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                    "title": "Repo Id",
                                },
                            }
                        ],
                        "responses": {
                            "202": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "title": "Response Triage Boilerplate Pass Triage Boilerplate Post",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/trace-review": {
                    "post": {
                        "tags": ["Passes"],
                        "summary": "Trace Review Pass",
                        "description": """\
Kick off a trace-review pass in the BACKGROUND.

Scans every Langfuse trace since the last run, deterministically
flags outliers (cost, observation count, tool errors, repeated
pauses, rejected generations, explore storms), runs a cheap
flash-model inspector over the flagged subset, and files draft
tickets with proposed solutions.\
""",
                        "operationId": "trace_review_pass_trace_review_post",
                        "parameters": [
                            {
                                "name": "repo_id",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                    "title": "Repo Id",
                                },
                            }
                        ],
                        "responses": {
                            "202": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "title": "Response Trace Review Pass Trace Review Post",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/roadmap-sync": {
                    "post": {
                        "tags": ["Passes"],
                        "summary": "Roadmap Sync Pass",
                        "description": """\
Kick off a roadmap-sync pass in the BACKGROUND.

Reads ROADMAP.md from the configured repo and reconciles its
H2 sections against the board's existing epics by an embedded
``<!-- epic-id: ... -->`` marker. Creates new epics for unmarked
sections, updates existing epics whose body/title changed, and
opens a PR with the marker insertions so the next run is
idempotent.\
""",
                        "operationId": "roadmap_sync_pass_roadmap_sync_post",
                        "parameters": [
                            {
                                "name": "repo_id",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                    "title": "Repo Id",
                                },
                            }
                        ],
                        "responses": {
                            "202": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "title": "Response Roadmap Sync Pass Roadmap Sync Post",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/trace-health": {
                    "post": {
                        "tags": ["Passes"],
                        "summary": "Trace Health Check",
                        "description": """\
Kick off a trace-health check in the BACKGROUND and return at
once.  The check fetches Langfuse traces from the last 24h,
detects unsessioned traces, and files a draft ticket if needed.
No LLM — deterministic and fast.\
""",
                        "operationId": "trace_health_check_trace_health_post",
                        "parameters": [
                            {
                                "name": "repo_id",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                    "title": "Repo Id",
                                },
                            }
                        ],
                        "responses": {
                            "202": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "title": "Response Trace Health Check Trace Health Post",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/langfuse-cleanup": {
                    "post": {
                        "tags": ["Passes"],
                        "summary": "Langfuse Cleanup Pass",
                        "description": """\
Kick off a Langfuse trace cleanup in the BACKGROUND and return at
once.  The cleanup deletes the oldest traces until the project is
at most ``max_traces`` rows.  Pure HTTP, no LLM.\
""",
                        "operationId": "langfuse_cleanup_pass_langfuse_cleanup_post",
                        "parameters": [
                            {
                                "name": "repo_id",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                    "title": "Repo Id",
                                },
                            }
                        ],
                        "responses": {
                            "202": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "title": "Response Langfuse Cleanup Pass Langfuse Cleanup Post",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/meta": {
                    "post": {
                        "tags": ["Passes"],
                        "summary": "Meta Pass",
                        "description": """\
Kick off a META pass in the BACKGROUND and return at once.

The meta-agent surveys ALL registered repo clones, identifies
extraction and alignment opportunities, and files drafts to the
meta board and per-repo boards respectively.  This is a global
pass — it does not fan out per-repo.\
""",
                        "operationId": "meta_pass_meta_post",
                        "parameters": [
                            {
                                "name": "repo_id",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                    "title": "Repo Id",
                                },
                            }
                        ],
                        "responses": {
                            "202": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "title": "Response Meta Pass Meta Post",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/run-health": {
                    "post": {
                        "tags": ["Passes"],
                        "summary": "Run Health Pass",
                        "description": """\
Kick off a RUN-HEALTH pass in the BACKGROUND and return at once.

The run-health agent reads every board's run registry over the window,
flags failed/degraded runs deterministically, runs one LLM pass to
separate real failures from legitimate empties, and files
high-confidence draft tickets to the mill board. Global — it does not
fan out per-repo.\
""",
                        "operationId": "run_health_pass_run_health_post",
                        "parameters": [
                            {
                                "name": "repo_id",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                    "title": "Repo Id",
                                },
                            }
                        ],
                        "responses": {
                            "202": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "title": "Response Run Health Pass Run Health Post",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/runs": {
                    "get": {
                        "tags": ["Traces"],
                        "summary": "List Runs",
                        "description": """\
Return recent background-run entries (newest first).

``?repo_id=X`` returns X's runs. Without it (or ``?repo_id=all``) the
aggregate view UNIONS every per-repo registry. Periodic runs (audit,
bc_check, health, …) are recorded into the per-repo registry, not the
lead repo's, so reading only the default registry would hide them on
the all-repos board even though they show on the per-repo board.\
""",
                        "operationId": "list_runs_runs_get",
                        "parameters": [
                            {
                                "name": "repo_id",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                    "title": "Repo Id",
                                },
                            }
                        ],
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "array",
                                            "items": {
                                                "type": "object",
                                                "additionalProperties": True,
                                            },
                                            "title": "Response List Runs Runs Get",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/active": {
                    "get": {
                        "tags": ["Traces"],
                        "summary": "List Active",
                        "description": """\
Return tickets currently being processed by a pipeline stage.

``?repo_id=X`` filters to active tickets belonging to that repo.
When omitted, returns all (current behaviour preserved).\
""",
                        "operationId": "list_active_active_get",
                        "parameters": [
                            {
                                "name": "repo_id",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                    "title": "Repo Id",
                                },
                            }
                        ],
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "array",
                                            "items": {
                                                "type": "object",
                                                "additionalProperties": True,
                                            },
                                            "title": "Response List Active Active Get",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/traces/recent": {
                    "get": {
                        "tags": ["Traces"],
                        "summary": "List Recent Traces",
                        "description": """\
Return recent Langfuse traces, filtered by cost and limited in
count.  *limit* is clamped to 1–50; *min_cost* and *max_cost* are
inclusive USD filters on ``totalCost``.

Each trace now includes an ``observationSummary`` with per-trace
token counts, model, tool-call list, and error/warning counts so
fleet-level cost analysis can attribute spend without fetching every
trace individually.\
""",
                        "operationId": "list_recent_traces_traces_recent_get",
                        "parameters": [
                            {
                                "name": "limit",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "type": "integer",
                                    "default": 10,
                                    "title": "Limit",
                                },
                            },
                            {
                                "name": "min_cost",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "anyOf": [{"type": "number"}, {"type": "null"}],
                                    "title": "Min Cost",
                                },
                            },
                            {
                                "name": "max_cost",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "anyOf": [{"type": "number"}, {"type": "null"}],
                                    "title": "Max Cost",
                                },
                            },
                        ],
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "array",
                                            "items": {
                                                "type": "object",
                                                "additionalProperties": True,
                                            },
                                            "title": "Response List Recent Traces Traces Recent Get",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/traces/{trace_id}": {
                    "get": {
                        "tags": ["Traces"],
                        "summary": "Get Trace Detail",
                        "description": """\
Return full Langfuse trace detail including all observations.

Callers that need the complete prompt/completion bodies, per-
observation token usage, or raw cost-details should use this
endpoint (one call per trace) rather than ``/traces/recent``,
which only returns aggregated summaries.\
""",
                        "operationId": "get_trace_detail_traces__trace_id__get",
                        "parameters": [
                            {
                                "name": "trace_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string", "title": "Trace Id"},
                            }
                        ],
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "title": "Response Get Trace Detail Traces  Trace Id  Get",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/candidates": {
                    "get": {
                        "tags": ["Candidates"],
                        "summary": "List Candidates",
                        "description": """\
List AGENT.md candidates for a repo.

By default returns only pending entries — validated and rejected
candidates are kept in the file as an audit trail but the UI
shouldn't re-surface them. Pass ``include_acted=true`` to fetch
everything.

When ``repo_id`` is ``"all"`` (or empty) the candidates from every
repo are aggregated into a single flat list, each tagged with its
owning ``repo_id`` so the UI can target validate/reject at the
correct per-board file. The synthetic ``"meta"`` board is skipped —
it has no candidates file.\
""",
                        "operationId": "list_candidates_candidates_get",
                        "parameters": [
                            {
                                "name": "repo_id",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "type": "string",
                                    "default": "",
                                    "title": "Repo Id",
                                },
                            },
                            {
                                "name": "include_acted",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "type": "boolean",
                                    "default": False,
                                    "title": "Include Acted",
                                },
                            },
                        ],
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "array",
                                            "items": {
                                                "$ref": "#/components/schemas/CandidateRead"
                                            },
                                            "title": "Response List Candidates Candidates Get",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/candidates/{candidate_id}/validate": {
                    "post": {
                        "tags": ["Candidates"],
                        "summary": "Validate Candidate",
                        "description": """\
File the audited-repo draft ticket and stamp the candidate.

The ticket lands on the repo whose board owns the candidates file
— same repo that retrospect was reviewing when it proposed the
rule — so refine + implement clone the right tree and edit the
right AGENT.md.\
""",
                        "operationId": "validate_candidate_candidates__candidate_id__validate_post",
                        "parameters": [
                            {
                                "name": "candidate_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string", "title": "Candidate Id"},
                            },
                            {
                                "name": "repo_id",
                                "in": "query",
                                "required": True,
                                "schema": {"type": "string", "title": "Repo Id"},
                            },
                        ],
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/CandidateRead"
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/candidates/{candidate_id}/reject": {
                    "post": {
                        "tags": ["Candidates"],
                        "summary": "Reject Candidate",
                        "description": """\
Mark the candidate rejected — no ticket is filed. The entry
stays in the file as audit trail but the UI hides it on the next
refresh.\
""",
                        "operationId": "reject_candidate_candidates__candidate_id__reject_post",
                        "parameters": [
                            {
                                "name": "candidate_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string", "title": "Candidate Id"},
                            },
                            {
                                "name": "repo_id",
                                "in": "query",
                                "required": True,
                                "schema": {"type": "string", "title": "Repo Id"},
                            },
                        ],
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/CandidateRead"
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/agents": {
                    "get": {
                        "tags": ["Agents"],
                        "summary": "List Enabled Agents",
                        "description": """\
Return the periodic-agent names enabled for *repo_id*.

When *repo_id* is missing, ``"all"``, or unknown, an empty list is
returned — the per-repo agent run endpoints each target a single
repo, so the aggregate board has nothing meaningful to offer (the
frontend hides the dropdown there anyway).\
""",
                        "operationId": "list_enabled_agents_agents_get",
                        "parameters": [
                            {
                                "name": "repo_id",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "anyOf": [{"type": "string"}, {"type": "null"}],
                                    "title": "Repo Id",
                                },
                            }
                        ],
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                            "title": "Response List Enabled Agents Agents Get",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/board/cards": {
                    "get": {
                        "tags": ["Board"],
                        "summary": "Board Cards",
                        "description": """\
Return all tickets as card objects for the board JS hydration.

Mirrors ``GET /tickets`` but returns the flat card shape expected
by robotsix-board's ``board.js`` instead of the full ``TicketRead``
model.

``include_closed`` **defaults to False** — terminal states
(CLOSED, EPIC_CLOSED, ANSWERED) are excluded.  Pass
``include_closed=true`` to retrieve them.

Closed and epic-closed cards are sorted by ``updated_at``
descending (most recent first); all other cards remain sorted by
``created_at`` ascending.\
""",
                        "operationId": "board_cards_board_cards_get",
                        "parameters": [
                            {
                                "name": "include_closed",
                                "in": "query",
                                "required": False,
                                "schema": {
                                    "type": "boolean",
                                    "default": False,
                                    "title": "Include Closed",
                                },
                            }
                        ],
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "array",
                                            "items": {
                                                "type": "object",
                                                "additionalProperties": True,
                                            },
                                            "title": "Response Board Cards Board Cards Get",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/board/move/{card_id}/{target_status}": {
                    "post": {
                        "tags": ["Board"],
                        "summary": "Board Move",
                        "description": """\
Move a card to a new column (robotsix-board move action).

Receives the ``POST`` from robotsix-board's ``board-card-move``
form (JSON_HYDRATION mode).  Translates to a ticket state
transition.\
""",
                        "operationId": "board_move_board_move__card_id___target_status__post",
                        "parameters": [
                            {
                                "name": "card_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string", "title": "Card Id"},
                            },
                            {
                                "name": "target_status",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string", "title": "Target Status"},
                            },
                        ],
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "additionalProperties": True,
                                            "title": "Response Board Move Board Move  Card Id   Target Status  Post",
                                        }
                                    }
                                },
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/chat-skill": {
                    "get": {
                        "tags": ["ChatSkill"],
                        "summary": "Chat Skill",
                        "description": """\
Return the chat-agent board skill as a SKILL.md document.

The response is ``text/markdown`` with YAML frontmatter so the
chat agent can consume it as a standard skill file.\
""",
                        "operationId": "chat_skill_chat_skill_get",
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {
                                    "text/plain": {"schema": {"type": "string"}}
                                },
                            }
                        },
                    }
                },
                "/repos/{repo_id}": {
                    "delete": {
                        "tags": ["Repos"],
                        "summary": "Deregister Repo",
                        "description": """\
Remove a runtime-registered repo from the machine-owned overlay.

Only repos with ``source="auto"`` (machine-registered) can be
deregistered.  Operator-configured repos (``source="config"``)
are permanent and return 403.  Unknown repos return 404.

After removal the overlay YAML is updated and the in-process
:class:`ReposRegistry` is hot-reloaded.\
""",
                        "operationId": "deregister_repo_repos__repo_id__delete",
                        "parameters": [
                            {
                                "name": "repo_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string", "title": "Repo Id"},
                            }
                        ],
                        "responses": {
                            "204": {"description": "Successful Response"},
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/tickets/ingest": {
                    "post": {
                        "tags": ["Tickets"],
                        "summary": "Ingest Ticket",
                        "description": """\
Create a ticket with creation-time dedup (``POST /tickets/ingest``).

Returns 201 with ``deduped=False`` when a new ticket is created.
Returns 200 with ``deduped=True`` when the report matches an
existing ticket (a history note is appended to the existing one).
Returns 404 when *repo_id* is not registered.\
""",
                        "operationId": "ingest_ticket_tickets_ingest_post",
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/TicketIngest"
                                    }
                                }
                            },
                            "required": True,
                        },
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {"application/json": {"schema": {}}},
                            },
                            "422": {
                                "description": "Validation Error",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/HTTPValidationError"
                                        }
                                    }
                                },
                            },
                        },
                    }
                },
                "/metrics": {
                    "get": {
                        "summary": "Metrics",
                        "description": "Endpoint that serves Prometheus metrics.",
                        "operationId": "metrics_metrics_get",
                        "responses": {
                            "200": {
                                "description": "Successful Response",
                                "content": {"application/json": {"schema": {}}},
                            }
                        },
                    }
                },
            },
            "components": {
                "schemas": {
                    "Body_upload_screenshot_tickets__ticket_id__screenshots_post": {
                        "properties": {
                            "file": {
                                "type": "string",
                                "contentMediaType": "application/octet-stream",
                                "title": "File",
                            }
                        },
                        "type": "object",
                        "required": ["file"],
                        "title": "Body_upload_screenshot_tickets__ticket_id__screenshots_post",
                    },
                    "CandidateRead": {
                        "properties": {
                            "candidate_id": {"type": "string", "title": "Candidate Id"},
                            "section": {"type": "string", "title": "Section"},
                            "rule": {"type": "string", "title": "Rule"},
                            "rationale": {"type": "string", "title": "Rationale"},
                            "proposed_at": {"type": "string", "title": "Proposed At"},
                            "source_ticket": {
                                "type": "string",
                                "title": "Source Ticket",
                            },
                            "status": {"type": "string", "title": "Status"},
                            "filed_ticket": {
                                "anyOf": [{"type": "string"}, {"type": "null"}],
                                "title": "Filed Ticket",
                            },
                            "repo_id": {"type": "string", "title": "Repo Id"},
                        },
                        "type": "object",
                        "required": [
                            "candidate_id",
                            "section",
                            "rule",
                            "rationale",
                            "proposed_at",
                            "source_ticket",
                            "status",
                            "filed_ticket",
                            "repo_id",
                        ],
                        "title": "CandidateRead",
                        "description": "JSON shape returned to the board UI.",
                    },
                    "Comment": {
                        "properties": {
                            "id": {
                                "anyOf": [{"type": "integer"}, {"type": "null"}],
                                "title": "Id",
                            },
                            "ticket_id": {"type": "string", "title": "Ticket Id"},
                            "body": {"type": "string", "title": "Body"},
                            "author": {
                                "type": "string",
                                "title": "Author",
                                "default": "user",
                            },
                            "parent_id": {
                                "anyOf": [{"type": "integer"}, {"type": "null"}],
                                "title": "Parent Id",
                            },
                            "closed_at": {
                                "anyOf": [
                                    {"type": "string", "format": "date-time"},
                                    {"type": "null"},
                                ],
                                "title": "Closed At",
                            },
                            "created_at": {
                                "type": "string",
                                "format": "date-time",
                                "title": "Created At",
                            },
                        },
                        "type": "object",
                        "required": ["ticket_id", "body"],
                        "title": "Comment",
                        "description": """\
Reviewer comment on a ticket — supports threading via parent_id
and open/closed tracking on top-level threads via closed_at.\
""",
                    },
                    "CommentCreate": {
                        "properties": {
                            "body": {"type": "string", "title": "Body"},
                            "author": {
                                "type": "string",
                                "title": "Author",
                                "default": "user",
                            },
                            "parent_id": {
                                "anyOf": [{"type": "integer"}, {"type": "null"}],
                                "title": "Parent Id",
                            },
                        },
                        "type": "object",
                        "required": ["body"],
                        "title": "CommentCreate",
                        "description": "API request shape for creating a comment (optionally threaded via parent_id).",
                    },
                    "HTTPValidationError": {
                        "properties": {
                            "detail": {
                                "items": {
                                    "$ref": "#/components/schemas/ValidationError"
                                },
                                "type": "array",
                                "title": "Detail",
                            }
                        },
                        "type": "object",
                        "title": "HTTPValidationError",
                    },
                    "RepoRegistration": {
                        "properties": {
                            "repo_id": {"type": "string", "title": "Repo Id"},
                            "forge_remote_url": {
                                "type": "string",
                                "title": "Forge Remote Url",
                            },
                            "board_id": {
                                "anyOf": [{"type": "string"}, {"type": "null"}],
                                "title": "Board Id",
                            },
                        },
                        "type": "object",
                        "required": ["repo_id", "forge_remote_url"],
                        "title": "RepoRegistration",
                        "description": "Request body for POST /repos.",
                    },
                    "RepoRegistrationResult": {
                        "properties": {
                            "repo_id": {"type": "string", "title": "Repo Id"},
                            "board_id": {"type": "string", "title": "Board Id"},
                            "forge_remote_url": {
                                "anyOf": [{"type": "string"}, {"type": "null"}],
                                "title": "Forge Remote Url",
                            },
                            "registered": {"type": "boolean", "title": "Registered"},
                        },
                        "type": "object",
                        "required": [
                            "repo_id",
                            "board_id",
                            "forge_remote_url",
                            "registered",
                        ],
                        "title": "RepoRegistrationResult",
                        "description": "Response body for POST /repos.",
                    },
                    "State": {
                        "type": "string",
                        "enum": [
                            "draft",
                            "human_issue_approval",
                            "ready",
                            "documenting",
                            "code_review",
                            "deliverable",
                            "human_mr_approval",
                            "implement_complete",
                            "waiting_auto_merge",
                            "maintenance",
                            "rebasing",
                            "fixing_ci",
                            "addressing_review",
                            "done",
                            "closed",
                            "errored",
                            "blocked",
                            "asked",
                            "answered",
                            "awaiting_user_reply",
                            "epic_open",
                            "epic_closed",
                        ],
                        "title": "State",
                    },
                    "TicketCreate": {
                        "properties": {
                            "title": {"type": "string", "title": "Title"},
                            "description": {
                                "type": "string",
                                "title": "Description",
                                "default": "",
                            },
                            "depends_on": {
                                "anyOf": [{"type": "string"}, {"type": "null"}],
                                "title": "Depends On",
                            },
                            "unblocks": {
                                "anyOf": [
                                    {"items": {"type": "string"}, "type": "array"},
                                    {"type": "null"},
                                ],
                                "title": "Unblocks",
                            },
                            "source": {
                                "type": "string",
                                "title": "Source",
                                "default": "user",
                            },
                            "kind": {
                                "$ref": "#/components/schemas/TicketKind",
                                "default": "task",
                            },
                            "parent_id": {
                                "anyOf": [{"type": "string"}, {"type": "null"}],
                                "title": "Parent Id",
                            },
                            "repo_id": {
                                "anyOf": [{"type": "string"}, {"type": "null"}],
                                "title": "Repo Id",
                            },
                        },
                        "type": "object",
                        "required": ["title"],
                        "title": "TicketCreate",
                        "description": "API request shape for creating a new ticket.",
                    },
                    "TicketEvent": {
                        "properties": {
                            "id": {
                                "anyOf": [{"type": "integer"}, {"type": "null"}],
                                "title": "Id",
                            },
                            "ticket_id": {"type": "string", "title": "Ticket Id"},
                            "state": {"$ref": "#/components/schemas/State"},
                            "note": {
                                "anyOf": [{"type": "string"}, {"type": "null"}],
                                "title": "Note",
                            },
                            "at": {
                                "type": "string",
                                "format": "date-time",
                                "title": "At",
                            },
                            "prev_hash": {
                                "anyOf": [{"type": "string"}, {"type": "null"}],
                                "title": "Prev Hash",
                            },
                            "hash": {"type": "string", "title": "Hash", "default": ""},
                        },
                        "type": "object",
                        "required": ["ticket_id", "state"],
                        "title": "TicketEvent",
                        "description": "Append-only state-transition history with hash-chain integrity.",
                    },
                    "TicketIngest": {
                        "properties": {
                            "repo_id": {"type": "string", "title": "Repo Id"},
                            "title": {"type": "string", "title": "Title"},
                            "body": {"type": "string", "title": "Body"},
                            "source_tag": {"type": "string", "title": "Source Tag"},
                        },
                        "type": "object",
                        "required": ["repo_id", "title", "body", "source_tag"],
                        "title": "TicketIngest",
                        "description": "Payload for ``POST /tickets/ingest``.",
                    },
                    "TicketKind": {
                        "type": "string",
                        "enum": ["task", "inquiry", "epic"],
                        "title": "TicketKind",
                        "description": """\
Enumeration of ticket kinds — canonical source of truth for ``kind`` values.

Persisted as the canonical UPPERCASE member *name* via
``CaseTolerantEnum`` (below).  ``State`` is the other name-mapped
StrEnum and is intentionally left with its auto-generated ``Enum``
column — not in scope for this ticket.\
""",
                    },
                    "TicketMigrate": {
                        "properties": {
                            "repo_id": {"type": "string", "title": "Repo Id"},
                            "note": {
                                "anyOf": [{"type": "string"}, {"type": "null"}],
                                "title": "Note",
                            },
                        },
                        "type": "object",
                        "required": ["repo_id"],
                        "title": "TicketMigrate",
                        "description": "API request shape for migrating a ticket to another board.",
                    },
                    "TicketRead": {
                        "properties": {
                            "id": {"type": "string", "title": "Id"},
                            "title": {"type": "string", "title": "Title"},
                            "state": {"$ref": "#/components/schemas/State"},
                            "kind": {"type": "string", "title": "Kind"},
                            "branch": {
                                "anyOf": [{"type": "string"}, {"type": "null"}],
                                "title": "Branch",
                            },
                            "parent_id": {
                                "anyOf": [{"type": "string"}, {"type": "null"}],
                                "title": "Parent Id",
                            },
                            "parent_title": {
                                "anyOf": [{"type": "string"}, {"type": "null"}],
                                "title": "Parent Title",
                            },
                            "source": {"type": "string", "title": "Source"},
                            "origin_session": {
                                "anyOf": [{"type": "string"}, {"type": "null"}],
                                "title": "Origin Session",
                            },
                            "origin_session_url": {
                                "anyOf": [{"type": "string"}, {"type": "null"}],
                                "title": "Origin Session Url",
                            },
                            "cost_usd": {"type": "number", "title": "Cost Usd"},
                            "pre_redraft_cost_usd": {
                                "type": "number",
                                "title": "Pre Redraft Cost Usd",
                                "default": 0.0,
                            },
                            "cumulative_cost": {
                                "anyOf": [{"type": "number"}, {"type": "null"}],
                                "title": "Cumulative Cost",
                            },
                            "depends_on": {
                                "anyOf": [{"type": "string"}, {"type": "null"}],
                                "title": "Depends On",
                            },
                            "unmet_deps": {
                                "items": {"type": "string"},
                                "type": "array",
                                "title": "Unmet Deps",
                            },
                            "unblocks": {
                                "items": {"type": "string"},
                                "type": "array",
                                "title": "Unblocks",
                                "default": [],
                            },
                            "dependencies": {
                                "items": {
                                    "additionalProperties": True,
                                    "type": "object",
                                },
                                "type": "array",
                                "title": "Dependencies",
                                "default": [],
                            },
                            "pr_url": {
                                "anyOf": [{"type": "string"}, {"type": "null"}],
                                "title": "Pr Url",
                            },
                            "retry_attempt": {
                                "type": "integer",
                                "title": "Retry Attempt",
                            },
                            "last_transient_error": {
                                "anyOf": [{"type": "string"}, {"type": "null"}],
                                "title": "Last Transient Error",
                            },
                            "next_retry_at": {
                                "anyOf": [
                                    {"type": "string", "format": "date-time"},
                                    {"type": "null"},
                                ],
                                "title": "Next Retry At",
                            },
                            "priority": {
                                "type": "boolean",
                                "title": "Priority",
                                "default": False,
                            },
                            "board_id": {
                                "type": "string",
                                "title": "Board Id",
                                "default": "",
                            },
                            "created_at": {
                                "type": "string",
                                "format": "date-time",
                                "title": "Created At",
                            },
                            "updated_at": {
                                "type": "string",
                                "format": "date-time",
                                "title": "Updated At",
                            },
                            "pending_question": {
                                "anyOf": [{"type": "string"}, {"type": "null"}],
                                "title": "Pending Question",
                            },
                        },
                        "type": "object",
                        "required": [
                            "id",
                            "title",
                            "state",
                            "kind",
                            "branch",
                            "parent_id",
                            "source",
                            "origin_session",
                            "origin_session_url",
                            "cost_usd",
                            "depends_on",
                            "unmet_deps",
                            "retry_attempt",
                            "last_transient_error",
                            "next_retry_at",
                            "created_at",
                            "updated_at",
                        ],
                        "title": "TicketRead",
                        "description": "API response shape for reading a ticket, including computed fields like unmet_deps and PR URL.",
                    },
                    "TicketTransition": {
                        "properties": {
                            "state": {"$ref": "#/components/schemas/State"},
                            "note": {
                                "anyOf": [{"type": "string"}, {"type": "null"}],
                                "title": "Note",
                            },
                        },
                        "type": "object",
                        "required": ["state"],
                        "title": "TicketTransition",
                        "description": "API request shape for transitioning a ticket to a new state.",
                    },
                    "ValidationError": {
                        "properties": {
                            "loc": {
                                "items": {
                                    "anyOf": [{"type": "string"}, {"type": "integer"}]
                                },
                                "type": "array",
                                "title": "Location",
                            },
                            "msg": {"type": "string", "title": "Message"},
                            "type": {"type": "string", "title": "Error Type"},
                            "input": {"title": "Input"},
                            "ctx": {"type": "object", "title": "Context"},
                        },
                        "type": "object",
                        "required": ["loc", "msg", "type"],
                        "title": "ValidationError",
                    },
                }
            },
            "tags": [
                {
                    "name": "Health",
                    "description": "Liveness, readiness, and service health probes",
                },
                {
                    "name": "Tickets",
                    "description": "Ticket CRUD, transitions, events, and metadata",
                },
                {"name": "Comments", "description": "Ticket comment management"},
                {"name": "Epics", "description": "Epic grouping and management"},
                {
                    "name": "Passes",
                    "description": "Solver passes and ticket processing",
                },
                {
                    "name": "Traces",
                    "description": "Execution traces and agent run history",
                },
                {
                    "name": "Candidates",
                    "description": "Merge request candidate inspection",
                },
                {"name": "Agents", "description": "Agent lifecycle and status"},
                {
                    "name": "Board",
                    "description": "Board card management and workflow transitions",
                },
                {"name": "Repos", "description": "Runtime repo registration"},
            ],
        }
    )
