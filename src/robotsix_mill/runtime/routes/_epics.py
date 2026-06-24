"""Epic creation and child-generation routes."""

from __future__ import annotations

import logging
import threading
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from ...core.models import TicketKind, TicketRead
from ..deps import (
    enrich_ticket_read,
    get_run_registry,
    get_service,
    get_settings,
)
from ._tickets import _repo_config_for_ticket

log = logging.getLogger(__name__)

router = APIRouter(tags=["Epics"])


@router.post("/epics", response_model=TicketRead, status_code=201)
def create_epic(
    body: dict,
    request: Request,
    svc=Depends(get_service),
    settings=Depends(get_settings),
) -> TicketRead:
    """Create a new epic — accepts ``{"title": str, "description": str}``.

    An optional ``repo_id`` field scopes the epic to a specific repo's
    board.  When omitted in single-repo mode the sole repo is used;
    in multi-repo mode ``repo_id`` is required and a 400 is returned
    if it is missing.
    """
    title = body.get("title", "")
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    description = body.get("description", "")
    repo_id = body.get("repo_id")
    repos = request.app.state.repos
    board_id = ""
    if repo_id == "meta":
        # The synthetic cross-repo meta board is selectable in the UI
        # and queryable via ?repo_id=meta, but it is not a registered
        # repo. Accept it here so creating an epic on the meta board
        # works instead of 400-ing as an "unknown repo".
        board_id = "meta"
    elif repo_id:
        if repo_id not in repos.repos:
            sorted_keys = sorted(repos.repos.keys())
            raise HTTPException(
                status_code=400,
                detail=f"Unknown repo: '{repo_id}'. Known repos: {sorted_keys}",
            )
        board_id = repos.repos[repo_id].board_id
    elif len(repos.repos) == 1:
        board_id = next(iter(repos.repos.values())).board_id
    else:
        # Multi-repo mode with no repo_id: require it.
        sorted_keys = sorted(repos.repos.keys())
        raise HTTPException(
            status_code=400,
            detail=f"repo_id is required when multiple repos are configured. "
            f"Available repos: {sorted_keys}",
        )

    try:
        ticket = svc.create(
            title=title,
            description=description,
            kind=TicketKind.EPIC,
            board_id=board_id or None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return enrich_ticket_read(ticket, settings, svc)


@router.get(
    "/tickets/{ticket_id}/children",
    response_model=list[TicketRead],
)
def list_children(
    ticket_id: str,
    request: Request,
    svc=Depends(get_service),
    settings=Depends(get_settings),
) -> list[TicketRead]:
    """Return all tickets whose ``parent_id`` equals *ticket_id*."""
    parent = svc.get(ticket_id)
    if parent is None:
        raise HTTPException(404, "ticket not found")
    repo_config = _repo_config_for_ticket(parent, request.app.state.repos)
    return [
        enrich_ticket_read(
            t,
            settings,
            svc,
            blocking_cost=False,
            fetch_pr_url=False,
            repo_config=repo_config,
        )
        for t in svc.list_children(ticket_id)
    ]


@router.post("/tickets/{ticket_id}/generate-children", status_code=202)
def generate_children(  # noqa: C901
    ticket_id: str,
    request: Request,
    svc=Depends(get_service),
    settings=Depends(get_settings),
    registry=Depends(get_run_registry),
) -> dict:
    """Generate child tickets from an epic description using the LLM
    epic-breakdown agent.  Returns ``202 Accepted`` immediately — the
    agent runs in a background thread.

    Returns ``400`` if the ticket is not an epic.  Returns ``404`` if
    the ticket does not exist.
    """
    ticket = svc.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket not found")
    if ticket.kind != TicketKind.EPIC:
        raise HTTPException(400, "ticket is not an epic")

    # In multi-repo mode the default svc is bound to the first repo's
    # board. ``svc.get`` finds the epic via the cross-board fanout and
    # resolves ``ticket.board_id``, but subsequent writes (``create``,
    # ``set_depends_on``, ``set_content_hash``) default to the bound
    # service's board — so an auto-mail epic would silently spawn its
    # children on the mill board. Use a service pinned to the epic's
    # actual board for every mutation in the background runner.
    from ...core.service import TicketService as _TicketService

    epic_board_id = ticket.board_id or svc.board_id
    epic_svc = _TicketService(settings, board_id=epic_board_id)

    # Resolve the RepoConfig for the epic's board so the background
    # run emits Langfuse traces under the right project — without
    # this the agent call runs outside any ``start_ticket_root_span``
    # context, every span lacks the ``langfuse.public_key`` attribute
    # the filtered exporter routes on, and the trace is dropped.
    repos = request.app.state.repos
    epic_repo_config = _repo_config_for_ticket(ticket, repos)

    run_id = registry.start("epic-breakdown", repo_id=epic_board_id)

    def _run() -> None:  # noqa: C901
        try:
            from .. import tracing
            from ...agents.epic_breakdown import (
                plan_child_dependencies,
                run_epic_breakdown_agent,
            )
            from ...config.repos import resolve_child_board_id

            session_id = ticket_id
            with tracing.start_ticket_root_span(
                session_id,
                "epic-breakdown",
                repo_config=epic_repo_config,
                extra_attributes={"ticket_id": ticket_id},
            ) as root:
                root.set_input(
                    {
                        "ticket_id": ticket_id,
                        "epic_title": ticket.title,
                    }
                )
                description = epic_svc.workspace(ticket).read_description()

                # Build the available-repos list for the agent prompt.
                available_repos: list[tuple[str, str]] = []
                epic_repo_id = ""
                for rid, rc in repos.repos.items():
                    available_repos.append((rid, rc.board_id))
                    if rc.board_id == epic_board_id:
                        epic_repo_id = rid

                result = run_epic_breakdown_agent(
                    settings=settings,
                    epic_title=ticket.title,
                    epic_description=description,
                    available_repos=available_repos or None,
                    epic_repo_id=epic_repo_id,
                )
                # Advisory pre-filing dedup: flag (never drop) children
                # whose scope overlaps a recent ticket or an earlier
                # sibling in this batch. Best-effort — a failure must not
                # block filing.
                from datetime import datetime, timezone

                from ...core.dedup import annotate_child_body, find_child_overlaps

                child_titles = list(result.child_titles)
                child_bodies = list(result.child_bodies)
                child_repo_ids = list(result.child_repo_ids)

                # Tolerate short repo_ids list (default missing entries to epic repo).
                if len(child_repo_ids) < len(child_titles):
                    child_repo_ids.extend(
                        [""] * (len(child_titles) - len(child_repo_ids))
                    )
                # Truncate extra repo_ids beyond titles.
                child_repo_ids = child_repo_ids[: len(child_titles)]

                overlap_notes = find_child_overlaps(
                    epic_svc,
                    ticket_id,
                    child_titles,
                    child_bodies,
                    settings,
                    datetime.now(timezone.utc),
                )

                # Cache TicketService per board to avoid rebuilding.
                per_board_svc: dict[str, Any] = {}

                created_children: list[tuple[str, str, str]] = []
                for title, body, dup_note, repo_id in zip(
                    child_titles,
                    child_bodies,
                    overlap_notes,
                    child_repo_ids,
                    strict=True,
                ):
                    if dup_note:
                        log.warning(
                            "epic %s: child '%s' flagged as possible duplicate — %s",
                            ticket_id,
                            title,
                            dup_note,
                        )
                        body = annotate_child_body(body, dup_note)

                    # Resolve the child's target board.
                    child_board = resolve_child_board_id(
                        repo_id, epic_board_id, ticket_id, repos
                    )
                    if child_board not in per_board_svc:
                        per_board_svc[child_board] = _TicketService(
                            settings, board_id=child_board
                        )
                    child_svc = per_board_svc[child_board]

                    child = child_svc.create(
                        title=title,
                        description=body,
                        kind=TicketKind.TASK,
                        parent_id=ticket_id,
                    )
                    created_children.append((child.id, title, body))
                created_ids = [cid for cid, _t, _b in created_children]

                # Dependency wiring: use a fan-out capable service (empty
                # board_id) for child_board_id lookups so cross-board
                # children can be resolved.  The create_child callable
                # must also create on the correct child board.
                fanout_svc = _TicketService(settings)

                def _child_board_id(cid: str) -> str:
                    t = fanout_svc.get(cid)
                    return t.board_id if t is not None else epic_board_id

                def _create_child(title: str, body: str) -> str:
                    # Bump children go on the epic's own board (they are
                    # cross-cutting infrastructure tickets).
                    return epic_svc.create(
                        title=title,
                        description=body,
                        kind=TicketKind.TASK,
                        parent_id=ticket_id,
                    ).id

                for child_id, deps in plan_child_dependencies(
                    created_children,
                    child_board_id=_child_board_id,
                    create_child=_create_child,
                ).items():
                    epic_svc.set_depends_on(child_id, deps)

                # Apply the revised epic body to the epic immediately
                # (generate-children is a one-shot manual trigger).
                if result.epic_body and result.epic_body.strip():
                    new_hash = epic_svc.workspace(ticket).write_description(
                        result.epic_body.strip()
                    )
                    epic_svc.set_content_hash(ticket_id, new_hash)

                summary = (
                    f"Created {len(created_ids)} children: "
                    f"{', '.join(created_ids[:5])}"
                    f"{'…' if len(created_ids) > 5 else ''}"
                )
                root.set_output(
                    {
                        "children_created": len(created_ids),
                        "child_ids": created_ids,
                        "epic_body_updated": bool(
                            result.epic_body and result.epic_body.strip()
                        ),
                    }
                )
                # Record the breakdown in the epic's own history so the
                # drawer shows what happened (and how much it cost).
                # add_step_event uses the current state, so this stays
                # in EPIC_OPEN — no state machine churn.
                try:
                    body_changed = bool(result.epic_body and result.epic_body.strip())
                    epic_svc.add_step_event(
                        ticket_id,
                        "epic-breakdown: spawned "
                        f"{len(created_ids)} child(ren)"
                        + (" + revised epic body" if body_changed else ""),
                    )
                except Exception:
                    log.exception(
                        "epic-breakdown: add_step_event failed for %s",
                        ticket_id,
                    )
            registry.finish_ok(run_id, summary)
            log.info(
                "epic-breakdown done: %d children for %s",
                len(created_ids),
                ticket_id,
            )
        except Exception as e:  # noqa: BLE001 — background; just log
            log.exception("epic-breakdown failed for %s", ticket_id)
            registry.finish_error(run_id, str(e))

    threading.Thread(
        target=_run, name=f"epic-breakdown-{ticket_id}", daemon=True
    ).start()
    return {"status": "started"}
