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
failing commit), tickets whose branch has an OPEN MR (unmerged work — those
keep their normal flow; a branch alone is NOT such evidence, see
``_has_open_mr``), and tickets whose failing run/commit can no longer be
anchored to a run on the target branch (unverifiable — skip rather than
risk a false close).

Deterministic: forge reads + ``service.mark_done`` (the escape hatch that
force-closes a ci-source ticket from any non-terminal state).  No LLM, no
memory ledger.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from typing import TYPE_CHECKING, Any

from ...config import get_repos_config, target_branch_for
from ...core.models import SourceKind, TicketEvent
from ...core.service import TicketService
from ...core.states import DONE_OR_CLOSED
from ...scm.forge import get_forge
from .timeout_escalation_runner import _boards_to_scan

if TYPE_CHECKING:
    from ...config import Settings

log = logging.getLogger("robotsix_mill.ci_auto_close")

# Matches the CI-monitor draft title: "CI failure: <workflow> on <branch>".
# The workflow name may contain spaces, so the branch is anchored to the tail.
_TITLE_RE = re.compile(r"CI failure: (.+?) on (\S+)$", re.IGNORECASE)
# Body markers written by the CI monitor (``poll_loops_ci._poll_one_repo_ci``).
_BODY_WF_RE = re.compile(r"\*\*Workflow:\*\*\s*(.+)")
_BODY_COMMIT_RE = re.compile(r"\*\*Commit:\*\*\s*`([0-9a-fA-F]{6,})`")
_BODY_CREATED_RE = re.compile(r"\*\*Created:\*\*\s*(\d{4}-\d{2}-\d{2}T[0-9:.]+Z?)")
# First GitHub Actions run URL in the body (``.../actions/runs/<id>``). The
# ``**Run:**`` link survives refinement even when markers and title do not.
_RUN_URL_RE = re.compile(r"actions/runs/(\d+)")

_TERMINAL_CONCLUSIONS = frozenset({"success", "failure"})

# Author recorded on force-closed BLOCKED tickets via ``mark_done``.
_AUTHOR = "ci-auto-close"

# Prefix written to ticket history when a cron-only workflow is manually
# dispatched after its fix landed. Doubles as the dedupe key: its presence in
# the ticket's history means "already dispatched — never dispatch twice".
_DISPATCH_NOTE = "ci_auto_close: dispatched workflow"


def _parse_workflow_branch(
    ticket: Any, body: str
) -> tuple[str | None, str | None, str | None]:
    """Extract ``(workflow_name, branch, source)`` from a ci ticket.

    Prefers the ``**Workflow:**`` / ``**Branch:**`` body markers (``source``
    ``"markers"``), falling back to the ``CI failure: <wf> on <branch>`` title
    (``source`` ``"title"``).  ``source`` is ``None`` when neither path yields
    both a workflow and a branch — the caller then tries the run-URL anchor.
    """
    body = body or ""
    m = _BODY_WF_RE.search(body)
    wf = m.group(1).strip() if m else None
    branch = None
    m = re.search(r"\*\*Branch:\*\*\s*(\S+)", body)
    if m:
        branch = m.group(1).strip()
    if wf and branch:
        return wf, branch, "markers"
    # Title fallback (unrefined tickets keep wf+branch in the title).
    m = _TITLE_RE.match(ticket.title or "")
    if m:
        t_wf = m.group(1).strip()
        t_branch = m.group(2).strip()
        return t_wf or wf, t_branch or branch, "title"
    return wf, branch, None


def _run_url_id(body: str) -> int | None:
    """Return the first ``actions/runs/<id>`` run id in *body*, or ``None``.

    Refine rewrites both the title and the body markers, but it keeps the
    ``**Run:**`` link (a ``.../actions/runs/<id>`` URL) intact, so this is the
    last anchor that survives refinement.
    """
    if not body:
        return None
    m = _RUN_URL_RE.search(body)
    return int(m.group(1)) if m else None


