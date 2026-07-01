"""Background agent pass routes."""

from __future__ import annotations

import importlib
import logging
import threading
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request

from ..deps import get_run_registry

log = logging.getLogger(__name__)

router = APIRouter(tags=["Passes"])


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


# ---------------------------------------------------------------------------
# Shared helpers for the factory
# ---------------------------------------------------------------------------


def _default_summary(result: Any) -> str:
    """Standard drafts-list summary: ``"Created N drafts: id1, id2, …"``."""
    draft_ids = [d["id"] for d in result.drafts_created[:5]]
    return (
        f"Created {len(result.drafts_created)} drafts: "
        f"{', '.join(draft_ids)}"
        f"{'…' if len(result.drafts_created) > 5 else ''}"
    )


def _make_background_pass(
    *,
    kind: str,
    runner_module: str,
    runner_func: str,
    docstring: str,
    uses_tracing: bool = True,
    summary_builder: Callable[[Any], str] | None = None,
    extra_runner_kwargs: Callable[[Request], dict] | None = None,
) -> Callable[..., dict]:
    """Factory for background-pass POST handlers.

    Returns a callable with signature ``(repo_id, request, registry) -> dict``
    suitable for ``@router.post(...)`` decoration.  The callable launches the
    real pass in a daemon thread and returns ``{"status": "started"}`` at once.

    Parameters
    ----------
    kind:
        Pass kind string used for ``registry.start()``, the thread name,
        and (when tracing is enabled) the Langfuse session / span stage.
    runner_module:
        Absolute dotted module path of the runner (e.g.
        ``"robotsix_mill.runners.periodic_runner"``).  Imported lazily inside the
        background thread so that ``monkeypatch.setattr`` in tests can
        intercept it.
    runner_func:
        Name of the callable inside *runner_module*.
    docstring:
        Set as ``__doc__`` on the returned handler.
    uses_tracing:
        When ``False`` the handler skips the ``make_session_id`` /
        ``start_ticket_root_span`` imports and calls, and does **not** pass
        ``session_id`` to the runner.
    summary_builder:
        Called with the runner's return value to produce the string stored
        via ``registry.finish_ok()``.  When ``None`` (default),
        :func:`_default_summary` is used.
    extra_runner_kwargs:
        Optional callable ``(request) -> dict`` whose return value is
        forwarded as extra keyword arguments to the runner function.
    """

    def handler(
        repo_id: str | None = None,
        request: Request = None,
        registry=Depends(get_run_registry),
    ) -> dict:
        def _run() -> None:
            # Lazy-import the runner so that monkeypatch.setattr in tests
            # can intercept it before the background thread starts.
            mod = importlib.import_module(runner_module)
            fn = getattr(mod, runner_func)

            if uses_tracing:
                from ..tracing import make_session_id, start_ticket_root_span

            repo_configs = _resolve_agent_run_repos(repo_id, request)

            extra: dict = extra_runner_kwargs(request) if extra_runner_kwargs else {}

            for rc in repo_configs:
                run_id = None
                try:
                    repo_key = rc.repo_id if rc else ""
                    run_id = registry.start(kind, repo_id=repo_key)

                    if uses_tracing:
                        session_id = make_session_id(kind)
                        with start_ticket_root_span(session_id, kind, repo_config=rc):
                            r = fn(
                                session_id=session_id,
                                repo_config=rc,
                                **extra,
                            )
                    else:
                        r = fn(repo_config=rc, **extra)

                    summary = (
                        summary_builder(r)
                        if summary_builder is not None
                        else _default_summary(r)
                    )
                    registry.finish_ok(run_id, summary)
                    log.info("%s pass done", kind)
                except Exception as e:  # noqa: BLE001 — background; just log
                    log.exception("%s pass failed", kind)
                    if run_id:
                        registry.finish_error(run_id, str(e))

        threading.Thread(target=_run, name=f"{kind}-pass", daemon=True).start()
        return {"status": "started"}

    handler.__doc__ = docstring
    handler.__name__ = f"{kind.replace('-', '_')}_pass"
    return handler


# ===========================================================================
# Converted handlers — each is a single _make_background_pass(…) call
# ===========================================================================

