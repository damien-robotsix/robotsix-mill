"""Background agent pass routes."""

from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, Depends, HTTPException, Request

from ..deps import get_run_registry

log = logging.getLogger(__name__)

router = APIRouter()


def _resolve_agent_run_repos(repo_id: str | None, request: Request) -> list:
    """Resolve *repo_id* to a list of ``RepoConfig`` (or ``None``) for
    agent-run routes.

    Returns a list so the caller can iterate in ``_run()``, one pass
    per repo.  A ``None`` element means single-repo backward compat
    (the runner uses global secrets / memory paths).
    """
    repos = request.app.state.repos
    if repo_id is None:
        if len(repos.repos) <= 1:
            return [None]  # single-repo backward compat
        # Multi-repo, no repo_id → fan out across all repos.
        return list(repos.repos.values())
    if repo_id == "all":
        return list(repos.repos.values())
    if repo_id not in repos.repos:
        sorted_keys = sorted(repos.repos.keys())
        raise HTTPException(
            status_code=400,
            detail=f"Unknown repo: '{repo_id}'. Known repos: {sorted_keys}",
        )
    return [repos.repos[repo_id]]


@router.post("/audit", status_code=202)
def audit_pass(
    repo_id: str | None = None,
    request: Request = None,
    registry=Depends(get_run_registry),
) -> dict:
    """Kick off an audit pass in the BACKGROUND and return at once.

    The audit runs the LLM agent for minutes — blocking the HTTP
    response made the browser fetch drop ("NetworkError"). New draft
    tickets appear on the board when it finishes.
    """
    from ...audit_runner import run_audit_pass
    from ..tracing import make_session_id, start_ticket_root_span

    repo_configs = _resolve_agent_run_repos(repo_id, request)

    def _run() -> None:
        for rc in repo_configs:
            run_id = None
            try:
                run_id = registry.start("audit", repo_id=rc.repo_id if rc else "")
                session_id = make_session_id("audit")
                with start_ticket_root_span(session_id, "audit", repo_config=rc):
                    r = run_audit_pass(session_id=session_id, repo_config=rc)
                draft_ids = [d["id"] for d in r.drafts_created[:5]]
                summary = (
                    f"Created {len(r.drafts_created)} drafts: "
                    f"{', '.join(draft_ids)}"
                    f"{'…' if len(r.drafts_created) > 5 else ''}"
                )
                registry.finish_ok(run_id, summary)
                log.info("audit pass done: %d draft(s)", len(r.drafts_created))
            except Exception as e:  # noqa: BLE001 — background; just log
                log.exception("audit pass failed")
                if run_id:
                    registry.finish_error(run_id, str(e))

    threading.Thread(target=_run, name="audit-pass", daemon=True).start()
    return {"status": "started"}


@router.post("/bc-check", status_code=202)
def bc_check_pass(
    repo_id: str | None = None,
    request: Request = None,
    registry=Depends(get_run_registry),
) -> dict:
    """Kick off a bc-check pass in the BACKGROUND and return at once.

    The bc-check agent inspects the codebase for backward-compat shims
    and dead-code branches that are ripe for removal, drafting tickets
    when it finds candidates. New drafts appear on the board when it
    finishes.
    """
    from ...bc_check_runner import run_bc_check_pass
    from ..tracing import make_session_id, start_ticket_root_span

    repo_configs = _resolve_agent_run_repos(repo_id, request)

    def _run() -> None:
        for rc in repo_configs:
            run_id = None
            try:
                run_id = registry.start("bc-check", repo_id=rc.repo_id if rc else "")
                session_id = make_session_id("bc-check")
                with start_ticket_root_span(session_id, "bc-check", repo_config=rc):
                    r = run_bc_check_pass(session_id=session_id, repo_config=rc)
                draft_ids = [d["id"] for d in r.drafts_created[:5]]
                summary = (
                    f"Created {len(r.drafts_created)} drafts: "
                    f"{', '.join(draft_ids)}"
                    f"{'…' if len(r.drafts_created) > 5 else ''}"
                )
                registry.finish_ok(run_id, summary)
                log.info("bc-check pass done: %d draft(s)", len(r.drafts_created))
            except Exception as e:  # noqa: BLE001 — background; just log
                log.exception("bc-check pass failed")
                if run_id:
                    registry.finish_error(run_id, str(e))

    threading.Thread(target=_run, name="bc-check-pass", daemon=True).start()
    return {"status": "started"}