def _resolve_anchors(
    forge: Any, ticket: Any, body: str
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    """Resolve ``(wf, branch, failing_head, failing_created_at, source)``.

    Starts from the body markers / title (:func:`_parse_workflow_branch`) plus
    the ``**Commit:**`` / ``**Created:**`` markers.  When markers *and* title
    both fail to yield a workflow + branch, falls back to resolving the first
    ``actions/runs/<id>`` URL in the body via ``forge.get_workflow_run`` and
    uses the run's ``name``/``path``, ``head_branch``, ``head_sha`` and
    ``created_at`` as anchors (``source`` ``"run-url"``).
    """
    wf, branch, source = _parse_workflow_branch(ticket, body)
    f_sha = _failing_head(body)
    created = _failing_created_at(body)
    if wf and branch:
        return wf, branch, f_sha, created, source

    run_id = _run_url_id(body)
    if run_id is None:
        return wf, branch, f_sha, created, None
    try:
        run = forge.get_workflow_run(run_id=run_id)
    except Exception:
        run = None
    if not run:
        return wf, branch, f_sha, created, None
    r_wf = (run.get("name") or run.get("path") or "").strip() or wf
    r_branch = (run.get("head_branch") or "").strip() or branch
    r_sha = (run.get("head_sha") or "").strip() or f_sha
    r_created = (run.get("created_at") or "").strip() or created
    return r_wf, r_branch, r_sha, r_created, "run-url"


def _failing_head(body: str) -> str | None:
    """Return the failing commit SHA from the body ``**Commit:**`` marker."""
    if not body:
        return None
    m = _BODY_COMMIT_RE.search(body)
    return m.group(1) if m else None


def _failing_created_at(body: str) -> str | None:
    """Return the failing run's ISO timestamp from the body ``**Created:**``
    marker (written by the CI monitor next to ``**Run:**``), or ``None``.
    """
    if not body:
        return None
    m = _BODY_CREATED_RE.search(body)
    return m.group(1) if m else None


def _created_at(run: dict[str, Any]) -> str:
    return str(run.get("created_at") or "")


def _attempt(run: dict[str, Any]) -> int:
    """Return the 1-based ``run_attempt`` for *run* (missing/None → 1).

    A value ``> 1`` means the run is a re-run of the SAME commit — the most
    common way a transient CI failure gets fixed without a new head.
    """
    try:
        return int(run.get("run_attempt") or 1)
    except TypeError, ValueError:
        return 1


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

    def _label(r: dict[str, Any]) -> str:
        attempt = _attempt(r)
        suffix = f" (re-run attempt {attempt})" if attempt > 1 else ""
        return f"[{r.get('id')}{suffix}]({r.get('html_url')})"

    refs = ", ".join(_label(r) for r in green_runs if r.get("id") is not None)
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

    try:
        forge = get_forge(settings, repo_config=repo_config)
    except Exception:
        log.warning(
            "ci_auto_close: get_forge failed for repo %s, ticket %s",
            getattr(repo_config, "repo_id", "?"),
            ticket.id,
            exc_info=True,
        )
        return False

    wf, target, f_sha, failing_created_at, source = _resolve_anchors(
        forge, ticket, body
    )
    if not wf or not target:
        log.info("ci_auto_close: skip %s — unparseable workflow/branch", ticket.id)
        return False

    if not f_sha:
        log.info(
            "ci_auto_close: skip %s — cannot anchor (source=%s)", ticket.id, source
        )
        return False  # cannot anchor the failure — skip

    # Keep the ticket's normal flow ONLY when its branch has an open MR
    # with unmerged work.  A branch by itself proves nothing: implement
    # sets ``ticket.branch`` the moment it starts, long before any MR
    # exists.  Incident 2026-09-07 (mill ticket
    # 20260907T144701Z-ci-failure-ci-on-main-ef8e): implement created the
    # branch, misdiagnosed the red suite and parked the ticket BLOCKED
    # with no PR; main's CI was green again from 14:58Z, yet the old
    # ``if ticket.branch: return False`` skipped it every 15 min.
    try:
        if _has_open_mr(forge, ticket):
            log.info(
                "ci_auto_close: skip %s — open MR with unmerged work (source=%s)",
                ticket.id,
                source,
            )
            return False
    except Exception:
        log.warning(
            "ci_auto_close: pr_status failed for repo %s, ticket %s",
            getattr(repo_config, "repo_id", "?"),
            ticket.id,
            exc_info=True,
        )
        return False  # cannot tell whether an MR exists — skip, never guess

    try:
        runs = forge.list_workflow_runs(branch=target)
    except Exception:
        log.warning(
            "ci_auto_close: list_workflow_runs failed for repo %s, ticket %s",
            getattr(repo_config, "repo_id", "?"),
            ticket.id,
            exc_info=True,
        )
        return False

    tag_scoped = _is_tag_target(settings, repo_config, target)
    runs = _with_tag_runs(forge, settings, ticket, runs) if tag_scoped else runs

    green_runs, skip_reason = _green_since_failure(
        runs,
        wf,
        f_sha,
        failing_created_at=failing_created_at,
        # A green ``workflow_dispatch`` on the failing commit is a
        # deliberate re-run whatever the target: a red main fixed by a repo
        # SETTING (missing secret/variable) never gets a new head, so
        # requiring a second green parked it until an unrelated push
        # (robotsix-linkedin 93c8, 2026-10-29).
        dispatch_is_fix=True,
    )
    if not green_runs:
        try:
            dispatched = not tag_scoped and _maybe_dispatch_cron_workflow(
                service, forge, ticket, wf, target, f_sha, runs
            )
        except Exception:
            log.warning(
                "ci_auto_close: cron-dispatch check failed for %s",
                ticket.id,
                exc_info=True,
            )
            dispatched = False
        log.info(
            "ci_auto_close: skip %s — %s%s (source=%s)",
            ticket.id,
            skip_reason,
            " (dispatched cron-only workflow)" if dispatched else "",
            source,
        )
        return False  # still red / not enough greens / cannot anchor

    log.info("ci_auto_close: close %s — workflow green (source=%s)", ticket.id, source)
    _close(service, ticket.id, wf, target, green_runs)
    return True


def _is_tag_target(settings: Settings, repo_config: Any, target: str) -> bool:
    """True when *target* is a tag the CI monitor's tag scan files for
    (matches ``ci_monitor_tag_glob`` and is not the merge target branch).
    """
    if not settings.ci_monitor_scan_tags:
        return False
    if not fnmatch.fnmatchcase(target, settings.ci_monitor_tag_glob):
        return False
    try:
        return target != target_branch_for(settings, repo_config)
    except Exception:
        return True


def _with_tag_runs(
    forge: Any, settings: Settings, ticket: Any, runs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Add the tag-push runs to a tag ticket's ``branch=<tag>`` runs.

    A tag ticket closes on a green run on that tag (``branch=<tag>`` also
    covers a ``workflow_dispatch`` on the tag) or on the next tag's green
    push run — never on a green main.  Best-effort: a listing failure keeps
    *runs* unchanged.
    """
    try:
        tag_runs = forge.list_tag_workflow_runs(tag_glob=settings.ci_monitor_tag_glob)
    except Exception:
        log.warning(
            "ci_auto_close: list_tag_workflow_runs failed for ticket %s",
            ticket.id,
            exc_info=True,
        )
        return runs
    known = {r.get("id") for r in runs}
    return list(runs) + [r for r in tag_runs if r.get("id") not in known]


def _has_open_mr(forge: Any, ticket: Any) -> bool:
    """Return True when *ticket*'s branch has an OPEN (unmerged) MR.

    This is the same forge question the API's read-model ``pr_url`` field
    answers (``runtime.deps._pr_url`` → ``forge.pr_status``); the row
    itself stores no PR url, only ``branch``.  Every merge-family state
    (IMPLEMENT_COMPLETE, HUMAN_MR_APPROVAL, WAITING_AUTO_MERGE, REBASING,
    FIXING_CI, ADDRESSING_REVIEW) means "PR open", so asking the forge
    covers them all — including a ticket BLOCKED out of one of them —
    without a state table that could drift.  A closed-unmerged MR is not
    unmerged work in flight (the merge stage blocks such tickets), so it
    does not keep the ticket alive either.  Raises on forge failure so
    the caller can skip rather than guess.
    """
    if not ticket.branch:
        return False
    pr = forge.pr_status(source_branch=ticket.branch)
    if not pr:
        return False
    return not pr.get("merged") and (pr.get("state") or "open") == "open"


def _green_since_failure(
    runs: list[dict[str, Any]],
    wf: str,
    f_sha: str,
    *,
    failing_created_at: str | None = None,
    dispatch_is_fix: bool = False,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Return ``(green_runs, None)`` when the workflow *wf* is fixed at-or-after
    *f_sha*, else ``(None, reason)`` naming why the ticket is skipped.

    *failing_created_at* (the ticket's ``**Created:**`` marker, ISO-8601 Z)
    anchors the failure when its run has already aged out of the forge's
    run window: the listing is a fixed-size page across EVERY workflow of the
    repo, so on a busy repo the failing run drops off within hours (mill
    2026-09-09: a 03:27Z Security Audit failure was gone from the page by
    the 03:53Z pass, and the ticket was skipped every 15 min as ``failing
    head outside run window`` while main had been green on that workflow
    since 04:18Z).  When every completed run of *wf* in the window post-dates
    the failure, the whole window IS "at-or-after the failing commit" and the
    normal streak rule applies to it.

    A fix is identifiable — one green suffices — when a green run is either on
    a head strictly newer than the failing commit (main advanced to a new
    green head) OR a *re-run* of the failing commit itself (``run_attempt >
    1``: a transient failure fixed by re-running the same run, which never
    produces a new head). Otherwise two consecutive greens at-or-after the
    failing commit are required.  With *dispatch_is_fix* (passed for every
    target: a tag never gets a new head, and neither does a main fixed by a
    repo setting) a green ``workflow_dispatch`` run on the failing commit is
    a deliberate re-run and also suffices.

    ``reason`` is one of ``cannot anchor`` (no completed runs for the
    workflow), ``failing head outside run window`` (the failing commit is no
    longer among the recent runs), ``still red`` (the newest run at-or-after
    the failing commit is a failure), or ``lone green on failing head, attempt
    1`` (a single first-attempt green on the same commit — need a second).
    """
    completed = [
        r
        for r in runs
        if (r.get("name") or "") == wf
        and (r.get("conclusion") or "") in _TERMINAL_CONCLUSIONS
    ]
    completed.sort(key=_created_at, reverse=True)
    if not completed:
        return None, "cannot anchor"
    idx = next(
        (i for i, r in enumerate(completed) if (r.get("head_sha") or "") == f_sha),
        None,
    )
    if idx is None:
        if failing_created_at and all(
            _created_at(r) > failing_created_at for r in completed
        ):
            # The failing run aged out of the page; everything we can see
            # ran after it, so the window is the "since" set.
            since = completed
        else:
            return None, "failing head outside run window"
    else:
        since = completed[: idx + 1]
    streak = 0
    for r in since:
        if r.get("conclusion") == "success":
            streak += 1
        else:
            break
    if streak == 0:
        return None, "still red"
    green_runs = [r for r in since if r.get("conclusion") == "success"]
    fix_identifiable = any(
        (r.get("head_sha") or "") != f_sha
        or _attempt(r) > 1
        or (dispatch_is_fix and r.get("event") == "workflow_dispatch")
        for r in green_runs
    )
    if streak < 2 and not fix_identifiable:
        return None, "lone green on failing head, attempt 1"
    return green_runs, None


def _is_cron_only(triggers: list[str] | None) -> bool:
    """True when *triggers* describe a cron-only workflow.

    A cron-only workflow runs on ``schedule`` and can be hand-triggered via
    ``workflow_dispatch`` but has no ``push`` / ``pull_request`` trigger, so
    landing a fix on the merge target never re-runs it — the last (red) run
    lingers until the next scheduled tick (up to a week). Conservative: any
    ``push`` / ``pull_request`` trigger (even branch-scoped) disqualifies it,
    so we only auto-dispatch workflows that genuinely never run on a merge.
    """
    if not triggers:
        return False
    events = set(triggers)
    return (
        "schedule" in events
        and "workflow_dispatch" in events
        and "push" not in events
        and "pull_request" not in events
    )


def _already_dispatched(service: TicketService, ticket_id: str) -> bool:
    """True when a dispatch was already recorded for *ticket_id*.

    Enforces "dispatch at most once per ticket". On any history-read failure
    returns True (fail safe: never risk a second dispatch).
    """
    try:
        events: list[TicketEvent] = service.history(ticket_id)
    except Exception:
        log.warning(
            "ci_auto_close: history read failed for %s", ticket_id, exc_info=True
        )
        return True
    return any(_DISPATCH_NOTE in (e.note or "") for e in events)


def _maybe_dispatch_cron_workflow(
    service: TicketService,
    forge: Any,
    ticket: Any,
    wf: str,
    target: str,
    f_sha: str,
    runs: list[dict[str, Any]],
) -> bool:
    """Dispatch a cron-only workflow once after its fix landed; return True iff
    a dispatch was issued this pass.

    Called when the workflow is not (yet) green. A workflow whose only
    triggers are ``schedule`` + ``workflow_dispatch`` never re-runs when a fix
    merges to *target*, so ``ci_auto_close`` would see the stale red run for up
    to a week. When the latest run of *wf* is still the failing commit (no run
    newer than the fix) and the workflow is cron-only, trigger one
    ``workflow_dispatch`` run on *target* and record it; the next pass closes
    (or re-blocks) on that run's conclusion.

    Guards: dispatch at most once per ticket (:func:`_already_dispatched`), and
    skip when a run newer than the failing commit already exists (a fresh run
    is on its way — ``_green_since_failure`` handles it).
    """
    completed = [r for r in runs if (r.get("name") or "") == wf]
    if not completed:
        return False  # cannot resolve the workflow file
    completed.sort(key=_created_at, reverse=True)
    latest = completed[0]
    # A run newer than the failing commit already exists → nothing to dispatch.
    if (latest.get("head_sha") or "") != f_sha:
        return False
    path = (latest.get("path") or "").strip()
    if not path:
        return False
    if _already_dispatched(service, ticket.id):
        return False
    if not _is_cron_only(forge.get_workflow_triggers(path=path)):
        return False

    workflow_file = path.rsplit("/", 1)[-1]
    result = forge.dispatch_workflow(workflow_file=workflow_file, ref=target)
    if not result.get("dispatched"):
        log.warning(
            "ci_auto_close: dispatch of %s for %s failed: %s",
            workflow_file,
            ticket.id,
            result.get("reason"),
        )
        return False

    service.add_step_event(
        ticket.id,
        f"{_DISPATCH_NOTE} '{wf}' ({workflow_file}) on {target} — cron-only "
        "workflow re-triggered after its fix merged; awaiting the new run's "
        "conclusion.",
    )
    log.info(
        "ci_auto_close: dispatched cron-only workflow %s for %s",
        workflow_file,
        ticket.id,
    )
    return True


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
        for repo_cfg in get_repos_config().repos.values():
            if repo_cfg.board_id and repo_cfg.board_id not in repo_by_board:
                repo_by_board[repo_cfg.board_id] = repo_cfg
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
