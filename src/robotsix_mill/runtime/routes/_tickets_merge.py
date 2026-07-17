"""Merge & CI ticket routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from ...config import Settings
from ...config.repos import target_branch_for
from ...core.models import TicketRead
from ...core.service import TicketService
from ...core.states import State
from ...forge import get_forge
from ...stages.merge import (
    _verify_merge_ancestor,
    _changelog_warnings_for_ticket,
)
from ..deps import (
    enrich_ticket_read,
    get_service,
    get_settings,
    get_worker,
    maybe_enqueue,
    resolve_ticket_id,
)
from ._tickets import _repo_config_for_ticket

log = logging.getLogger(__name__)

router = APIRouter(tags=["Tickets"])


def _workspace_repo_dir_from_svc(svc, ticket) -> str | None:
    """Return the ticket's workspace clone dir, or None if missing."""
    ws = svc.workspace(ticket)
    repo = ws.dir / "repo"
    if not (repo / ".git").exists():
        return None
    return str(repo)


@router.post("/tickets/{ticket_id}/merge-now", response_model=TicketRead)
def merge_now(
    ticket_id: str,
    request: Request,
    svc=Depends(get_service),
    worker=Depends(get_worker),
    settings=Depends(get_settings),
) -> TicketRead:
    """Merge the PR for a ticket in human_mr_approval directly via the
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
    protection, conflict, etc.).
    """
    ticket_id = resolve_ticket_id(ticket_id, svc)
    ticket = svc.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket not found")
    if ticket.state is not State.HUMAN_MR_APPROVAL:
        raise HTTPException(409, "ticket is not in human_mr_approval")

    repo_config = _repo_config_for_ticket(ticket, request.app.state.repos)

    # Multi-repo mode: when the deliver stage wrote ``pr_urls.json`` we
    # merge every touched repo's PR. Reuse the merge stage's helpers so
    # the manifest schema stays single-sourced.
    from ...stages.merge import _load_pr_urls, _repo_config_for_entry

    try:
        pr_entries = _load_pr_urls(svc.workspace(ticket).artifacts_dir)
    except ValueError as e:
        raise HTTPException(409, f"pr_urls.json corrupted: {e}") from e

    if pr_entries:
        merged_urls: list[str] = []
        for entry in pr_entries:
            repo_id = entry.get("repo_id", "")
            branch = entry.get("branch", "")
            url = entry.get("url", branch)
            rc = _repo_config_for_entry(entry)
            entry_forge = get_forge(settings, repo_config=rc)
            # Idempotent re-press: skip repos whose PR is already merged.
            pr = entry_forge.pr_status(source_branch=branch)
            if pr is None or pr.get("merged"):
                merged_urls.append(url)
                continue
            result = entry_forge.merge_pr(source_branch=branch)
            if not result["merged"]:
                raise HTTPException(
                    409, f"merge rejected for {repo_id}: {result['reason']}"
                )
            # Verify the merged commit actually reached the repo's target
            # branch before trusting merge_pr()'s success. A confirmed
            # non-ancestor blocks the DONE transition (best-effort allow
            # when there is no local clone or git errors).
            entry_repo_dir = svc.workspace(ticket).dir / "repos" / repo_id
            repo_dir = (
                str(entry_repo_dir) if (entry_repo_dir / ".git").exists() else None
            )
            sha = pr.get("sha", "")
            target = target_branch_for(settings, rc)
            if not _verify_merge_ancestor(repo_dir, sha, ticket.id, target):
                raise HTTPException(
                    409,
                    f"merge reported success for {repo_id} but commit "
                    f"{sha[:8] or '(none)'} is not on origin/{target} — "
                    "refusing to mark done",
                )
            merged_urls.append(pr.get("url", url))

        ticket = svc.transition(
            ticket_id,
            State.DONE,
            note=f"merged via board: {', '.join(merged_urls)}",
        )
        maybe_enqueue(ticket, worker)  # retrospect picks up DONE
        return enrich_ticket_read(ticket, settings, svc, repo_config=repo_config)

    # Single-repo path (unchanged).
    forge = get_forge(settings, repo_config=repo_config)
    pr = forge.pr_status(source_branch=ticket.branch)
    if pr is None:
        raise HTTPException(409, "no PR found for branch — nothing to merge")
    pr_url = pr.get("url", ticket.branch)

    result = forge.merge_pr(source_branch=ticket.branch)
    if not result["merged"]:
        raise HTTPException(409, result["reason"])

    # Verify the merged commit actually reached origin/<target> before
    # trusting merge_pr()'s success. A confirmed non-ancestor blocks the
    # DONE transition, leaving the ticket in HUMAN_MR_APPROVAL (best-effort
    # allow when there is no local clone or git errors).
    repo = svc.workspace(ticket).repo_dir
    repo_dir = str(repo) if (repo / ".git").exists() else None
    sha = pr.get("sha", "")
    target = target_branch_for(settings, repo_config)
    if not _verify_merge_ancestor(repo_dir, sha, ticket.id, target):
        raise HTTPException(
            409,
            f"merge reported success but commit {sha[:8] or '(none)'} is not "
            f"on origin/{target} — refusing to mark done",
        )

    ticket = svc.transition(
        ticket_id,
        State.DONE,
        note=f"merged via board: {pr_url}",
    )

    maybe_enqueue(ticket, worker)  # retrospect picks up DONE
    return enrich_ticket_read(ticket, settings, svc, repo_config=repo_config)


