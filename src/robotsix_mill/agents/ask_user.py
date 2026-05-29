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

        from ..runtime.tracing import current_session

        ticket_id = current_session()
        if ticket_id is None:
            return "Error: no active ticket session — cannot determine current ticket."

        from ..core.service import TicketService

        svc = TicketService(settings)
        try:
            svc.add_comment(
                ticket_id,
                f"[ASK_USER]\n\n{question}",
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
