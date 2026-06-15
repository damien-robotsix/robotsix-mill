"""MultiRepoCiFixMixin: inline CI-fix recovery for multi-repo merge tickets.

Extracted from ``MultiRepoMixin`` so that ``multi_repo.py`` stays under
the 600-line ceiling (AC #7).  ``MultiRepoCiFixMixin`` is a separate
mixin that ``MergeStage`` inherits from alongside ``MultiRepoMixin``.
"""

from __future__ import annotations

from pathlib import Path

from ...config import ConfigError, get_repo_config
from ...core.models import Ticket
from ...core.states import State
from ...forge import get_forge
from ..base import Outcome, StageContext
from ..ci_fix import _pr_changed_paths
from ._base import _MergeStageBase
from ._shared import (
    _build_failing_summary,
    _read_counter,
    _write_counter,
    log,
)


class MultiRepoCiFixMixin(_MergeStageBase):
    """Inline CI-fix recovery for multi-repo tickets.

    Runs the CI-fix agent on one failing multi-repo PR per poll,
    bounded by a per-repo attempt counter.
    """

    def _multi_repo_fix_ci(
        self, ticket: Ticket, ctx: StageContext, status: dict
    ) -> Outcome:
        """Run the CI-fix agent on one multi-repo PR whose CI is failing.

        Mirrors the single-repo :class:`CIFixStage` but inline (a multi-repo
        ticket has one state). Bounded by a per-repo attempt counter; a
        ``DONE`` agent run that produces no new commits still counts toward the
        cap so a flaky CI can't loop forever. Exhausting the cap -> BLOCKED.
        Returns the ticket's current state (re-poll) while making progress.
        """
        s = ctx.settings
        from robotsix_mill.stages import merge as _facade

        repo_id = status["repo_id"]
        branch = status["branch"]
        ws = ctx.service.workspace(ticket)
        repo_dir = ws.dir / "repos" / repo_id
        if not (repo_dir / ".git").exists():
            return Outcome(
                State.BLOCKED,
                f"clone for {repo_id} missing — re-run implement",
            )
        try:
            rc = get_repo_config(repo_id)
        except ConfigError as e:
            return Outcome(
                State.BLOCKED, f"unknown repo_id '{repo_id}': {e} — resumable"
            )

        counter_path = ws.artifacts_dir / f"ci_fix_{repo_id}.count"
        attempt = _read_counter(counter_path) + 1
        max_attempts = s.ci_fix_max_attempts
        if attempt > max_attempts:
            _write_counter(counter_path, 0)
            return Outcome(
                State.BLOCKED,
                f"ci fix for {repo_id} failed after {max_attempts} attempt(s) — "
                "manual intervention required",
            )

        forge = get_forge(s, repo_config=rc)
        try:
            ci = forge.check_status(source_branch=branch)
        except Exception as e:  # noqa: BLE001 — transient
            log.warning(
                "%s: check_status failed for %s (retry): %s", ticket.id, repo_id, e
            )
            return Outcome(ticket.state)

        # --- cycle-ceiling gate (mirrors CIFixStage) ---
        conclusion = (ci or {}).get("conclusion")
        cycle_counter_path = ws.artifacts_dir / f"ci_fix_{repo_id}_cycles.txt"
        if conclusion == "success":
            # CI turned green between polls — reset and re-poll.
            _write_counter(cycle_counter_path, 0)
            return Outcome(ticket.state)
        if conclusion == "failure":
            cycles = _read_counter(cycle_counter_path)
            if s.ci_fix_max_cycles > 0 and cycles >= s.ci_fix_max_cycles:
                _write_counter(cycle_counter_path, 0)
                log.warning(
                    "%s: multi-repo ci-fix for %s hit hard ceiling of %d cycle(s) "
                    "without turning CI green — escalating to BLOCKED",
                    ticket.id,
                    repo_id,
                    s.ci_fix_max_cycles,
                )
                return Outcome(
                    State.BLOCKED,
                    f"ci fix for {repo_id} exhausted hard ceiling of "
                    f"{s.ci_fix_max_cycles} cycle(s) without turning CI green "
                    f"— manual intervention required",
                )
            _write_counter(cycle_counter_path, cycles + 1)

        failing = (ci or {}).get("failing", [])

        log_text = ""
        alerts: list[dict] = []
        changed_paths: set[str] = set()
        try:
            alerts = forge.list_code_scanning_alerts(source_branch=branch)
            changed_paths = _pr_changed_paths(forge, branch)
            pr = forge.pr_status(source_branch=branch)
            head_sha = (pr or {}).get("sha", "")
            if head_sha:
                for run in forge.list_workflow_runs(head_sha=head_sha):
                    if run.get("conclusion") == "failure":
                        logs = forge.fetch_workflow_job_logs(run_id=run["id"])
                        if logs:
                            log_text += (
                                f"\n--- {run.get('name', 'workflow')} "
                                f"(run {run['id']}) ---\n{logs}"
                            )
        except Exception:  # noqa: BLE001 — best-effort enrichment
            log.warning(
                "%s: failed to fetch job logs / alerts for %s", ticket.id, repo_id
            )

        failing_summary = _build_failing_summary(
            failing, log_text, alerts, changed_paths
        )
        log.info(
            "%s: multi-repo CI failing for %s — ci-fix attempt %d/%d",
            ticket.id,
            repo_id,
            attempt,
            max_attempts,
        )

        ok = False
        try:
            # Attribute the agent's cost/traces to the ticket's session, and
            # to the TARGET repo's Langfuse project, not an orphan trace.
            with _facade.tracing.start_ticket_root_span(
                ticket.id, "ci_fix", repo_config=rc
            ):
                remote_url = _facade._resolve_remote_url(s, rc)
                token = _facade.github_token(s, repo_config=rc)

                # Reconcile with remote PR branch first so the ci-fix
                # agent sees any foreign commits.
                reconciled = _facade.git_ops.reconcile_with_remote_pr(
                    Path(repo_dir), remote_url, branch, token
                )
                if reconciled is _facade.git_ops.ReconcileResult.DIVERGED:
                    return Outcome(
                        State.BLOCKED,
                        f"{repo_id}: PR branch diverged from the workspace clone (a human "
                        f"likely pushed to it) — manual reconciliation required. "
                        f"The mill refuses to force-push: push_with_lease cannot "
                        f"protect this case (reconcile already fetched the foreign "
                        f"commit into the lease ref), so it would silently "
                        f"overwrite that commit.",
                    )
                if reconciled is _facade.git_ops.ReconcileResult.UNAVAILABLE:
                    log.warning(
                        "%s: %s: could not reach the remote PR branch to "
                        "reconcile — proceeding; push_with_lease backstops a "
                        "stale push",
                        ticket.id,
                        repo_id,
                    )

                mem_path = s.memory_file_for("ci_fix", rc.board_id)
                result = _facade.run_ci_fix_agent(
                    settings=s,
                    repo_dir=str(repo_dir),
                    branch=branch,
                    failing_summary=failing_summary,
                    memory=_facade.load_memory(mem_path),
                    ticket_id=ticket.id,
                    board_id=rc.board_id,
                )
                ok = result.status == "DONE"
                if result.updated_memory:
                    _facade.persist_memory(mem_path, result.updated_memory)
        except Exception as e:  # noqa: BLE001
            log.exception(
                "%s: multi-repo ci-fix crashed for %s: %s", ticket.id, repo_id, e
            )
            ok = False

        if ok:
            # No new commits (agent reported DONE but changed nothing) still
            # counts toward the cap so a flaky check can't loop forever.
            try:
                local = _facade.git_ops.head_sha(repo_dir)
                remote = _facade.git_ops.remote_branch_sha(repo_dir, branch)
            except Exception:  # noqa: BLE001 — be safe: assume changes
                local, remote = None, "force-push"
            if local is not None and remote == local:
                _write_counter(counter_path, attempt)
                log.info(
                    "%s: multi-repo ci-fix for %s made no changes (attempt %d/%d)",
                    ticket.id,
                    repo_id,
                    attempt,
                    max_attempts,
                )
                return Outcome(ticket.state)
            try:
                _facade.git_ops.push_with_lease(
                    repo_dir,
                    branch=branch,
                    remote_url=_facade._resolve_remote_url(s, rc),
                    token=_facade.github_token(s, repo_config=rc),
                )
            except Exception as e:  # noqa: BLE001
                _write_counter(counter_path, attempt)
                return Outcome(
                    State.BLOCKED,
                    f"ci fix for {repo_id} succeeded but force-push failed: {e}",
                )
            _write_counter(counter_path, 0)
            log.info(
                "%s: multi-repo ci fix pushed for %s — re-poll", ticket.id, repo_id
            )
            return Outcome(ticket.state)

        # Agent failed — record the attempt and re-poll.
        _write_counter(counter_path, attempt)
        log.warning(
            "%s: multi-repo ci-fix attempt %d/%d failed for %s — retrying next poll",
            ticket.id,
            attempt,
            max_attempts,
            repo_id,
        )
        return Outcome(ticket.state)