@router.post("/completeness-check", status_code=202)
def completeness_check_pass(
    repo_id: str | None = None,
    request: Request = None,
    registry=Depends(get_run_registry),
) -> dict:
    from ...completeness_check_runner import run_completeness_check_pass
    from ..tracing import make_session_id, start_ticket_root_span

    repo_configs = _resolve_agent_run_repos(repo_id, request)

    def _run() -> None:
        for rc in repo_configs:
            run_id = None
            try:
                run_id = registry.start(
                    "completeness-check", repo_id=rc.repo_id if rc else ""
                )
                session_id = make_session_id("completeness-check")
                with start_ticket_root_span(
                    session_id, "completeness-check", repo_config=rc
                ):
                    r = run_completeness_check_pass(
                        session_id=session_id, repo_config=rc
                    )
                draft_ids = [d["id"] for d in r.drafts_created[:5]]
                summary = (
                    f"Created {len(r.drafts_created)} drafts: "
                    f"{', '.join(draft_ids)}"
                    f"{'…' if len(r.drafts_created) > 5 else ''}"
                )
                registry.finish_ok(run_id, summary)
                log.info(
                    "completeness-check pass done: %d draft(s)", len(r.drafts_created)
                )
            except Exception as e:
                log.exception("completeness-check pass failed")
                if run_id:
                    registry.finish_error(run_id, str(e))

    threading.Thread(target=_run, name="completeness-check-pass", daemon=True).start()
    return {"status": "started"}


@router.post("/agent-check", status_code=202)
def agent_check_pass(
    repo_id: str | None = None,
    request: Request = None,
    registry=Depends(get_run_registry),
) -> dict:
    """Kick off an agent-check pass in the BACKGROUND and return at
    once. The agent inspects every agent's prompt, tools, and
    structured output, looking for coherence gaps (e.g. an agent
    promising behaviour its tools can't deliver). New draft tickets
    appear on the board when it finishes.
    """
    from ...agent_check_runner import run_agent_check_pass
    from ..tracing import make_session_id, start_ticket_root_span

    repo_configs = _resolve_agent_run_repos(repo_id, request)

    def _run() -> None:
        for rc in repo_configs:
            run_id = None
            try:
                run_id = registry.start("agent_check", repo_id=rc.repo_id if rc else "")
                session_id = make_session_id("agent-check")
                with start_ticket_root_span(session_id, "agent-check", repo_config=rc):
                    r = run_agent_check_pass(session_id=session_id, repo_config=rc)
                draft_ids = [d["id"] for d in r.drafts_created[:5]]
                summary = (
                    f"Created {len(r.drafts_created)} drafts: "
                    f"{', '.join(draft_ids)}"
                    f"{'…' if len(r.drafts_created) > 5 else ''}"
                )
                registry.finish_ok(run_id, summary)
                log.info(
                    "agent-check pass done: %d draft(s)",
                    len(r.drafts_created),
                )
            except Exception as e:  # noqa: BLE001 — background; just log
                log.exception("agent-check pass failed")
                if run_id:
                    registry.finish_error(run_id, str(e))

    threading.Thread(target=_run, name="agent-check-pass", daemon=True).start()
    return {"status": "started"}


