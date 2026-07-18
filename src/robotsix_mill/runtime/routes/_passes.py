"""Background agent pass routes."""

from __future__ import annotations

import importlib
import logging
import threading
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request

from ..deps import get_repos_registry, get_run_registry

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
        request: Request = None,  # type: ignore[assignment]
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
# Pass registry — single source of truth for all manually-triggerable
# periodic passes.  Each entry maps pass name → (kind, runner_module,
# runner_func, display_label).
#
# Adding a new periodic pass here makes it automatically triggerable from
# the board with no additional wiring needed.
# ===========================================================================

_PASS_REGISTRY: dict[str, dict[str, Any]] = {
    # -- llm_agent passes (run via periodic_runner) --
    "audit": {
        "kind": "llm_agent",
        "runner_module": "robotsix_mill.runners.periodic_runner",
        "runner_func": "run_audit_pass",
        "label": "Audit",
    },
    "agent_check": {
        "kind": "llm_agent",
        "runner_module": "robotsix_mill.runners.periodic_runner",
        "runner_func": "run_agent_check_pass",
        "label": "Agent Check",
    },
    "bc_check": {
        "kind": "llm_agent",
        "runner_module": "robotsix_mill.runners.periodic_runner",
        "runner_func": "run_bc_check_pass",
        "label": "BC Check",
    },
    "completeness_check": {
        "kind": "llm_agent",
        "runner_module": "robotsix_mill.runners.periodic_runner",
        "runner_func": "run_completeness_check_pass",
        "label": "Completeness",
    },
    "copy_paste": {
        "kind": "llm_agent",
        "runner_module": "robotsix_mill.runners.periodic_runner",
        "runner_func": "run_copy_paste_pass",
        "label": "Copy Paste",
    },
    "docstring_coverage": {
        "kind": "llm_agent",
        "runner_module": "robotsix_mill.runners.periodic_runner",
        "runner_func": "run_docstring_coverage_pass",
        "label": "Doc Coverage",
    },
    "forge_parity": {
        "kind": "llm_agent",
        "runner_module": "robotsix_mill.runners.periodic_runner",
        "runner_func": "run_forge_parity_pass",
        "label": "Forge Parity",
    },
    "frontend_sync": {
        "kind": "llm_agent",
        "runner_module": "robotsix_mill.runners.periodic_runner",
        "runner_func": "run_frontend_sync_pass",
        "label": "Frontend Sync",
    },
    "health": {
        "kind": "llm_agent",
        "runner_module": "robotsix_mill.runners.periodic_runner",
        "runner_func": "run_health_pass",
        "label": "Health Check",
    },
    "module_curator": {
        "kind": "llm_agent",
        "runner_module": "robotsix_mill.runners.periodic_runner",
        "runner_func": "run_module_curator_pass",
        "label": "Module Curator",
    },
    "state_sync": {
        "kind": "llm_agent",
        "runner_module": "robotsix_mill.runners.periodic_runner",
        "runner_func": "run_state_sync_pass",
        "label": "State Sync",
    },
    "survey": {
        "kind": "llm_agent",
        "runner_module": "robotsix_mill.runners.periodic_runner",
        "runner_func": "run_survey_pass",
        "label": "Survey",
    },
    "test_gap": {
        "kind": "llm_agent",
        "runner_module": "robotsix_mill.runners.periodic_runner",
        "runner_func": "run_test_gap_pass",
        "label": "Test Gaps",
    },
    "triage_boilerplate": {
        "kind": "llm_agent",
        "runner_module": "robotsix_mill.runners.periodic_runner",
        "runner_func": "run_triage_boilerplate_pass",
        "label": "Triage Boilerplate",
    },
    # -- schedule_only passes --
    "changelog_autofill": {
        "kind": "schedule_only",
        "runner_module": "robotsix_mill.runners.changelog_autofill_runner",
        "runner_func": "run_changelog_autofill_pass",
        "label": "Changelog Autofill",
    },
    "config_sync": {
        "kind": "schedule_only",
        "runner_module": "robotsix_mill.runners.periodic_runner",
        "runner_func": "run_config_sync_pass",
        "label": "Config Sync",
    },
    "data_dir_gc": {
        "kind": "schedule_only",
        "runner_module": "robotsix_mill.runners.data_dir_gc",
        "runner_func": "run_data_dir_gc_pass",
        "label": "Data Dir GC",
    },
    "member_sync": {
        "kind": "schedule_only",
        "runner_module": "robotsix_mill.runners.member_sync_runner",
        "runner_func": "run_member_sync_pass",
        "label": "Member Sync",
    },
    "pin_bump": {
        "kind": "schedule_only",
        "runner_module": "robotsix_mill.runners.pin_bump_runner",
        "runner_func": "run_pin_bump_pass",
        "label": "Pin Bump",
    },
    "repo_description_sync": {
        "kind": "schedule_only",
        "runner_module": "robotsix_mill.runners.repo_description_sync_runner",
        "runner_func": "run_repo_description_sync_pass",
        "label": "Repo Description Sync",
    },
    "roadmap_sync": {
        "kind": "schedule_only",
        "runner_module": "robotsix_mill.runners.roadmap_sync_runner",
        "runner_func": "run_roadmap_sync_pass",
        "label": "Roadmap Sync",
    },
    "trace_review": {
        "kind": "schedule_only",
        "runner_module": "robotsix_mill.runners.trace_review_runner",
        "runner_func": "run_trace_review_pass",
        "label": "Trace Review",
    },
    # -- global-only / no-tracing passes (formerly hand-wired routes) --
    "trace_health": {
        "kind": "schedule_only",
        "runner_module": "robotsix_mill.runners.trace_health_runner",
        "runner_func": "run_trace_health_pass",
        "label": "Trace Health",
        "uses_tracing": False,
    },
    "langfuse_cleanup": {
        "kind": "schedule_only",
        "runner_module": "robotsix_mill.runners.langfuse_cleanup_runner",
        "runner_func": "run_langfuse_cleanup_pass_wrapper",
        "label": "Langfuse Cleanup",
        "uses_tracing": False,
        "extra_runner_kwargs": lambda request: {
            "settings": request.app.state.settings,
            "max_traces": request.app.state.settings.langfuse_cleanup_max_traces,
        },
    },
    "meta": {
        "kind": "global_only",
        "runner_module": "robotsix_mill.meta.runner",
        "runner_func": "run_meta_pass_wrapper",
        "label": "Meta",
        "global_only": True,
    },
    "run_health": {
        "kind": "global_only",
        "runner_module": "robotsix_mill.runners.run_health_runner",
        "runner_func": "run_run_health_pass_wrapper",
        "label": "Run Health",
        "global_only": True,
    },
}


