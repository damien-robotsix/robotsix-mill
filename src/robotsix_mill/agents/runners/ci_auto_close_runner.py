"""CI-failure auto-close runner — periodic re-check for ``source=ci`` tickets.

The CI monitor files ``source="ci"`` tickets whenever a workflow fails on the
merge target branch (``**Workflow:**`` / ``**Branch:**`` / ``**Commit:**``
markers in the body; title ``CI failure: <workflow> on <branch>``).  A ticket
can outlive the underlying failure: the workflow turns green on main but the
ticket keeps sitting in DRAFT, a human gate, or BLOCKED, waiting for a human
to notice and close it by hand (observed 2026-09-02: 4 ci-source tickets sat
in blocked / human_issue_approval / human_mr_approval for hours after the
workflow was already green on main — closed BY HAND).

This pass re-runs the failure detection against the CURRENT main head: for
every non-terminal ``source=ci`` ticket it parses the failing workflow +
branch from the title/body, then inspects the forge's completed runs of that
workflow on the target branch.  When the workflow has completed green on a
head at-or-after the failing commit, the ticket is transitioned to DONE with
a note naming the green run ids/links.

Green rule (per operator directive):
  * one deterministic green suffices when a fix commit for the same cause is
    identifiable — here taken as a green head *strictly newer* than the
    failing commit (main advanced to a new green head = the failure cause was
    addressed by landing work);
  * otherwise two consecutive green runs at-or-after the failing commit.

Never touched: tickets whose workflow is still red (no green at-or-after the
failing commit), tickets with a branch (an open MR with unmerged work — those
keep their normal flow), and tickets whose failing run/commit can no longer
be anchored to a run on the target branch (unverifiable — skip rather than
risk a false close).

Deterministic: forge reads + ``service.mark_done`` (the escape hatch that
force-closes a ci-source ticket from any non-terminal state).  No LLM, no
memory ledger.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from ...config import get_repos_config
from ...core.models import SourceKind
from ...core.service import TicketService
from ...core.states import DONE_OR_CLOSED
from ...forge import get_forge
from .timeout_escalation_runner import _boards_to_scan

if TYPE_CHECKING:
    from ...config import Settings

log = logging.getLogger("robotsix_mill.ci_auto_close")

# Matches the CI-monitor draft title: "CI failure: <workflow> on <branch>".
# The workflow name may contain spaces, so the branch is anchored to the tail.
_TITLE_RE = re.compile(r"CI failure: (.+?) on (\S+)$", re.IGNORECASE)
# Body markers written by the CI monitor (``poll_loops._poll_one_repo_ci``).
_BODY_WF_RE = re.compile(r"\*\*Workflow:\*\*\s*(.+)")
_BODY_COMMIT_RE = re.compile(r"\*\*Commit:\*\*\s*`([0-9a-fA-F]{6,})`")

_TERMINAL_CONCLUSIONS = frozenset({"success", "failure"})

# Author recorded on force-closed BLOCKED tickets via ``mark_done``.
_AUTHOR = "ci-auto-close"


def _parse_workflow_branch(ticket: Any, body: str) -> tuple[str | None, str | None]:
    """Extract ``(workflow_name, branch)`` from a ci ticket.

    Prefers the ``**Workflow:**`` / ``**Branch:**`` body markers, falling back
    to the ``CI failure: <wf> on <branch>`` title (which survives refinement
    when the body is overwritten).
    """
    body = body or ""
    m = _BODY_WF_RE.search(body)
    wf = m.group(1).strip() if m else None
    branch = None
    m = re.search(r"\*\*Branch:\*\*\s*(\S+)", body)
    if m:
        branch = m.group(1).strip()
    if wf and branch:
        return wf, branch
    # Title fallback (refined tickets keep wf+branch in the title).
    m = _TITLE_RE.match(ticket.title or "")
    if m:
        t_wf = m.group(1).strip()
        t_branch = m.group(2).strip()
        return t_wf or wf, t_branch or branch
    return wf, branch


def _failing_head(body: str) -> str | None:
    """Return the failing commit SHA from the body ``**Commit:**`` marker."""
    if not body:
        return None
    m = _BODY_COMMIT_RE.search(body)
    return m.group(1) if m else None


def _created_at(run: dict[str, Any]) -> str:
    return str(run.get("created_at") or "")


def _close(
    service: TicketService,
    ticket_id: str,
    wf: str,
    target: str,
    green_runs: list[dict[str, Any]],
) -> None:
    """Transition *ticket_id* to DONE with a note naming the green run links.

    Uses ``service.mark_done`` (the can_transition-bypassing escape hatch)
    so a ci ticket is closed from ANY non-terminal state — a human gate,
    DRAFT, BLOCKED, etc. ``mark_done`` treats ci-source tickets as
    force-close (they have no feature branch to merge-verify).
    """
    refs = ", ".join(
        f"[{r.get('id')}]({r.get('html_url')})"
        for r in green_runs
        if r.get("id") is not None
    )
    note = (
        f"CI auto-close: workflow '{wf}' is green on {target} at-or-after the "
        f"failing commit; green run(s): {refs}. Closed by the ci-auto-close "
        "periodic pass — no human/monitor involvement required."
    )
    try:
        service.mark_done(ticket_id, note=note, author=_AUTHOR)
    except Exception:
        log.warning("ci_auto_close: could not close %s", ticket_id, exc_info=True)


def _maybe_close(
    service: TicketService,
    settings: Settings,
    repo_config: Any,
    ticket: Any,
) -> bool:
    """Return True when *ticket*'s workflow is green on main at-or-after the
    failing commit and the ticket was closed to DONE.
    """
    try:
        body = service.workspace(ticket).read_description() or ""
    except Exception:
        body = ""
    wf, target = _parse_workflow_branch(ticket, body)
    if not wf or not target:
        return False
    # A ticket with a branch has an open MR with unmerged work — keep its
    # normal flow (ci-failure monitor tickets have no branch).
    if ticket.branch:
        return False

    f_sha = _failing_head(body)
    if not f_sha:
        return False  # cannot anchor the failure — skip

    try:
        forge = get_forge(settings, repo_config=repo_config)
        runs = forge.list_workflow_runs(branch=target)
    except Exception:
        log.warning(
            "ci_auto_close: list_workflow_runs failed for repo %s, ticket %s",
            getattr(repo_config, "repo_id", "?"),
            ticket.id,
            exc_info=True,
        )
        return False

    green_runs = _green_since_failure(runs, wf, f_sha)
    if not green_runs:
        return False  # still red / not enough greens / cannot anchor

    _close(service, ticket.id, wf, target, green_runs)
    return True


def _green_since_failure(
    runs: list[dict[str, Any]], wf: str, f_sha: str
) -> list[dict[str, Any]] | None:
    """Return the green runs at-or-after *f_sha* when the workflow *wf* is
    fixed, else ``None``.

    Two consecutive greens at-or-after the failing commit, OR a single green
    on a head strictly newer than the failing commit (an identifiable fix
    commit: main advanced to a new green head) both count as fixed. A still-red
    workflow, a lone green on the same commit, or a failing commit that is no
    longer in the recent run window all return ``None``.
    """
    completed = [
        r
        for r in runs
        if (r.get("name") or "") == wf
        and (r.get("conclusion") or "") in _TERMINAL_CONCLUSIONS
    ]
    completed.sort(key=_created_at, reverse=True)
    if not completed:
        return None
    idx = next(
        (i for i, r in enumerate(completed) if (r.get("head_sha") or "") == f_sha),
        None,
    )
    if idx is None:
        return None
    since = completed[: idx + 1]
    streak = 0
    for r in since:
        if r.get("conclusion") == "success":
            streak += 1
        else:
            break
    if streak == 0:
        return None
    green_runs = [r for r in since if r.get("conclusion") == "success"]
    fix_identifiable = any((r.get("head_sha") or "") != f_sha for r in green_runs)
    if streak < 2 and not (streak >= 1 and fix_identifiable):
        return None
    return green_runs


def _process_board(
    settings: Settings, repo_config: Any, counts: dict[str, Any]
) -> None:
    """Close every non-terminal source=ci ticket on *repo_config*'s board
    whose workflow is green on main; mutates *counts*.
    """
    board_id = repo_config.board_id
    try:
        service = TicketService(settings, board_id=board_id)
        tickets = service.list(exclude_states=DONE_OR_CLOSED)
    except Exception:
        log.exception("ci_auto_close: board=%r list failed", board_id)
        return
    for t in tickets:
        if t.source != SourceKind.CI:
            continue
        try:
            closed = _maybe_close(service, settings, repo_config, t)
        except Exception:
            log.exception("ci_auto_close: %s failed", t.id)
            counts["skipped"] += 1
            continue
        counts["checked"] += 1
        if closed:
            counts["closed"] += 1
        else:
            counts["skipped"] += 1


def run_ci_auto_close(settings: Settings) -> dict[str, Any]:
    """One ci auto-close pass over every board's non-terminal ci tickets.

    Returns ``{"closed": N, "checked": N, "skipped": N}``.
    """
    # Map board_id -> repo_config so each ticket's forge has the right identity.
    repo_by_board: dict[str, Any] = {}
    try:
        for rc in get_repos_config().repos.values():
            if rc.board_id and rc.board_id not in repo_by_board:
                repo_by_board[rc.board_id] = rc
    except Exception:
        log.exception("ci_auto_close: could not load repos config")

    counts = {"closed": 0, "checked": 0, "skipped": 0}
    for board_id in _boards_to_scan(settings):
        rc = repo_by_board.get(board_id)
        if rc is None:
            # No forge identity for this board — cannot verify a workflow.
            continue
        _process_board(settings, rc, counts)
    return counts