@router.post("/trace-health", status_code=202)
def trace_health_check(
    repo_id: str | None = None,
    request: Request = None,
    registry=Depends(get_run_registry),
) -> dict:
    """Kick off a trace-health check in the BACKGROUND and return at
    once.  The check fetches Langfuse traces from the last 24h,
    detects unsessioned traces, and files a draft ticket if needed.
    No LLM — deterministic and fast.
    """
    from ...trace_health_runner import run_trace_health_check

    repo_configs = _resolve_agent_run_repos(repo_id, request)

    def _run() -> None:
        for rc in repo_configs:
            run_id = None
            try:
                run_id = registry.start(
                    "trace-health", repo_id=rc.repo_id if rc else ""
                )
                r = run_trace_health_check(repo_config=rc)
                summary = (
                    f"{r.unsessioned_count}/{r.total_traces} "
                    f"traces unsessioned ({r.window_start} to "
                    f"{r.window_end}) — "
                    f"{'draft created' if r.draft_created else 'no alert'}"
                )
                registry.finish_ok(run_id, summary)
                if r.draft_created:
                    log.info(
                        "trace-health check: draft created — %d/%d traces unsessioned",
                        r.unsessioned_count,
                        r.total_traces,
                    )
                else:
                    log.info(
                        "trace-health check: no alert (%d/%d traces unsessioned)",
                        r.unsessioned_count,
                        r.total_traces,
                    )
            except Exception as e:  # noqa: BLE001 — background; just log
                log.exception("trace-health check failed")
                if run_id:
                    registry.finish_error(run_id, str(e))

    threading.Thread(target=_run, name="trace-health-check", daemon=True).start()
    return {"status": "started"}


@router.post("/langfuse-cleanup", status_code=202)
def langfuse_cleanup_pass(
    repo_id: str | None = None,
    request: Request = None,
    registry=Depends(get_run_registry),
) -> dict:
    """Kick off a Langfuse trace cleanup in the BACKGROUND and return at
    once.  The cleanup deletes the oldest traces until the project is
    at most ``max_traces`` rows.  Pure HTTP, no LLM.
    """
    from ...langfuse_cleanup_runner import run_langfuse_cleanup_pass

    repo_configs = _resolve_agent_run_repos(repo_id, request)
    settings = request.app.state.settings

    def _run() -> None:
        for rc in repo_configs:
            run_id = None
            try:
                run_id = registry.start(
                    "langfuse-cleanup", repo_id=rc.repo_id if rc else ""
                )
                r = run_langfuse_cleanup_pass(
                    settings=settings,
                    repo_config=rc,
                    max_traces=settings.langfuse_cleanup_max_traces,
                )
                summary = (
                    f"Langfuse project {r.project}: "
                    f"{r.traces_before} traces → "
                    f"{r.traces_deleted} deleted"
                )
                registry.finish_ok(run_id, summary)
                log.info(
                    "langfuse-cleanup: %s — %d traces → %d deleted",
                    r.project,
                    r.traces_before,
                    r.traces_deleted,
                )
            except Exception as e:  # noqa: BLE001 — background; just log
                log.exception("langfuse-cleanup failed")
                if run_id:
                    registry.finish_error(run_id, str(e))

    threading.Thread(target=_run, name="langfuse-cleanup", daemon=True).start()
    return {"status": "started"}


@router.post("/health-check", status_code=202)
def health_check_pass(
    repo_id: str | None = None,
    request: Request = None,
    registry=Depends(get_run_registry),
) -> dict:
    """Kick off a codebase-health pass in the BACKGROUND and return at
    once.

    The health pass runs the LLM agent for minutes — blocking the HTTP
    response made the browser fetch drop ("NetworkError"). New draft
    tickets appear on the board when it finishes.

    Mirrors the audit/trace-health pattern: registers the run on
    start so the /runs panel shows it in-flight, and on finish so it
    flips to ok/error with a summary. Without this the run is silently
    happening behind the scenes — the Langfuse trace exists but the
    board reports nothing.
    """
    from ...health_runner import run_health_pass
    from ..tracing import make_session_id, start_ticket_root_span

    repo_configs = _resolve_agent_run_repos(repo_id, request)

    def _run() -> None:
        for rc in repo_configs:
            run_id = None
            try:
                run_id = registry.start("health", repo_id=rc.repo_id if rc else "")
                session_id = make_session_id("health")
                with start_ticket_root_span(session_id, "health", repo_config=rc):
                    r = run_health_pass(session_id=session_id, repo_config=rc)
                draft_ids = [d["id"] for d in r.drafts_created[:5]]
                summary = (
                    f"Created {len(r.drafts_created)} drafts: "
                    f"{', '.join(draft_ids)}"
                    f"{'…' if len(r.drafts_created) > 5 else ''}"
                )
                registry.finish_ok(run_id, summary)
                log.info("health pass done: %d draft(s)", len(r.drafts_created))
            except Exception as e:  # noqa: BLE001 — background; just log
                log.exception("health pass failed")
                if run_id:
                    registry.finish_error(run_id, str(e))

    threading.Thread(target=_run, name="health-pass", daemon=True).start()
    return {"status": "started"}