audit_pass = _make_background_pass(
    kind="audit",
    runner_module="robotsix_mill.runners.periodic_runner",
    runner_func="run_audit_pass",
    docstring="""Kick off an audit pass in the BACKGROUND and return at once.

    The audit runs the LLM agent for minutes — blocking the HTTP
    response made the browser fetch drop ("NetworkError"). New draft
    tickets appear on the board when it finishes.""",
)
router.post("/audit", status_code=202)(audit_pass)


bc_check_pass = _make_background_pass(
    kind="bc-check",
    runner_module="robotsix_mill.runners.periodic_runner",
    runner_func="run_bc_check_pass",
    docstring="""Kick off a bc-check pass in the BACKGROUND and return at once.

    The bc-check agent inspects the codebase for backward-compat shims
    and dead-code branches that are ripe for removal, drafting tickets
    when it finds candidates. New drafts appear on the board when it
    finishes.""",
)
router.post("/bc-check", status_code=202)(bc_check_pass)


completeness_check_pass = _make_background_pass(
    kind="completeness-check",
    runner_module="robotsix_mill.runners.periodic_runner",
    runner_func="run_completeness_check_pass",
    docstring="""Kick off a completeness-check pass in the BACKGROUND and return at once.""",
)
router.post("/completeness-check", status_code=202)(completeness_check_pass)


agent_check_pass = _make_background_pass(
    kind="agent_check",
    runner_module="robotsix_mill.runners.periodic_runner",
    runner_func="run_agent_check_pass",
    docstring="""Kick off an agent-check pass in the BACKGROUND and return at
    once. The agent inspects every agent's prompt, tools, and
    structured output, looking for coherence gaps (e.g. an agent
    promising behaviour its tools can't deliver). New draft tickets
    appear on the board when it finishes.""",
)
router.post("/agent-check", status_code=202)(agent_check_pass)


health_check_pass = _make_background_pass(
    kind="health",
    runner_module="robotsix_mill.runners.periodic_runner",
    runner_func="run_health_pass",
    docstring="""Kick off a codebase-health pass in the BACKGROUND and return at
    once.

    The health pass runs the LLM agent for minutes — blocking the HTTP
    response made the browser fetch drop ("NetworkError"). New draft
    tickets appear on the board when it finishes.

    Mirrors the audit/trace-health pattern: registers the run on
    start so the /runs panel shows it in-flight, and on finish so it
    flips to ok/error with a summary. Without this the run is silently
    happening behind the scenes — the Langfuse trace exists but the
    board reports nothing.""",
)
router.post("/health-check", status_code=202)(health_check_pass)


test_gap_pass = _make_background_pass(
    kind="test-gap",
    runner_module="robotsix_mill.runners.periodic_runner",
    runner_func="run_test_gap_pass",
    docstring="""Kick off a test-gap inspection pass in the BACKGROUND.""",
)
router.post("/test-gap", status_code=202)(test_gap_pass)


survey_pass = _make_background_pass(
    kind="survey",
    runner_module="robotsix_mill.runners.periodic_runner",
    runner_func="run_survey_pass",
    docstring="""Kick off a survey pass in the BACKGROUND and return at once.

    The survey agent discovers similar open-source projects, studies
    their approaches, and proposes concrete improvements as draft
    tickets. New drafts appear on the board when it finishes.""",
)
router.post("/survey", status_code=202)(survey_pass)


copy_paste_pass = _make_background_pass(
    kind="copy-paste",
    runner_module="robotsix_mill.runners.periodic_runner",
    runner_func="run_copy_paste_pass",
    docstring="""Kick off a copy-paste pass in the BACKGROUND and return at once.

    The copy-paste agent detects clone/duplication clusters across the
    codebase, triages the worst offenders, and proposes consolidation
    as draft tickets. New drafts appear on the board when it finishes.""",
)
router.post("/copy-paste", status_code=202)(copy_paste_pass)


module_curator_pass = _make_background_pass(
    kind="module_curator",
    runner_module="robotsix_mill.runners.periodic_runner",
    runner_func="run_module_curator_pass",
    docstring="""Kick off a module-curator pass in the BACKGROUND and return at once.

    The module-curator agent compares the live directory tree against
    ``docs/modules.yaml`` and files draft tickets for unclassified files,
    stale paths, and new module proposals. New drafts appear on the board
    when it finishes.""",
)
router.post("/module-curator", status_code=202)(module_curator_pass)