# ---------------------------------------------------------------------------
# Generic pass endpoints
# ---------------------------------------------------------------------------


@router.get("/passes")
def list_passes(
    request: Request,
    repo_id: str | None = None,
    repos=Depends(get_repos_registry),
) -> list[dict]:
    """Return all known periodic passes with their kind, label, and
    per-repo enabled status.

    When *repo_id* is given and resolves to a known repo, each entry
    carries an ``enabled`` bool derived from the repo's presence files
    (same resolution as the worker's periodic supervisor).  When
    *repo_id* is missing or ``"all"``, ``enabled`` is always ``False``.
    """
    enabled_names: set[str] = set()
    if repo_id and repo_id != "all":
        repo_config = repos.repos.get(repo_id)
        if repo_config is not None:
            from ...agents.periodic_loader import discover_periodic_workflows

            settings = request.app.state.settings
            worker = request.app.state.worker
            clone_dir = worker._find_config_clone_dir(repo_config)
            for wf in discover_periodic_workflows(clone_dir):
                if not wf.enabled:
                    continue
                if getattr(settings, f"{wf.name}_periodic", True) is False:
                    continue
                enabled_names.add(wf.name)

    return [
        {
            "name": name,
            "kind": entry["kind"],
            "label": entry["label"],
            "enabled": name in enabled_names or entry.get("global_only", False),
        }
        for name, entry in _PASS_REGISTRY.items()
    ]


@router.post("/passes/{pass_id}/run", status_code=202)
def run_pass(
    pass_id: str,
    repo_id: str | None = None,
    request: Request = None,  # type: ignore[assignment]
    registry=Depends(get_run_registry),
) -> dict:
    """Kick off a periodic pass in the BACKGROUND and return at once.

    Looks up *pass_id* in the pass registry, imports its runner lazily,
    and launches it in a daemon thread.  Works for both ``llm_agent``
    and ``schedule_only`` passes.
    """
    entry = _PASS_REGISTRY.get(pass_id)
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown pass: {pass_id!r}. Known: {sorted(_PASS_REGISTRY.keys())}",
        )
    runner_module: str = entry["runner_module"]
    runner_func: str = entry["runner_func"]
    uses_tracing: bool = entry.get("uses_tracing", True)
    global_only: bool = entry.get("global_only", False)
    extra_kwargs_builder: Callable[[Request], dict] | None = entry.get(
        "extra_runner_kwargs"
    )

    def _run() -> None:
        mod = importlib.import_module(runner_module)
        fn = getattr(mod, runner_func)

        if uses_tracing:
            from ..tracing import make_session_id, start_ticket_root_span

        if global_only:
            repo_configs: list[Any] = [None]
        else:
            repo_configs = _resolve_agent_run_repos(repo_id, request)

        extra: dict = extra_kwargs_builder(request) if extra_kwargs_builder else {}

        for rc in repo_configs:
            run_id = None
            try:
                repo_key = rc.repo_id if rc else ""
                run_id = registry.start(pass_id, repo_id=repo_key)

                if uses_tracing:
                    session_id = make_session_id(pass_id)
                    with start_ticket_root_span(session_id, pass_id, repo_config=rc):
                        r = fn(session_id=session_id, repo_config=rc, **extra)
                else:
                    r = fn(session_id=None, repo_config=rc, **extra)

                try:
                    summary = _default_summary(r)
                except AttributeError, TypeError:
                    summary = f"Pass completed: {r.summary if hasattr(r, 'summary') else str(r)[:200]}"

                registry.finish_ok(run_id, summary)
                log.info("%s pass done", pass_id)
            except Exception as e:  # noqa: BLE001 — background; just log
                log.exception("%s pass failed", pass_id)
                if run_id:
                    registry.finish_error(run_id, str(e))

    threading.Thread(target=_run, name=f"{pass_id}-pass", daemon=True).start()
    return {"status": "started"}