@router.post("/test-gap", status_code=202)
def test_gap_pass(
    repo_id: str | None = None,
    request: Request = None,
    registry=Depends(get_run_registry),
) -> dict:
    """Kick off a test-gap inspection pass in the BACKGROUND."""
    from ...test_gap_runner import run_test_gap_pass
    from ..tracing import make_session_id, start_ticket_root_span

    repo_configs = _resolve_agent_run_repos(repo_id, request)

    def _run() -> None:
        for rc in repo_configs:
            run_id = None
            try:
                run_id = registry.start("test-gap", repo_id=rc.repo_id if rc else "")
                session_id = make_session_id("test-gap")
                with start_ticket_root_span(session_id, "test-gap", repo_config=rc):
                    r = run_test_gap_pass(session_id=session_id, repo_config=rc)
                draft_ids = [d["id"] for d in r.drafts_created[:5]]
                summary = (
                    f"Created {len(r.drafts_created)} drafts: "
                    f"{', '.join(draft_ids)}"
                    f"{'…' if len(r.drafts_created) > 5 else ''}"
                )
                registry.finish_ok(run_id, summary)
                log.info("test-gap pass done: %d draft(s)", len(r.drafts_created))
            except Exception as e:  # noqa: BLE001 — background; just log
                log.exception("test-gap pass failed")
                if run_id:
                    registry.finish_error(run_id, str(e))

    threading.Thread(target=_run, name="test-gap-pass", daemon=True).start()
    return {"status": "started"}


@router.post("/survey", status_code=202)
def survey_pass(
    repo_id: str | None = None,
    request: Request = None,
    registry=Depends(get_run_registry),
) -> dict:
    """Kick off a survey pass in the BACKGROUND and return at once.

    The survey agent discovers similar open-source projects, studies
    their approaches, and proposes concrete improvements as draft
    tickets. New drafts appear on the board when it finishes.
    """
    from ...survey_runner import run_survey_pass
    from ..tracing import make_session_id, start_ticket_root_span

    repo_configs = _resolve_agent_run_repos(repo_id, request)

    def _run() -> None:
        for rc in repo_configs:
            run_id = None
            try:
                run_id = registry.start("survey", repo_id=rc.repo_id if rc else "")
                session_id = make_session_id("survey")
                with start_ticket_root_span(session_id, "survey", repo_config=rc):
                    r = run_survey_pass(session_id=session_id, repo_config=rc)
                draft_ids = [d["id"] for d in r.drafts_created[:5]]
                summary = (
                    f"Created {len(r.drafts_created)} drafts: "
                    f"{', '.join(draft_ids)}"
                    f"{'…' if len(r.drafts_created) > 5 else ''}"
                )
                registry.finish_ok(run_id, summary)
                log.info("survey pass done: %d draft(s)", len(r.drafts_created))
            except Exception as e:  # noqa: BLE001 — background; just log
                log.exception("survey pass failed")
                if run_id:
                    registry.finish_error(run_id, str(e))

    threading.Thread(target=_run, name="survey-pass", daemon=True).start()
    return {"status": "started"}