@router.get("/tickets/{ticket_id}/merge-info")
def get_merge_info(
    ticket_id: str,
    request: Request,
    svc=Depends(get_service),
    settings=Depends(get_settings),
) -> dict:
    """Return CI status, mergeable flag, and changed files for the PR/MR
    backing *ticket_id*.  Each forge call is individually resilient —
    a failure in one field does not crash the whole response."""
    ticket_id = resolve_ticket_id(ticket_id, svc)
    ticket = svc.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket not found")

    branch = ticket.branch or f"{settings.branch_prefix}{ticket_id}"
    repo_config = _repo_config_for_ticket(ticket, request.app.state.repos)

    # Resolve forge once; remains None when forge is not configured.
    forge = None
    try:
        forge = get_forge(settings, repo_config=repo_config)
    except RuntimeError:
        pass  # forge not configured

    # --- mergeable -------------------------------------------------------
    mergeable: bool | None = None
    if forge is not None:
        try:
            pr = forge.pr_status(source_branch=branch)
            if pr is not None:
                mergeable = pr.get("mergeable")
        except Exception:
            pass  # best-effort: mergeable is optional

    # --- CI conclusion / failing checks ----------------------------------
    ci_conclusion: str | None = None
    ci_failing: list[dict] = []
    if forge is not None:
        try:
            cs = forge.check_status(source_branch=branch)
            if cs is not None:
                ci_conclusion = cs.get("conclusion")
                if ci_conclusion == "failure":
                    ci_failing = [
                        {
                            "name": f.get("name", ""),
                            "summary": (f.get("summary") or "")[:200],
                        }
                        for f in (cs.get("failing") or [])
                    ]
        except Exception:
            pass  # best-effort: CI status is optional

    # --- files -----------------------------------------------------------
    files: list[dict] = []
    if forge is not None:
        try:
            raw = forge.pr_files(source_branch=branch)
            # Sort by total changes desc, cap at 50.
            raw.sort(
                key=lambda f: f.get("additions", 0) + f.get("deletions", 0),
                reverse=True,
            )
            files = raw[:50]
        except Exception:
            pass  # best-effort: file list is optional

    # --- changelog warnings ----------------------------------------------
    changelog_warnings: list[dict] = []
    if forge is not None:
        try:
            repo_dir = _workspace_repo_dir_from_svc(svc, ticket)
            changelog_warnings = _changelog_warnings_for_ticket(repo_dir, ticket_id)
        except Exception:
            pass  # best-effort: changelog is advisory

    return {
        "mergeable": mergeable,
        "ci_conclusion": ci_conclusion,
        "ci_failing": ci_failing,
        "files": files,
        "changelog_warnings": changelog_warnings,
    }


