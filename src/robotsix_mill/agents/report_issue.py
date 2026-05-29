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
from ..core.models import SourceKind

# Tickets in these states are "done with"; an identical title may be
# filed again (e.g. a regression of a previously-fixed issue). Any
# other state means it's still pending — don't duplicate it.
_DONE_WITH = {"closed", "done"}

_CATEGORIES = (
    "missing-tool", "error", "workflow-improvement",
    "missing-input", "code-quality", "other",
)


def make_report_issue_tool(
    settings: Settings,
    *,
    agent_name: str | None = None,
    board_id: str = "",
):
    """Return the ``report_issue`` closure bound to *settings*.

    Lazily constructs a ``TicketService`` per call so this stays cheap
    to attach to every agent and hermetic for tests.

    Args:
        settings: The application settings instance.
        agent_name: If provided, stamped into the ticket body so the
            originating agent is identifiable at a glance (e.g.
            ``"run_tests"``).  When ``None`` or empty, the generic
            wording is used.
        board_id: The board the host agent is running for. Threaded
            through to the ``TicketService`` so issues filed from
            inside an auto-mail run land on the auto-mail board,
            not on the default DB. Empty string preserves the
            legacy single-repo behaviour.
    """

    def report_issue(
        title: str,
        body: str = "",
        category: str = "other",
        evidence: str = "",
    ) -> str:
        """File a draft only for blocking issues that prevent completing the current task.

        Do NOT file for: cosmetic observations, style nits, nice-to-have
        improvements, or non-blocking observations. When in doubt, do NOT file.
        category: missing-tool|error|workflow-improvement|missing-input|other.
        Never raises."""
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

            service = TicketService(settings, board_id=board_id)

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

            if agent_name:
                full_body = (
                    f"**Reported by the `{agent_name}` agent** "
                    f"(category: {cat})\n\n"
                    f"{(body or '').strip()}\n"
                )
            else:
                full_body = (
                    f"**Reported by an agent** (category: {cat})\n\n"
                    f"{(body or '').strip()}\n"
                )

            evidence = (evidence or "").strip()
            if evidence:
                # Truncate at 8 KB to keep the workspace lean.
                evidence_bytes = evidence.encode("utf-8")
                if len(evidence_bytes) > 8192:
                    evidence_bytes = evidence_bytes[:8192]
                    # Decode back, replacing any trailing partial multi-byte char.
                    evidence = evidence_bytes.decode("utf-8", errors="ignore")
                else:
                    evidence = evidence_bytes.decode("utf-8")

            ticket = service.create(title, full_body, source=SourceKind.AGENT,
                                     origin_session=current_session())

            if evidence:
                workspace = service.workspace(ticket)
                artifacts = workspace.artifacts_dir
                (artifacts / "evidence.txt").write_text(evidence, encoding="utf-8")

                # Append a pointer line to the description so anyone
                # reading description.md knows to check the evidence file.
                full_body += "\n> Raw evidence attached at artifacts/evidence.txt\n"
                workspace.write_description(full_body)

            return f"report_issue: filed draft {ticket.id}"
        except Exception as e:  # noqa: BLE001 — never abort the agent run
            return f"report_issue: could not file ticket ({e!r})"

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(ToolInfo(
        name="report_issue",
        description="File a draft only for blocking issues preventing task completion.",
        category="reporting",
        parameters={"title": "str", "body": "str = \"\"", "category": "str = \"other\"", "evidence": "str = \"\""},
    ))

    return report_issue