@router.post("/copy-paste", status_code=202)
def copy_paste_pass(
    repo_id: str | None = None,
    request: Request = None,
    registry=Depends(get_run_registry),
) -> dict:
    """Kick off a copy-paste pass in the BACKGROUND and return at once.

    The copy-paste agent detects clone/duplication clusters across the
    codebase, triages the worst offenders, and proposes consolidation
    as draft tickets. New drafts appear on the board when it finishes.
    """
    from ...copy_paste_runner import run_copy_paste_pass
    from ..tracing import make_session_id, start_ticket_root_span

    repo_configs = _resolve_agent_run_repos(repo_id, request)

    def _run() -> None:
        for rc in repo_configs:
            run_id = None
            try:
                run_id = registry.start("copy-paste", repo_id=rc.repo_id if rc else "")
                session_id = make_session_id("copy-paste")
                with start_ticket_root_span(session_id, "copy-paste", repo_config=rc):
                    r = run_copy_paste_pass(session_id=session_id, repo_config=rc)
                draft_ids = [d["id"] for d in r.drafts_created[:5]]
                summary = (
                    f"Created {len(r.drafts_created)} drafts: "
                    f"{', '.join(draft_ids)}"
                    f"{'…' if len(r.drafts_created) > 5 else ''}"
                )
                registry.finish_ok(run_id, summary)
                log.info("copy-paste pass done: %d draft(s)", len(r.drafts_created))
            except Exception as e:  # noqa: BLE001 — background; just log
                log.exception("copy-paste pass failed")
                if run_id:
                    registry.finish_error(run_id, str(e))

    threading.Thread(target=_run, name="copy-paste-pass", daemon=True).start()
    return {"status": "started"}


@router.post("/module-curator", status_code=202)
def module_curator_pass(
    repo_id: str | None = None,
    request: Request = None,
    registry=Depends(get_run_registry),
) -> dict:
    """Kick off a module-curator pass in the BACKGROUND and return at once.

    The module-curator agent compares the live directory tree against
    ``docs/modules.yaml`` and files draft tickets for unclassified files,
    stale paths, and new module proposals. New drafts appear on the board
    when it finishes.
    """
    from ...module_curator_runner import run_module_curator_pass
    from ..tracing import make_session_id, start_ticket_root_span

    repo_configs = _resolve_agent_run_repos(repo_id, request)

    def _run() -> None:
        for rc in repo_configs:
            run_id = None
            try:
                run_id = registry.start(
                    "module_curator", repo_id=rc.repo_id if rc else ""
                )
                session_id = make_session_id("module_curator")
                with start_ticket_root_span(
                    session_id, "module_curator", repo_config=rc
                ):
                    r = run_module_curator_pass(session_id=session_id, repo_config=rc)
                draft_ids = [d["id"] for d in r.drafts_created[:5]]
                summary = (
                    f"Created {len(r.drafts_created)} drafts: "
                    f"{', '.join(draft_ids)}"
                    f"{'…' if len(r.drafts_created) > 5 else ''}"
                )
                registry.finish_ok(run_id, summary)
                log.info("module-curator pass done: %d draft(s)", len(r.drafts_created))
            except Exception as e:  # noqa: BLE001 — background; just log
                log.exception("module-curator pass failed")
                if run_id:
                    registry.finish_error(run_id, str(e))

    threading.Thread(target=_run, name="module-curator-pass", daemon=True).start()
    return {"status": "started"}


