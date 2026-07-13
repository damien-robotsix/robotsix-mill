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
from ..core.service import TicketService
from ..core.states import DONE_OR_CLOSED
from ..core.text_noop import is_completion_announcement, is_noop_report
from ..runtime import tracing as _tracing

_CATEGORIES = (
    "missing-tool",
    "error",
    "workflow-improvement",
    "missing-input",
    "code-quality",
    "other",
)

_EVIDENCE_MAX = 64 * 1024


def _validate_title(title: str) -> str | None:
    """Validate *title*; return an error string or ``None`` to proceed."""
    title = title.strip()
    if not title:
        return "report_issue: a non-empty title is required"
    if is_completion_announcement(title):
        return (
            "report_issue: completion announcement suppressed — "
            "not filed (return your structured result instead)"
        )
    if is_noop_report(title):
        return "report_issue: no actionable issue — not filed (clean/no-op report)"
    return None


def _check_duplicate(title: str, service: TicketService) -> str | None:
    """Return a duplicate notice if *title* matches a non-terminal ticket."""
    norm = title.casefold()
    for t in service.list():
        if t.title.strip().casefold() == norm and t.state not in DONE_OR_CLOSED:
            return (
                f"report_issue: already filed as {t.id} "
                f"(state={t.state.value}) — not duplicating"
            )
    return None


def _build_body(body: str, category: str, agent_name: str | None) -> str:
    """Build the final ticket description body."""
    if agent_name:
        return (
            f"**Reported by the `{agent_name}` agent** "
            f"(category: {category})\n\n"
            f"{(body or '').strip()}\n"
        )
    return (
        f"**Reported by an agent** (category: {category})\n\n{(body or '').strip()}\n"
    )


def _attach_evidence(
    service: TicketService, ticket, full_body: str, evidence: str
) -> str:
    """Truncate and persist *evidence*, append pointer line to *full_body*."""
    evidence = evidence.strip()
    if not evidence:
        return full_body

    evidence_bytes = evidence.encode("utf-8")
    if len(evidence_bytes) > _EVIDENCE_MAX:
        evidence_bytes = evidence_bytes[:_EVIDENCE_MAX]
        evidence = evidence_bytes.decode("utf-8", errors="ignore")
    else:
        evidence = evidence_bytes.decode("utf-8")

    workspace = service.workspace(ticket)
    artifacts = workspace.artifacts_dir
    (artifacts / "evidence.txt").write_text(evidence, encoding="utf-8")

    full_body += "\n> Raw evidence attached at artifacts/evidence.txt\n"
    workspace.write_description(full_body)
    return full_body


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
            # Step 1: validate title
            err = _validate_title(title)
            if err:
                return err

            # Step 2: coerce category
            cat = category if category in _CATEGORIES else "other"

            # Step 3: dedup
            service = TicketService(settings, board_id=board_id)
            dup = _check_duplicate(title, service)
            if dup:
                return dup

            # Step 4: build body
            full_body = _build_body(body, cat, agent_name)

            # Step 5: create ticket — inherit priority from originating ticket
            origin_session = _tracing.current_session()
            origin_priority = False
            if origin_session:
                origin = service.get(origin_session)
                if origin is not None:
                    origin_priority = origin.priority
            ticket = service.create(
                title,
                full_body,
                source=SourceKind.AGENT,
                origin_session=origin_session,
                priority=origin_priority,
            )

            # Step 6: attach evidence (if any)
            if evidence and evidence.strip():
                full_body = _attach_evidence(service, ticket, full_body, evidence)

            return f"report_issue: filed draft {ticket.id}"
        except Exception as e:  # noqa: BLE001 — never abort the agent run
            return f"report_issue: could not file ticket ({e!r})"

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(
        ToolInfo(
            name="report_issue",
            description="File a draft only for blocking issues preventing task completion.",
            category="reporting",
            parameters={
                "title": "str",
                "body": 'str = ""',
                "category": 'str = "other"',
                "evidence": 'str = ""',
            },
        )
    )

    return report_issue