forge_parity_pass = _make_background_pass(
    kind="forge-parity",
    runner_module="robotsix_mill.runners.periodic_runner",
    runner_func="run_forge_parity_pass",
    docstring="""Kick off a forge-parity pass in the BACKGROUND and return at once.

    The forge-parity agent compares forge adapter implementations (GitHub vs GitLab)
    against the Forge ABC, flags drift (single-adapter overrides, divergent
    implementations, extra methods), and files at most 3 draft tickets per pass.
    New drafts appear on the board when it finishes.""",
)
router.post("/forge-parity", status_code=202)(forge_parity_pass)


config_sync_pass = _make_background_pass(
    kind="config-sync",
    runner_module="robotsix_mill.runners.periodic_runner",
    runner_func="run_config_sync_pass",
    docstring="""Kick off a config-sync drift detection pass in the BACKGROUND.""",
)
router.post("/config-sync", status_code=202)(config_sync_pass)


member_sync_pass = _make_background_pass(
    kind="member-sync",
    runner_module="robotsix_mill.runners.member_sync_runner",
    runner_func="run_member_sync_pass",
    docstring="""Kick off a workspace member-sync pass in the BACKGROUND.

    The deterministic member-sync pass clones the managed repo, detects
    its vcs2l workspace members from ``repos.yaml``, and upserts them into
    ``config/repos.yaml`` (registering new members, refreshing existing
    ones, flagging vanished ones for removal).""",
    summary_builder=lambda r: (
        f"+{len(r.added)} added, {len(r.updated)} updated, "
        f"{len(r.flagged_for_removal)} flagged"
    ),
)
router.post("/member-sync", status_code=202)(member_sync_pass)


state_sync_pass = _make_background_pass(
    kind="state-sync",
    runner_module="robotsix_mill.runners.periodic_runner",
    runner_func="run_state_sync_pass",
    docstring="""Kick off a state-sync pass in the BACKGROUND and return at once.

    The state-sync agent inspects the board's state consistency, checking for
    stale state values, typos, and missing transitions. New draft tickets
    appear on the board when it finishes.""",
)
router.post("/state-sync", status_code=202)(state_sync_pass)


env_doc_sync_pass = _make_background_pass(
    kind="env-doc-sync",
    runner_module="robotsix_mill.runners.periodic_runner",
    runner_func="run_env_doc_sync_pass",
    docstring="""Kick off an env-doc-sync pass in the BACKGROUND and return at once.

    The env-doc-sync agent cross-references env-var declarations in the
    Settings mixins against docs/configuration.md. New draft tickets appear
    on the board when it finishes.""",
)
router.post("/env-doc-sync", status_code=202)(env_doc_sync_pass)


frontend_sync_pass = _make_background_pass(
    kind="frontend-sync",
    runner_module="robotsix_mill.runners.periodic_runner",
    runner_func="run_frontend_sync_pass",
    docstring="""Kick off a frontend-sync pass in the BACKGROUND and return at once.

    The frontend-sync agent keeps the front-end codebase aligned with
    backend API definitions — route signatures, type bindings, and
    shared constants. New draft tickets appear on the board when it
    finishes.""",
)
router.post("/frontend-sync", status_code=202)(frontend_sync_pass)


security_posture_pass = _make_background_pass(
    kind="security_posture",
    runner_module="robotsix_mill.runners.periodic_runner",
    runner_func="run_security_posture_pass",
    docstring="""Kick off a security-posture pass in the BACKGROUND and return at once.

    The security-posture agent reviews the codebase for security
    weaknesses, dependency vulnerabilities, and configuration gaps,
    filing draft tickets for each finding. New drafts appear on the
    board when it finishes.""",
)
router.post("/security-posture", status_code=202)(security_posture_pass)


triage_boilerplate_pass = _make_background_pass(
    kind="triage_boilerplate",
    runner_module="robotsix_mill.runners.periodic_runner",
    runner_func="run_triage_boilerplate_pass",
    docstring="""Kick off a triage-boilerplate pass in the BACKGROUND and return at once.

    The triage-boilerplate agent scans recent triage tickets for recurring
    patterns and proposes boilerplate response templates, filing draft
    tickets for each finding. New drafts appear on the board when it finishes.""",
)
router.post("/triage-boilerplate", status_code=202)(triage_boilerplate_pass)