@router.post("/config-sync", status_code=202)
def config_sync_pass(
    repo_id: str | None = None,
    request: Request = None,
    registry=Depends(get_run_registry),
) -> dict:
    """Kick off a config-sync drift detection pass in the BACKGROUND."""
    from ...config_sync_runner import run_config_sync_pass
    from ..tracing import make_session_id, start_ticket_root_span

    repo_configs = _resolve_agent_run_repos(repo_id, request)

    def _run() -> None:
        for rc in repo_configs:
            run_id = None
            try:
                run_id = registry.start("config-sync", repo_id=rc.repo_id if rc else "")
                session_id = make_session_id("config-sync")
                with start_ticket_root_span(session_id, "config-sync", repo_config=rc):
                    r = run_config_sync_pass(session_id=session_id, repo_config=rc)
                draft_ids = [d["id"] for d in r.drafts_created[:5]]
                summary = (
                    f"Created {len(r.drafts_created)} drafts: "
                    f"{', '.join(draft_ids)}"
                    f"{'…' if len(r.drafts_created) > 5 else ''}"
                )
                registry.finish_ok(run_id, summary)
                log.info("config-sync pass done: %d draft(s)", len(r.drafts_created))
            except Exception as e:
                log.exception("config-sync pass failed")
                if run_id:
                    registry.finish_error(run_id, str(e))

    threading.Thread(target=_run, name="config-sync-pass", daemon=True).start()
    return {"status": "started"}


@router.post("/trace-review", status_code=202)
def trace_review_pass(
    repo_id: str | None = None,
    request: Request = None,
    registry=Depends(get_run_registry),
) -> dict:
    """Kick off a trace-review pass in the BACKGROUND.

    Scans every Langfuse trace since the last run, deterministically
    flags outliers (cost, observation count, tool errors, repeated
    pauses, rejected generations, explore storms), runs a cheap
    flash-model inspector over the flagged subset, and files draft
    tickets with proposed solutions.
    """
    from ...trace_review_runner import run_trace_review_pass
    from ..tracing import make_session_id, start_ticket_root_span

    repo_configs = _resolve_agent_run_repos(repo_id, request)

    def _run() -> None:
        for rc in repo_configs:
            run_id = None
            try:
                run_id = registry.start(
                    "trace-review",
                    repo_id=rc.repo_id if rc else "",
                )
                session_id = make_session_id("trace-review")
                with start_ticket_root_span(session_id, "trace-review", repo_config=rc):
                    r = run_trace_review_pass(
                        session_id=session_id,
                        repo_config=rc,
                    )
                summary = r.summary or f"created {len(r.drafts_created)} drafts"
                registry.finish_ok(run_id, summary)
                log.info("trace-review pass done: %s", summary)
            except Exception as e:  # noqa: BLE001 — background; just log
                log.exception("trace-review pass failed")
                if run_id:
                    registry.finish_error(run_id, str(e))

    threading.Thread(
        target=_run,
        name="trace-review-pass",
        daemon=True,
    ).start()
    return {"status": "started"}


@router.post("/roadmap-sync", status_code=202)
def roadmap_sync_pass(
    repo_id: str | None = None,
    request: Request = None,
    registry=Depends(get_run_registry),
) -> dict:
    """Kick off a roadmap-sync pass in the BACKGROUND.

    Reads ROADMAP.md from the configured repo and reconciles its
    H2 sections against the board's existing epics by an embedded
    ``<!-- epic-id: ... -->`` marker. Creates new epics for unmarked
    sections, updates existing epics whose body/title changed, and
    opens a PR with the marker insertions so the next run is
    idempotent.
    """
    from ...roadmap_sync_runner import run_roadmap_sync_pass
    from ..tracing import make_session_id, start_ticket_root_span

    repo_configs = _resolve_agent_run_repos(repo_id, request)

    def _run() -> None:
        for rc in repo_configs:
            run_id = None
            try:
                run_id = registry.start(
                    "roadmap-sync",
                    repo_id=rc.repo_id if rc else "",
                )
                session_id = make_session_id("roadmap-sync")
                with start_ticket_root_span(session_id, "roadmap-sync", repo_config=rc):
                    r = run_roadmap_sync_pass(
                        session_id=session_id,
                        repo_config=rc,
                    )
                registry.finish_ok(run_id, r.summary or "no changes")
                log.info("roadmap-sync pass done: %s", r.summary)
            except Exception as e:  # noqa: BLE001 — background; just log
                log.exception("roadmap-sync pass failed")
                if run_id:
                    registry.finish_error(run_id, str(e))

    threading.Thread(
        target=_run,
        name="roadmap-sync-pass",
        daemon=True,
    ).start()
    return {"status": "started"}


