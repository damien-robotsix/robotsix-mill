"""A ``ask_user`` tool for agents to pause a ticket and ask the
operator a clarifying question.

The refine and implement agents can call this tool when they hit
ambiguity instead of guessing or declaring BLOCKED.  It writes a
structured comment with a ``[ASK_USER]`` marker, returns a sentinel
string (``__ASK_USER_PAUSE__``) that the stage runner detects to
transition the ticket into ``AWAITING_USER_REPLY``, and is idempotent
so a retrying agent can't spam duplicate comments.
"""

from __future__ import annotations

from ..config import Settings
from ..core.states import ASK_USER_MARKER


def make_ask_user_tool(settings: Settings, agent_name: str):
    """Return the ``ask_user`` closure bound to *settings*.

    Args:
        settings: The application settings instance.
        agent_name: Stamped as the comment author so the originating
            agent is identifiable (e.g. ``"implement"``).
    """
    _called: list[bool] = [False]  # mutable for nonlocal semantics

    def ask_user(question: str) -> str:
        """Pause the current ticket and ask the operator a clarifying question.

        Use this ONLY for a genuine blocker you cannot resolve yourself:
        a mis-routed ticket (the spec targets code/paths that do not exist
        in this repo), a missing prerequisite, or genuinely ambiguous scope
        with no safe default.

        Do NOT use it to ask for permission or confirmation to PROCEED. If
        your implementation is complete and the tests pass, just finish —
        emit your structured output and stop. The pipeline reviews,
        documents, and delivers automatically; you never need sign-off to
        continue. Asking things like "the fix is done, ready for review,
        shall I proceed?" only pauses the ticket in awaiting_user_reply and
        stalls delivery until a human happens to reply.

        Returns a sentinel that stops the agent — the ticket will resume
        when the operator replies.

        Args:
            question: The question to ask the operator.
        """
        # Idempotency: if already called, return sentinel immediately
        # to prevent duplicate comments on retry.
        if _called[0]:
            return "__ASK_USER_PAUSE__"
        _called[0] = True

        from ._ticket_context import current_ticket_service

        result = current_ticket_service(settings)
        if result is None:
            return "Error: no active ticket session — cannot determine current ticket."
        _, ticket_id = result

        # Resolve the ticket's board BEFORE constructing the service,
        # otherwise the unbound service falls through to an empty
        # board_id and ``db.session(settings, "")`` raises ValueError
        # (empty board_id is no longer accepted). ``current_session()``
        # returns either the ticket id (ticket-driven flows) or a
        # periodic-session id like ``audit-...``; only the former
        # resolves to a row.
        #
        # When no repos.yaml is configured (single-repo dev / tests /
        # bespoke setups), fall back to the unbound service whose
        # ``_get_anywhere`` covers the legacy default DB plus any
        # disk-discovered boards.
        from ..config import get_repos_config
        from ..core.service import TicketService

        board_id = ""
        repos = get_repos_config().repos
        if repos:
            for rc in repos.values():
                probe = TicketService(settings, board_id=rc.board_id)
                if probe.get(ticket_id) is not None:
                    board_id = rc.board_id
                    break
            if not board_id:
                return (
                    "ask_user: could not resolve a board for "
                    f"session={ticket_id!r}; refusing to post the comment "
                    "(this happens when ask_user is invoked from a periodic "
                    "agent run — those have no owning ticket)."
                )

        svc = TicketService(settings, board_id=board_id)
        try:
            svc.add_comment(
                ticket_id,
                f"{ASK_USER_MARKER}\n\n{question}",
                author=agent_name,
            )
            return "__ASK_USER_PAUSE__"
        except Exception as e:
            return f"ask_user: could not post question ({e!r})"

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(
        ToolInfo(
            name="ask_user",
            description="Pause the current ticket and ask the operator a clarifying question. Returns a sentinel that stops the agent — the ticket will resume when the operator replies.",
            category="reporting",
            parameters={"question": "str"},
        )
    )

    return ask_user
