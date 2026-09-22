"""Infra account-block escalation + recovery (deterministic, no LLM).

When ``stages/ci_fix`` (or the CI-failure watcher) detects that GitHub
refused to start hosted jobs for an account (billing / spending-limit
block), it parks the ticket ``BLOCKED`` with a note carrying
:data:`~robotsix_mill.stages.ci_infra_block.INFRA_ACCOUNT_BLOCK_MARKER`.

The condition is *account-scoped*, not per-ticket: on 2026-09-18 GitHub
disabled hosted runners for every PRIVATE repo at once and 15 tickets
across three boards parked behind the single outage.  This pass, run on
the existing upstream-CI recovery loop, therefore:

1. collects every account-block-parked ticket across all boards;
2. raises **one** fleet-wide operator escalation naming the affected
   repos and the first-observed timestamp — deduped so it re-escalates
   only after ``infra_account_block_reescalate_seconds`` while unresolved;
3. reuses the upstream-CI recovery machinery to auto-resume each parked
   ticket once a hosted run on its repo succeeds again.

Pure forge status reads + state transitions — no AI agent, no tracing.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ...core.models import Ticket, TicketEvent
from ...core.service import TicketService
from ...core.states import State
from ...stages.ci_infra_block import INFRA_ACCOUNT_BLOCK_MARKER
from .timeout_escalation_runner import _boards_to_scan
from .upstream_ci_recovery_runner import (
    _refresh_pr_branch,
    _repo_for_board,
    _target_is_green,
)

if TYPE_CHECKING:
    from ...config import Settings

log = logging.getLogger("robotsix_mill.infra_account_block")

# escalate(repos, first_observed_iso) — inject in tests / to route the
# escalation to a real operator channel.  ``first_observed`` is ``None``
# when no block timestamp could be resolved.
EscalateFn = Callable[[list[str], str | None], None]


def _last_blocked_event(service: TicketService, ticket_id: str) -> TicketEvent | None:
    """Most recent ``BLOCKED`` history event for *ticket_id* (or ``None``)."""
    events: list[TicketEvent] = service.history(ticket_id, order="desc")
    for ev in events:
        if ev.state is State.BLOCKED:
            return ev
    return None


def _is_account_block_parked(note: str) -> bool:
    """True when *note* is an infra account-block park.

    The merge path prefixes multi-repo notes with ``[<repo_id>] ``; match
    the marker anywhere in the leading segment rather than at position 0.
    """
    return INFRA_ACCOUNT_BLOCK_MARKER in note[:160]


def _escalation_marker_path(settings: Settings) -> Any:
    return settings.data_dir / "infra_account_block_escalation.json"


def _load_escalation_marker(settings: Settings) -> dict[str, Any]:
    try:
        data = json.loads(_escalation_marker_path(settings).read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _should_escalate(settings: Settings, now: datetime) -> bool:
    """True when no escalation is on record within the re-escalate window."""
    interval = int(getattr(settings, "infra_account_block_reescalate_seconds", 86400))
    if interval <= 0:
        return True
    last = _load_escalation_marker(settings).get("last_escalated")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(str(last))
    except Exception:
        return True
    return (now - last_dt).total_seconds() >= interval


def _record_escalation(settings: Settings, now: datetime, repos: list[str]) -> None:
    path = _escalation_marker_path(settings)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"last_escalated": now.isoformat(), "repos": repos}))
    except Exception:
        log.exception("infra_account_block: could not persist escalation marker")


def _default_escalate(repos: list[str], first_observed: str | None) -> None:
    """Default operator escalation: a single consolidated WARNING.

    The 2026-09-04 escalation design routes such alerts to a chat
    ``user_chat`` subsession, but no such primitive exists in this repo
    yet — this WARNING is the operator-facing signal, and callers can pass
    ``escalate=`` to route it to a real channel once one exists.
    """
    log.warning(
        "OPERATOR ESCALATION — GitHub hosted CI is blocked (account / "
        "billing / spending-limit) for repo(s): %s. First observed: %s. "
        "Hosted jobs are refused before they start; self-hosted jobs are "
        "unaffected. Clear the block in GitHub 'Billing & plans'; parked "
        "tickets auto-resume once a hosted run on the repo succeeds again.",
        ", ".join(repos) or "unknown",
        first_observed or "unknown",
    )


def _collect_parked_by_board(
    settings: Settings,
) -> tuple[dict[str, tuple[TicketService, list[Ticket]]], list[str], datetime | None]:
    """Gather account-block-parked tickets across every board.

    Returns ``(parked_by_board, sorted_repos, first_observed)`` where
    ``first_observed`` is the earliest block timestamp seen (or ``None``).
    """
    parked_by_board: dict[str, tuple[TicketService, list[Ticket]]] = {}
    repos: set[str] = set()
    first_observed: datetime | None = None

    for board_id in _boards_to_scan(settings):
        service = TicketService(settings, board_id=board_id)
        try:
            blocked = service.list(state=State.BLOCKED)
        except Exception:
            log.exception("infra_account_block: board=%r list failed", board_id)
            continue

        parked: list[Ticket] = []
        for ticket in blocked:
            ev = _last_blocked_event(service, ticket.id)
            if ev is None or not _is_account_block_parked(ev.note or ""):
                continue
            parked.append(ticket)
            if ev.at is not None:
                first_observed = (
                    ev.at if first_observed is None else min(first_observed, ev.at)
                )
        if parked:
            parked_by_board[board_id] = (service, parked)
            rc = _repo_for_board(settings, board_id)
            repos.add(rc.repo_id if rc is not None else board_id)

    return parked_by_board, sorted(repos), first_observed


def _resume_board_if_green(
    settings: Settings,
    board_id: str,
    service: TicketService,
    parked: list[Ticket],
    result: dict[str, Any],
) -> None:
    """Resume *board_id*'s parked tickets once its hosted CI is green again."""
    from ...config import target_branch_for
    from ...forge import get_forge

    rc = _repo_for_board(settings, board_id)
    if rc is None:
        result["skipped"] += len(parked)
        return
    target = target_branch_for(settings, rc)
    try:
        forge = get_forge(settings, repo_config=rc)
        green, sha = _target_is_green(forge, target, parked[0].id)
    except Exception:
        log.exception(
            "infra_account_block: board=%r target %r status lookup failed",
            board_id,
            target,
        )
        result["skipped"] += len(parked)
        return
    if not green:
        result["still_parked"] += len(parked)
        return
    for ticket in parked:
        refresh = _refresh_pr_branch(forge, ticket)
        note = (
            f"hosted CI on `{target}` succeeded again ({sha[:8]}) — "
            f"auto-resumed by infra_account_block recovery; {refresh}"
        )
        try:
            service.resume_blocked(ticket.id, note=note)
            result["resumed"] += 1
            log.info("infra_account_block: %s resumed — %s", ticket.id, note)
        except Exception:
            log.exception("infra_account_block: %s resume failed", ticket.id)
            result["skipped"] += 1


