"""Meta-pass integration runner.

Wires together the cross-repo clone primitive, the meta-agent,
board routing (extraction → meta board, alignment → per-repo
board), the cross-repo memory ledger, and mill-pinned tracing.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .agent import MetaAgentResult, run_meta_agent
from ..config import Settings, get_repos_config
from ..core.models import SourceKind
from ..core.service import TicketService
from ..runners.pass_runner import _format_recent_proposals, load_memory, persist_memory
from ..runtime.tracing import force_traces_to_mill
from ..vcs import clone_all_repos

log = logging.getLogger("robotsix_mill.meta.runner")


# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------


@dataclass
class MetaPassResult:
    """Result of a completed ``run_meta_pass`` invocation."""

    updated_memory: str
    extraction_drafts_created: list[dict]  # [{"id": ..., "title": ...}, ...]
    alignment_drafts_created: list[dict]
    todo_drafts_created: list[dict] = field(default_factory=list)
    session_id: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slugify_title(title: str) -> str:
    """Turn *title* into a short slug for gap-id markers."""
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40]


def _build_body_with_gap_id(body: str, gap_id: str) -> str:
    """Append a ``<!-- meta-gap-id: ... -->`` marker to *body*."""
    if body:
        body += "\n\n"
    return body + f"<!-- {SourceKind.META}-gap-id: {gap_id} -->"


def _file_extraction_drafts(
    drafts: list,
    settings: Settings,
    session_id: str,
) -> list[dict]:
    """File extraction drafts to the meta board.  Best-effort."""
    created: list[dict] = []
    meta_service = TicketService(settings, board_id="meta")
    for draft in drafts:
        gap_id = _slugify_title(draft.title)
        body = _build_body_with_gap_id(draft.body, gap_id)
        try:
            ticket = meta_service.create(
                title=draft.title,
                description=body,
                source=SourceKind.META,
                origin_session=session_id,
            )
            created.append({"id": ticket.id, "title": ticket.title})
            log.info("meta pass: filed extraction draft %r", ticket.id)
        except Exception:
            log.exception(
                "meta pass: failed to create extraction draft %r",
                draft.title,
            )
    return created


def _file_repo_drafts(
    drafts: list,
    settings: Settings,
    session_id: str,
    *,
    label: str,
) -> list[dict]:
    """File per-repo drafts to their target repo boards.  Best-effort.

    Shared by alignment and TODO filing — ``label`` (``"alignment"`` /
    ``"todo"``) is used only in log messages.
    """
    created: list[dict] = []
    repos_config = get_repos_config()
    for draft in drafts:
        if not draft.target_repo_id:
            log.warning(
                "meta_pass: %s draft %r has no target_repo_id — skipping",
                label,
                draft.title,
            )
            continue

        target_rc = repos_config.repos.get(draft.target_repo_id)
        if target_rc is None:
            log.warning(
                "meta_pass: %s draft %r targets unknown repo_id %r — skipping",
                label,
                draft.title,
                draft.target_repo_id,
            )
            continue

        gap_id = _slugify_title(draft.title)
        body = _build_body_with_gap_id(draft.body, gap_id)
        try:
            target_service = TicketService(settings, board_id=target_rc.repo_id)
            ticket = target_service.create(
                title=draft.title,
                description=body,
                source=SourceKind.META,
                origin_session=session_id,
            )
            created.append({"id": ticket.id, "title": ticket.title})
            log.info("meta pass: filed %s draft %r", label, ticket.id)
        except Exception:
            log.exception(
                "meta pass: failed to create %s draft %r",
                label,
                draft.title,
            )
    return created


def _gather_meta_proposals(settings: Settings) -> str:
    """Query the meta board + every registered repo board for
    ``source=SourceKind.META`` tickets, de-duplicate by id, and
    return a ``<recent_proposals>`` block for the meta-agent prompt.
    """
    repos_config = get_repos_config()
    all_tickets: dict[str, object] = {}

    # 1. Meta board
    meta_service = TicketService(settings, board_id="meta")
    for t in meta_service.recent_proposals_for(SourceKind.META, limit=100):
        all_tickets[t.id] = t

    # 2. Every registered repo board
    for repo_config in repos_config.repos.values():
        service = TicketService(settings, board_id=repo_config.repo_id)
        for t in service.recent_proposals_for(SourceKind.META, limit=100):
            all_tickets[t.id] = t

    return _format_recent_proposals(list(all_tickets.values()))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_meta_pass(session_id: str) -> MetaPassResult:
    """Run a full meta-agent pass end-to-end.

    1. Instantiate settings.
    2. Cross-repo clone (best-effort).
    3. Resolve mill repo config for tracing (graceful degradation).
    4. Load the meta memory ledger.
    5. Gather prior meta proposals from all relevant boards.
    6. Invoke the meta-agent.
    7. File extraction drafts to the meta board and alignment + TODO
       drafts to their target repo boards.
    8. Persist updated memory.
    """
    # 1. Instantiate settings
    settings = Settings()

    # 2. Cross-repo clone (best-effort; empty is not an error)
    repo_clones = clone_all_repos(settings)

    # 3. Resolve mill repo config for tracing
    mill_repo_id = settings.trace_review_target_repo_id
    tracer_ctx = None
    if mill_repo_id:
        repos_config = get_repos_config()
        mill_repo = repos_config.repos.get(mill_repo_id)
        if mill_repo is not None:
            tracer_ctx = force_traces_to_mill(mill_repo)
        else:
            log.info(
                "meta_pass: trace_review_target_repo_id=%r not found in "
                "repos config — traces will use default project",
                mill_repo_id,
            )
    else:
        log.info(
            "meta_pass: trace_review_target_repo_id not configured "
            "— traces will use default project"
        )

    if tracer_ctx is None:
        from contextlib import nullcontext

        tracer_ctx = nullcontext()

    with tracer_ctx:
        # 4. Resolve memory
        memory_file = settings.memory_file_for("meta", "meta")
        memory = load_memory(memory_file)

        # 5. Gather prior proposals
        recent_proposals = _gather_meta_proposals(settings)

        # 6. Invoke meta-agent
        result: MetaAgentResult = run_meta_agent(
            settings=settings,
            memory=memory,
            recent_proposals=recent_proposals,
            repo_clones=repo_clones,
        )

        # 7. File drafts
        extraction_created = _file_extraction_drafts(
            result.extraction_drafts, settings, session_id
        )
        alignment_created = _file_repo_drafts(
            result.alignment_drafts, settings, session_id, label="alignment"
        )
        todo_created = _file_repo_drafts(
            result.todo_drafts, settings, session_id, label="todo"
        )

        # 8. Persist memory
        persist_memory(memory_file, result.updated_memory)

    return MetaPassResult(
        updated_memory=result.updated_memory,
        extraction_drafts_created=extraction_created,
        alignment_drafts_created=alignment_created,
        todo_drafts_created=todo_created,
        session_id=session_id,
    )
