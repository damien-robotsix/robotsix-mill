"""A ``report_issue`` tool injected into *every* agent by ``build_agent``.

Any agent (coordinator, rebase, ci-fix, audit, refine, explore, …) can
file a draft ticket when it hits something that blocks or degrades it:
a missing tool, an unrecoverable error, a workflow improvement, missing
detail in its inputs, etc. The ticket is a normal ``source="agent"``
DRAFT, so it flows through refine (which dedups against existing work)
and the human approval gate like any other draft.

Hard requirement: this must never spam. An agent stuck in a loop (the
rebase ping-pong we just bounded is the cautionary tale) would
otherwise file the same ticket hundreds of times. So the tool refuses
to create a second ticket while a non-terminal one with the same title
already exists — it reports back that the issue is already filed.
"""

from __future__ import annotations

from ..config import Settings

# Tickets in these states are "done with"; an identical title may be
# filed again (e.g. a regression of a previously-fixed issue). Any
# other state means it's still pending — don't duplicate it.
_DONE_WITH = {"closed", "done"}

_CATEGORIES = (
    "missing-tool", "error", "workflow-improvement",
    "missing-input", "other",
)


def make_report_issue_tool(settings: Settings):
    """Return the ``report_issue`` closure bound to *settings*.

    Lazily constructs a ``TicketService`` per call so this stays cheap
    to attach to every agent and hermetic for tests."""

    def report_issue(
        title: str, body: str = "", category: str = "other"
    ) -> str:
        """File a draft ticket about a problem you hit while working.

        Use this when something blocks or degrades your execution and
        is worth fixing in the system itself — e.g. a tool you needed
        is missing, an unrecoverable error, a workflow that should be
        improved, or detail missing from your inputs. Do NOT use it for
        the normal task outcome; only for meta issues about the system.

        Args:
            title: short, specific, stable summary (used for dedup —
                keep it the same for the same underlying issue).
            body: what happened, what you expected, and a concrete
                suggestion if you have one.
            category: one of missing-tool, error, workflow-improvement,
                missing-input, other.

        Returns a short status string (never raises — a failure here
        must not abort your run).
        """
        try:
            title = (title or "").strip()
            if not title:
                return "report_issue: a non-empty title is required"

            # No-op guard: agents (esp. on a clean run) sometimes call
            # this to say "nothing to report" — that is noise, not a
            # ticket. Same shared detector the retrospect stage uses.
            from ..core.text_noop import is_noop_report

            if is_noop_report(title):
                return (
                    "report_issue: no actionable issue — not filed "
                    "(clean/no-op report)"
                )
            cat = category if category in _CATEGORIES else "other"

            from ..core.service import TicketService
            from ..runtime.tracing import current_session

            service = TicketService(settings)

            # Dedup: skip if a non-terminal ticket with this title is
            # already open (prevents loop spam).
            norm = title.casefold()
            for t in service.list():
                if (
                    t.title.strip().casefold() == norm
                    and t.state.value not in _DONE_WITH
                ):
                    return (
                        f"report_issue: already filed as {t.id} "
                        f"(state={t.state.value}) — not duplicating"
                    )

            full_body = (
                f"**Reported by an agent** (category: {cat})\n\n"
                f"{(body or '').strip()}\n"
            )
            ticket = service.create(title, full_body, source="agent",
                                     origin_session=current_session())
            return f"report_issue: filed draft {ticket.id}"
        except Exception as e:  # noqa: BLE001 — never abort the agent run
            return f"report_issue: could not file ticket ({e!r})"

    return report_issue