# -- handlers with custom summary builders -------------------------------

trace_review_pass = _make_background_pass(
    kind="trace-review",
    runner_module="robotsix_mill.runners.trace_review_runner",
    runner_func="run_trace_review_pass",
    docstring="""Kick off a trace-review pass in the BACKGROUND.

    Scans every Langfuse trace since the last run, deterministically
    flags outliers (cost, observation count, tool errors, repeated
    pauses, rejected generations, explore storms), runs a cheap
    flash-model inspector over the flagged subset, and files draft
    tickets with proposed solutions.""",
    summary_builder=lambda r: r.summary or f"created {len(r.drafts_created)} drafts",
)
router.post("/trace-review", status_code=202)(trace_review_pass)


roadmap_sync_pass = _make_background_pass(
    kind="roadmap-sync",
    runner_module="robotsix_mill.runners.roadmap_sync_runner",
    runner_func="run_roadmap_sync_pass",
    docstring="""Kick off a roadmap-sync pass in the BACKGROUND.

    Reads ROADMAP.md from the configured repo and reconciles its
    H2 sections against the board's existing epics by an embedded
    ``<!-- epic-id: ... -->`` marker. Creates new epics for unmarked
    sections, updates existing epics whose body/title changed, and
    opens a PR with the marker insertions so the next run is
    idempotent.""",
    summary_builder=lambda r: r.summary or "no changes",
)
router.post("/roadmap-sync", status_code=202)(roadmap_sync_pass)


# ===========================================================================
# Custom handlers (kept explicit — non-standard structure or dependencies)
# ===========================================================================


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
    from ...runners.trace_health_runner import run_trace_health_check

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
                    f"Unsessoned: {r.unsessioned_count}, "
                    f"unnamed: {r.name_missing_count} / "
                    f"{r.total_traces} "
                    f"traces ({r.window_start} → "
                    f"{r.window_end}) — "
                    f"{'draft created' if r.draft_created else 'no alert'}"
                )
                registry.finish_ok(run_id, summary)
                if r.draft_created:
                    log.info(
                        "trace-health check: draft created — "
                        "%d unsessioned, %d unnamed / %d traces",
                        r.unsessioned_count,
                        r.name_missing_count,
                        r.total_traces,
                    )
                else:
                    log.info(
                        "trace-health check: no alert "
                        "(%d unsessioned, %d unnamed / %d traces)",
                        r.unsessioned_count,
                        r.name_missing_count,
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
    from ...runners.langfuse_cleanup_runner import run_langfuse_cleanup_pass

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


@router.post("/run-health", status_code=202)
def run_health_pass(
    request: Request = None,
    registry=Depends(get_run_registry),
) -> dict:
    """Kick off a RUN-HEALTH pass in the BACKGROUND and return at once.

    The run-health agent reads every board's run registry over the window,
    flags failed/degraded runs deterministically, runs one LLM pass to
    separate real failures from legitimate empties, and files
    high-confidence draft tickets to the mill board. Global — it does not
    fan out per-repo.
    """
    from ...runners.run_health_runner import (
        RunHealthPassResult,
        run_run_health_pass,
    )
    from ..tracing import make_session_id

    def _run() -> None:
        run_id = None
        try:
            run_id = registry.start("run_health")
            session_id = make_session_id("run_health")
            result: RunHealthPassResult = run_run_health_pass(session_id=session_id)
            ids = [d["id"] for d in result.drafts_created[:3]]
            summary = (
                f"{len(result.drafts_created)} draft(s): {', '.join(ids)}"
                if ids
                else "No drafts created"
            )
            registry.finish_ok(run_id, summary)
            log.info("run-health pass done: %d draft(s)", len(result.drafts_created))
        except Exception as e:  # noqa: BLE001 — background; just log
            log.exception("run-health pass failed")
            if run_id:
                registry.finish_error(run_id, str(e))

    threading.Thread(target=_run, name="run-health-pass", daemon=True).start()
    return {"status": "started"}