def run_infra_account_block_recovery(
    settings: Settings,
    *,
    escalate: EscalateFn | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One account-block escalation + recovery pass across every board.

    Returns ``{"escalated": 0|1, "resumed": n, "still_parked": n,
    "skipped": n}``.  ``escalated`` is 1 only when a *single* fleet-wide
    escalation was raised this pass (deduped by the re-escalate window).
    """
    escalate = escalate or _default_escalate
    now = now or datetime.now(UTC)

    parked_by_board, sorted_repos, first_observed = _collect_parked_by_board(settings)

    result: dict[str, Any] = {
        "escalated": 0,
        "resumed": 0,
        "still_parked": 0,
        "skipped": 0,
    }
    if not parked_by_board:
        return result

    # Raise ONE fleet-wide escalation (deduped by re-escalate window).
    if _should_escalate(settings, now):
        try:
            escalate(
                sorted_repos,
                first_observed.isoformat() if first_observed else None,
            )
            result["escalated"] = 1
            _record_escalation(settings, now, sorted_repos)
        except Exception:
            log.exception("infra_account_block: escalation sink failed")

    # Auto-resume each board's tickets once its hosted CI is green again.
    for board_id, (service, parked) in parked_by_board.items():
        _resume_board_if_green(settings, board_id, service, parked, result)

    log.info(
        "infra_account_block: pass complete — escalated=%d resumed=%d "
        "still_parked=%d skipped=%d",
        result["escalated"],
        result["resumed"],
        result["still_parked"],
        result["skipped"],
    )
    return result
