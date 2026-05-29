"""Operational monitoring + traces + deep review routes."""

from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..deps import (
    get_repos_registry,
    get_run_registry,
    get_service,
    get_settings,
    get_worker,
)

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/runs")
def list_runs(
    repo_id: str | None = None,
    request: Request = None,
    registry=Depends(get_run_registry),
) -> list[dict]:
    """Return recent background-run entries (newest first).

    ``?repo_id=X`` filters to runs associated with that repo.
    When omitted, returns all (current behaviour preserved).
    """
    entries = registry.list_all()
    if repo_id is not None:
        repos = request.app.state.repos
        if repo_id == "all":
            pass  # no filtering
        elif repo_id not in repos.repos:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown repo: '{repo_id}'. Known repos: "
                f"{sorted(repos.repos.keys())}",
            )
        else:
            # Filter entries that carry a repo_id matching the request.
            # Empty repo_id is treated as "applies to any repo" — covers
            # legacy entries filed before per-repo tagging landed plus
            # global runs from periodic agents that don't carry a
            # repo_id today. Strict equality on a non-empty filter would
            # hide every pre-wiring run in single-repo deployments.
            entries = [
                e for e in entries
                if e.get("repo_id") == repo_id or not e.get("repo_id")
            ]
    return entries


@router.get("/active")
def list_active(
    repo_id: str | None = None,
    request: Request = None,
    worker=Depends(get_worker),
) -> list[dict]:
    """Return tickets currently being processed by a pipeline stage.

    ``?repo_id=X`` filters to active tickets belonging to that repo.
    When omitted, returns all (current behaviour preserved).
    """
    active = [
        {"ticket_id": tid, "stage": info["stage"], "started_at": info["started_at"]}
        for tid, info in worker._active.items()
    ]
    if repo_id is not None:
        repos = request.app.state.repos
        if repo_id == "all":
            pass  # no filtering
        elif repo_id not in repos.repos:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown repo: '{repo_id}'. Known repos: "
                f"{sorted(repos.repos.keys())}",
            )
        else:
            target_board = repos.repos[repo_id].board_id
            # Look up each active ticket's board_id from the service
            filtered = []
            for item in active:
                ticket = worker.ctx.service.get(item["ticket_id"])
                if ticket and ticket.board_id == target_board:
                    filtered.append(item)
            active = filtered
    return active


@router.get("/traces/recent")
def list_recent_traces(
    limit: int = 10,
    min_cost: float | None = None,
    max_cost: float | None = None,
    settings=Depends(get_settings),
) -> list[dict]:
    """Return recent Langfuse traces, filtered by cost and limited in
    count.  *limit* is clamped to 1–50; *min_cost* and *max_cost* are
    inclusive USD filters on ``totalCost``."""
    from ...langfuse_client import list_recent_traces as _list_recent

    limit = max(1, min(limit, 50))
    traces = _list_recent(
        settings,
        limit=limit,
        min_cost=min_cost,
        max_cost=max_cost,
    )
    return [
        {
            "id": t.get("id", ""),
            "name": t.get("name", ""),
            "timestamp": t.get("timestamp", ""),
            "sessionId": t.get("sessionId"),
            "totalCost": t.get("totalCost"),
            "userId": t.get("userId"),
        }
        for t in traces
    ]


