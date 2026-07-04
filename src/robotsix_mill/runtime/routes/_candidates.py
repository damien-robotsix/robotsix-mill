"""AGENT.md candidate routes — list, validate, reject.

These endpoints expose the per-board ``AGENT_CANDIDATES.md`` file
(written by the retrospect stage) to the board UI. Validating an entry
files an audited-repo draft ticket whose body proposes the AGENT.md
edit, then stamps the candidate's Status line so it's not surfaced
again.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ...agents.candidates import (
    Candidate,
    candidates_path,
    load_candidates,
    prune_candidates,
    to_ticket_payload,
    update_status,
)
from ...core.models import SourceKind
from ...core.service import TicketService
from ..deps import get_settings, get_worker, maybe_enqueue

log = logging.getLogger(__name__)

# Per-module router, aggregated by routes/__init__.py via
# include_router (the post-#467 routes-split convention).
router = APIRouter(tags=["Candidates"])


class CandidateRead(BaseModel):
    """JSON shape returned to the board UI."""

    candidate_id: str
    section: str
    rule: str
    rationale: str
    proposed_at: str
    source_ticket: str
    status: str
    filed_ticket: str | None
    repo_id: str


def _to_read(c: Candidate, repo_id: str) -> CandidateRead:
    return CandidateRead(
        candidate_id=c.candidate_id,
        section=c.section,
        rule=c.rule,
        rationale=c.rationale,
        proposed_at=c.proposed_at,
        source_ticket=c.source_ticket,
        status=c.status,
        filed_ticket=c.filed_ticket,
        repo_id=repo_id,
    )


def _resolve_board(repo_id: str, request: Request):
    """Resolve *repo_id* to its ``RepoConfig`` or raise 400. UNlike the
    ticket routes we don't allow ``"all"`` or an empty value — the
    candidates file is strictly per-board."""
    repos = request.app.state.repos
    if not repo_id or repo_id not in repos.repos:
        sorted_keys = sorted(repos.repos.keys())
        raise HTTPException(
            status_code=400,
            detail=f"Unknown repo: '{repo_id}'. Known repos: {sorted_keys}",
        )
    return repos.repos[repo_id]


@router.get(
    "/candidates",
    response_model=list[CandidateRead],
)
def list_candidates(
    request: Request,
    repo_id: str = "",
    include_acted: bool = False,
    settings=Depends(get_settings),
) -> list[CandidateRead]:
    """List AGENT.md candidates for a repo.

    By default returns only pending entries — validated and rejected
    candidates are kept in the file as an audit trail but the UI
    shouldn't re-surface them. Pass ``include_acted=true`` to fetch
    everything.

    When ``repo_id`` is ``"all"`` (or empty) the candidates from every
    repo are aggregated into a single flat list, each tagged with its
    owning ``repo_id`` so the UI can target validate/reject at the
    correct per-board file. The synthetic ``"meta"`` board is skipped —
    it has no candidates file."""
    if not repo_id or repo_id == "all":
        out: list[CandidateRead] = []
        for rc in request.app.state.repos.repos.values():
            if rc.repo_id == "meta":
                continue
            path = candidates_path(settings.data_dir, rc.repo_id)
            cands = load_candidates(path)
            if not include_acted:
                cands = [c for c in cands if c.status == "pending"]
            out.extend(_to_read(c, rc.repo_id) for c in cands)
        return out
    rc = _resolve_board(repo_id, request)
    path = candidates_path(settings.data_dir, rc.repo_id)
    cands = load_candidates(path)
    if not include_acted:
        cands = [c for c in cands if c.status == "pending"]
    return [_to_read(c, rc.repo_id) for c in cands]


@router.post(
    "/candidates/{candidate_id}/validate",
    response_model=CandidateRead,
)
def validate_candidate(
    candidate_id: str,
    repo_id: str,
    request: Request,
    settings=Depends(get_settings),
    worker=Depends(get_worker),
) -> CandidateRead:
    """File the audited-repo draft ticket and stamp the candidate.

    The ticket lands on the repo whose board owns the candidates file
    — same repo that retrospect was reviewing when it proposed the
    rule — so refine + implement clone the right tree and edit the
    right AGENT.md.
    """
    rc = _resolve_board(repo_id, request)
    path = candidates_path(settings.data_dir, rc.repo_id)
    cands = load_candidates(path)
    target = next((c for c in cands if c.candidate_id == candidate_id), None)
    if target is None:
        raise HTTPException(404, "candidate not found")
    if target.status != "pending":
        raise HTTPException(
            409,
            f"candidate already {target.status}"
            + (f" → {target.filed_ticket}" if target.filed_ticket else ""),
        )

    title, body = to_ticket_payload(target)
    svc = TicketService(settings, board_id=rc.repo_id)
    try:
        ticket = svc.create(
            title,
            body,
            source=SourceKind.RETROSPECT,
            board_id=rc.repo_id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    maybe_enqueue(ticket, worker)

    updated = update_status(
        path,
        candidate_id,
        new_status="validated",
        filed_ticket=ticket.id,
    )
    if updated is None:
        # Vanishingly rare — the file changed between load and update.
        # The ticket exists, log so an operator can clean up by hand.
        log.warning(
            "candidate %s: ticket %s filed but status stamp failed; "
            "the entry may re-appear in the UI",
            candidate_id,
            ticket.id,
        )
        # Fall back to returning a synthesised record so the UI sees
        # the action took.
        updated = Candidate(
            candidate_id=target.candidate_id,
            section=target.section,
            rule=target.rule,
            rationale=target.rationale,
            proposed_at=target.proposed_at,
            source_ticket=target.source_ticket,
            status="validated",
            filed_ticket=ticket.id,
        )
    # Best-effort prune: resolved entries may now exceed the cap.
    try:
        dropped = prune_candidates(path, settings.retrospect_candidates_max_entries)
        if dropped:
            log.info(
                "validate_candidate: pruned %d resolved entries from %s",
                dropped,
                path,
            )
    except Exception:
        log.warning(
            "validate_candidate: prune_candidates failed for %s",
            path,
            exc_info=True,
        )
    return _to_read(updated, rc.repo_id)


@router.post(
    "/candidates/{candidate_id}/reject",
    response_model=CandidateRead,
)
def reject_candidate(
    candidate_id: str,
    repo_id: str,
    request: Request,
    settings=Depends(get_settings),
) -> CandidateRead:
    """Mark the candidate rejected — no ticket is filed. The entry
    stays in the file as audit trail but the UI hides it on the next
    refresh."""
    rc = _resolve_board(repo_id, request)
    path = candidates_path(settings.data_dir, rc.repo_id)
    updated = update_status(path, candidate_id, new_status="rejected")
    if updated is None:
        raise HTTPException(404, "candidate not found")
    # Best-effort prune: resolved entries may now exceed the cap.
    try:
        dropped = prune_candidates(path, settings.retrospect_candidates_max_entries)
        if dropped:
            log.info(
                "reject_candidate: pruned %d resolved entries from %s",
                dropped,
                path,
            )
    except Exception:
        log.warning(
            "reject_candidate: prune_candidates failed for %s",
            path,
            exc_info=True,
        )
    return _to_read(updated, rc.repo_id)