@router.get("/tickets/{ticket_id}/merge-reason")
def get_merge_reason(
    ticket_id: str,
    svc=Depends(get_service),
) -> dict:
    """Return the auto-merge blocking reason written by the merge
    stage, or an empty string when no reason has been recorded."""
    ticket_id = resolve_ticket_id(ticket_id, svc)
    ticket = svc.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket not found")
    reason_path = svc.workspace(ticket).artifacts_dir / "merge_reason.txt"
    if not reason_path.exists():
        return {"reason": ""}
    return {"reason": reason_path.read_text(encoding="utf-8").strip()}


@router.get("/tickets/{ticket_id}/merge-status")
def get_merge_status(
    ticket_id: str,
    request: Request,
    svc: TicketService = Depends(get_service),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    """Return live merge-readiness for a ticket's PR.

    Called by the ticket drawer before rendering the Merge button so
    the user sees *why* they can't merge right now (conflicts, failing
    CI, pending checks) instead of hitting a bare 409 from
    ``/merge-now``.  Returns ``can_merge: true`` on transient forge
    errors so the Merge button stays active — the actual merge
    endpoint handles the real rejection.
    """
    ticket_id = resolve_ticket_id(ticket_id, svc)
    ticket = svc.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket not found")

    # Only relevant for merge-ready states.  Everything else gets a
    # clean "no" so the drawer doesn't bother rendering a button.
    if ticket.state not in (
        State.HUMAN_MR_APPROVAL,
        State.WAITING_AUTO_MERGE,
        State.IMPLEMENT_COMPLETE,
    ):
        return {
            "mergeable": None,
            "ci_conclusion": None,
            "can_merge": False,
            "reason": f"ticket is not in a merge-relevant state (currently {ticket.state.value})",
            "changelog_warnings": [],
        }

    repo_config = _repo_config_for_ticket(ticket, request.app.state.repos)
    forge = get_forge(settings, repo_config=repo_config)

    # ── CHANGELOG warnings (advisory, non-blocking) ──────────────
    changelog_warnings: list[dict[str, str]] = []
    try:
        repo_dir = _workspace_repo_dir_from_svc(svc, ticket)
        changelog_warnings = _changelog_warnings_for_ticket(repo_dir, ticket_id)
    except Exception:  # CHANGELOG warnings are advisory only — silently skip
        pass

    # ── PR mergeability ──────────────────────────────────────────
    mergeable: bool | None = None
    try:
        pr = forge.pr_status(source_branch=ticket.branch)
    except Exception:
        # Transient forge error — stay optimistic; merge-now will
        # surface the real error if the user clicks.
        return {
            "mergeable": None,
            "ci_conclusion": None,
            "can_merge": True,
            "reason": "",
            "changelog_warnings": changelog_warnings,
        }

    if pr is None:
        return {
            "mergeable": None,
            "ci_conclusion": None,
            "can_merge": False,
            "reason": "No PR found for this branch",
            "changelog_warnings": changelog_warnings,
        }
    mergeable = pr.get("mergeable")

    # ── CI status ────────────────────────────────────────────────
    ci_conclusion: str | None = None
    try:
        ci = forge.check_status(source_branch=ticket.branch)
    except Exception:
        ci = None
    if ci is not None:
        ci_conclusion = ci.get("conclusion")

    # ── Compose result ───────────────────────────────────────────
    if mergeable is False:
        return {
            "mergeable": mergeable,
            "ci_conclusion": ci_conclusion,
            "can_merge": False,
            "reason": "PR has conflicts — rebase needed",
            "changelog_warnings": changelog_warnings,
        }
    if ci_conclusion == "failure":
        return {
            "mergeable": mergeable,
            "ci_conclusion": ci_conclusion,
            "can_merge": False,
            "reason": "CI checks are failing",
            "changelog_warnings": changelog_warnings,
        }
    if ci_conclusion == "pending":
        return {
            "mergeable": mergeable,
            "ci_conclusion": ci_conclusion,
            "can_merge": False,
            "reason": "CI checks are still running",
            "changelog_warnings": changelog_warnings,
        }

    return {
        "mergeable": mergeable,
        "ci_conclusion": ci_conclusion,
        "can_merge": True,
        "reason": "",
        "changelog_warnings": changelog_warnings,
    }