@router.post("/traces/{trace_id}/deep-review", status_code=202)
def deep_review_trace(
    trace_id: str,
    request: Request,
    settings=Depends(get_settings),
    registry=Depends(get_run_registry),
) -> dict:
    """Start a background deep review of a single Langfuse trace."""
    if not settings.tracing_enabled:
        return {"status": "unavailable"}

    state = request.app.state

    from ...langfuse_client import fetch_trace_detail
    from ...agents.trace_inspector import run_trace_inspector
    from .. import tracing

    run_id = registry.start("deep-review")

    def _run() -> None:
        try:
            detail = fetch_trace_detail(settings, trace_id)
            if detail is None:
                data = {
                    "status": "error",
                    "error": "trace unavailable — could not fetch from Langfuse",
                    "findings": [],
                    "source_trace_name": "(unnamed)",
                }
                state.deep_review_results[trace_id] = data
                state.deep_review_store.put(trace_id, data)
                registry.finish_error(
                    run_id, f"deep review of trace {trace_id}: trace unavailable"
                )
                return

            import json as _json
            import subprocess
            from ...vcs import git_ops

            # Clone the forge repo so the inspector can read_file /
            # list_dir / explore the actual code that produced this
            # trace. Best-effort: if the clone fails (no forge
            # configured, network down) we still run the inspector
            # in tool-less mode. The clone is at a stable, reusable
            # path; later passes reuse it.
            repo_dir = None
            if settings.forge_remote_url:
                cand = settings.data_dir / "deep_review_workspace" / "repo"
                try:
                    if (cand / ".git").exists():
                        # Update the existing clone in place.
                        try:
                            git_ops.try_rebase_onto(cand, settings.forge_target_branch)
                        except Exception:  # noqa: BLE001 — best effort
                            pass
                        repo_dir = cand
                    else:
                        git_ops.clone(
                            settings.forge_remote_url, cand,
                            settings.forge_target_branch, get_secrets().forge_token,
                        )
                        repo_dir = cand
                except subprocess.CalledProcessError as e:
                    log.warning(
                        "deep review clone failed (running tool-less): %s",
                        (e.stderr or "")[:200],
                    )

            # Read inspector memory (best-effort).
            memory_file = settings.memory_file_for("trace_inspector", "")
            memory = ""
            if memory_file.exists():
                try:
                    memory = memory_file.read_text(encoding="utf-8")
                except OSError:
                    memory = ""

            trace_data = _json.dumps(detail, default=str)
            # Wrap the LLM call in an OTel root span so its pydantic-ai
            # spans get exported as a properly-named, session-grouped
            # Langfuse trace.
            with tracing.start_ticket_root_span(
                tracing.make_session_id("deep-review"), "deep-review",
                extra_attributes={"source_trace_id": trace_id},
            ):
                result = run_trace_inspector(
                    settings=settings,
                    trace_data=trace_data,
                    repo_dir=repo_dir,
                    memory=memory,
                )
            # Persist updated memory verbatim (atomic write).
            if result.updated_memory:
                try:
                    memory_file.parent.mkdir(parents=True, exist_ok=True)
                    tmp = memory_file.with_suffix(".md.tmp")
                    tmp.write_text(result.updated_memory, encoding="utf-8")
                    tmp.replace(memory_file)
                except OSError as e:
                    log.warning(
                        "deep review: could not write memory file: %s", e
                    )

            data = {
                # JS renderDeepReviewResult treats status=="error" as
                # "show the error message" — use it for inspector
                # failures too so the UI surfaces the cause instead of
                # rendering an indistinguishable all-zeros result.
                "status": "ok" if not result.error else "error",
                "trace_id": trace_id,
                "findings": [f.model_dump() for f in result.findings],
                "error": result.error,
            }
            data["source_trace_name"] = detail.get("name", "(unnamed)")
            state.deep_review_results[trace_id] = data
            state.deep_review_store.put(trace_id, data)

            n_findings = len(result.findings)
            n_te = sum(1 for f in result.findings if f.category == "tool_error")
            n_al = sum(1 for f in result.findings if f.category == "agent_limitation")
            n_opt = sum(1 for f in result.findings if f.category == "optimization")
            if result.error:
                summary = f"deep review of trace {trace_id}: {result.error[:120]}"
                registry.finish_error(run_id, result.error[:300])
            else:
                summary = (
                    f"deep review of trace {trace_id}: "
                    f"{n_findings} findings ({n_te} TE, {n_al} AL, {n_opt} OPT)"
                )
                registry.finish_ok(run_id, summary)
            log.info("deep review of trace %s complete", trace_id)
        except Exception as e:  # noqa: BLE001 — background; just log
            log.exception("deep review of trace %s failed", trace_id)
            data = {
                "status": "error",
                "error": str(e),
                "findings": [],
                "source_trace_name": "(unnamed)",
            }
            state.deep_review_results[trace_id] = data
            state.deep_review_store.put(trace_id, data)
            registry.finish_error(run_id, str(e))

    # Mark as running before thread starts.
    state.deep_review_results[trace_id] = {"status": "running"}
    threading.Thread(
        target=_run, name=f"deep-review-{trace_id}", daemon=True
    ).start()
    return {"status": "started", "trace_id": trace_id}


@router.get("/deep-review/{trace_id}")
def get_deep_review_result(
    trace_id: str,
    request: Request,
) -> dict:
    """Return the stored deep-review result for *trace_id*."""
    state = request.app.state
    # Check in-memory first (catches running + recently completed).
    results = getattr(state, "deep_review_results", None)
    if results and trace_id in results:
        entry = results[trace_id]
        if isinstance(entry, dict) and entry.get("status") == "running":
            return entry
        return entry
    # Fall back to disk store.
    store = getattr(state, "deep_review_store", None)
    if store is not None:
        entry = store.get(trace_id)
        if entry is not None:
            return entry
    raise HTTPException(404, "no review found for this trace")


@router.get("/deep-review")
def list_deep_reviews(request: Request) -> list[dict]:
    """Return all stored deep reviews, newest first. Empty list if none."""
    store = getattr(request.app.state, "deep_review_store", None)
    if store is None:
        return []
    return store.list_all()