@router.post("/cost-reconciliation", status_code=202)
def cost_reconciliation_pass(
    repo_id: str | None = None,
    request: Request = None,
    registry=Depends(get_run_registry),
) -> dict:
    """Kick off a cost-reconciliation drift detection pass in the BACKGROUND."""
    from ...cost_reconciliation_runner import run_cost_reconciliation_pass
    from ..tracing import make_session_id, start_ticket_root_span

    repo_configs = _resolve_agent_run_repos(repo_id, request)

    def _run() -> None:
        for rc in repo_configs:
            run_id = None
            try:
                run_id = registry.start(
                    "cost-reconciliation", repo_id=rc.repo_id if rc else ""
                )
                session_id = make_session_id("cost-reconciliation")
                with start_ticket_root_span(
                    session_id, "cost-reconciliation", repo_config=rc
                ):
                    r = run_cost_reconciliation_pass(
                        session_id=session_id, repo_config=rc
                    )
                # Prefer the runner's own summary (delta or "no overrun");
                # fall back to generic drafts-list.
                runner_summary = (getattr(r, "summary", "") or "").strip()
                if runner_summary:
                    summary = runner_summary
                else:
                    draft_ids = [d["id"] for d in r.drafts_created[:5]]
                    summary = (
                        f"Created {len(r.drafts_created)} drafts: "
                        f"{', '.join(draft_ids)}"
                        f"{'…' if len(r.drafts_created) > 5 else ''}"
                    )
                registry.finish_ok(run_id, summary)
                log.info(
                    "cost-reconciliation pass done: %d draft(s)",
                    len(r.drafts_created),
                )
            except Exception as e:
                log.exception("cost-reconciliation pass failed")
                if run_id:
                    registry.finish_error(run_id, str(e))

    threading.Thread(target=_run, name="cost-reconciliation-pass", daemon=True).start()
    return {"status": "started"}


@router.post("/meta", status_code=202)
def meta_pass(
    request: Request = None,
    registry=Depends(get_run_registry),
) -> dict:
    """Kick off a META pass in the BACKGROUND and return at once.

    The meta-agent surveys ALL registered repo clones, identifies
    extraction and alignment opportunities, and files drafts to the
    meta board and per-repo boards respectively.  This is a global
    pass — it does not fan out per-repo.
    """
    from ...meta.runner import MetaPassResult, run_meta_pass
    from ..tracing import make_session_id

    def _run() -> None:
        run_id = None
        try:
            run_id = registry.start("meta")
            session_id = make_session_id("meta")
            result: MetaPassResult = run_meta_pass(session_id=session_id)
            total_drafts = len(result.extraction_drafts_created) + len(
                result.alignment_drafts_created
            )
            extraction_ids = [d["id"] for d in result.extraction_drafts_created[:3]]
            alignment_ids = [d["id"] for d in result.alignment_drafts_created[:3]]
            parts = []
            if extraction_ids:
                parts.append(f"Extraction: {', '.join(extraction_ids)}")
            if alignment_ids:
                parts.append(f"Alignment: {', '.join(alignment_ids)}")
            summary = "; ".join(parts) if parts else "No drafts created"
            if total_drafts > 6:
                summary += " …"
            registry.finish_ok(run_id, summary)
            log.info(
                "meta pass done: %d extraction + %d alignment = %d total draft(s)",
                len(result.extraction_drafts_created),
                len(result.alignment_drafts_created),
                total_drafts,
            )
        except Exception as e:  # noqa: BLE001 — background; just log
            log.exception("meta pass failed")
            if run_id:
                registry.finish_error(run_id, str(e))

    threading.Thread(target=_run, name="meta-pass", daemon=True).start()
    return {"status": "started"}
