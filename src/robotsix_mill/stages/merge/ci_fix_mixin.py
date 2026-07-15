"""MultiRepoCiFixMixin: inline CI-fix recovery for multi-repo merge tickets.

Extracted from ``MultiRepoMixin`` so that ``multi_repo.py`` stays under
the 600-line ceiling (AC #7).  ``MultiRepoCiFixMixin`` is a separate
mixin that ``MergeStage`` inherits from alongside ``MultiRepoMixin``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...config import ConfigError, get_repo_config, target_branch_for
from ...core.models import Ticket
from ...core.states import State
from ...forge import get_forge
from ..base import Outcome, StageContext
from ..ci_fix_codeql import (
    _CODQL_FP_TRIAGE_SENTINEL,
    _CODQL_FP_TRIAGE_VERDICTS,
    _codeql_block_note,
    _eligible_for_triage,
)
from ..ci_fix_helpers import _only_codeql_failing, _pr_changed_paths
from ._base import _MergeStageBase
from ._shared import (
    _build_failing_summary,
    _read_counter,
    _reconcile_with_remote_pr,
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

        # Fetch CI status BEFORE the attempt cap so FP triage can
        # intercept a CodeQL-only failure on the first poll.
        forge = get_forge(s, repo_config=rc)
        try:
            ci = forge.check_status(source_branch=branch)
        except Exception as e:  # noqa: BLE001 — transient
            log.warning(
                "%s: check_status failed for %s (retry): %s", ticket.id, repo_id, e
            )
            return Outcome(ticket.state)

        conclusion = (ci or {}).get("conclusion")
        cycle_counter_path = ws.artifacts_dir / f"ci_fix_{repo_id}_cycles.txt"
        if conclusion == "success":
            # CI turned green between polls — reset and re-poll.
            _write_counter(cycle_counter_path, 0)
            return Outcome(ticket.state)

        # Fetch alerts + changed_paths BEFORE the attempt cap so the
        # CodeQL FP triage path can inspect them.
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

        # --- CodeQL FP triage: early trigger before consuming attempts ---
        # If CodeQL is the sole remaining red check, try FP triage
        # immediately — before counting an attempt or a cycle.  The
        # triage call has its own guardrails (feature flag, run-once
        # sentinel, eligible alerts, etc.) and returns None when not
        # applicable.
        if conclusion == "failure":
            triage_outcome = self._try_multi_codeql_fp_triage(
                ticket, ctx, failing, alerts, changed_paths, repo_id, repo_dir
            )
            if triage_outcome is not None:
                return triage_outcome

        # --- Attempt cap ---
        counter_path = ws.artifacts_dir / f"ci_fix_{repo_id}.count"
        attempt = _read_counter(counter_path) + 1
        max_attempts = s.ci_fix_max_attempts
        if attempt > max_attempts:
            _write_counter(counter_path, 0)
            # Try to produce a CodeQL-specific block note when applicable.
            # NOTE: verdicts are not loaded here (categories 2/3 fall back to
            # the combined message); full verdict-aware diagnosis is delivered
            # by the single-repo CIFixStage path.
            codeql_note = _codeql_block_note(failing, alerts, changed_paths)
            if codeql_note is not None:
                return Outcome(
                    State.BLOCKED,
                    f"[{repo_id}] {codeql_note}",
                )
            return Outcome(
                State.BLOCKED,
                f"ci fix for {repo_id} failed after {max_attempts} attempt(s) — "
                "manual intervention required",
            )

        # --- Cycle ceiling gate (mirrors CIFixStage) ---
        if conclusion == "failure":
            cycles = _read_counter(cycle_counter_path)
            if s.ci_fix_max_cycles > 0 and cycles >= s.ci_fix_max_cycles:
                _write_counter(cycle_counter_path, 0)

                # --- CodeQL FP triage: last resort before BLOCKED ---
                triage_outcome = self._try_multi_codeql_fp_triage(
                    ticket, ctx, failing, alerts, changed_paths, repo_id, repo_dir
                )
                if triage_outcome is not None:
                    return triage_outcome

                log.warning(
                    "%s: multi-repo ci-fix for %s hit hard ceiling of %d cycle(s) "
                    "without turning CI green — escalating to BLOCKED",
                    ticket.id,
                    repo_id,
                    s.ci_fix_max_cycles,
                )
                # Try to produce a CodeQL-specific block note when applicable.
                # NOTE: verdicts are not loaded here (categories 2/3 fall back
                # to the combined message); full verdict-aware diagnosis is
                # delivered by the single-repo CIFixStage path.
                codeql_note2 = _codeql_block_note(failing, alerts, changed_paths)
                if codeql_note2 is not None:
                    return Outcome(
                        State.BLOCKED,
                        f"[{repo_id}] {codeql_note2}",
                    )
                return Outcome(
                    State.BLOCKED,
                    f"ci fix for {repo_id} exhausted hard ceiling of "
                    f"{s.ci_fix_max_cycles} cycle(s) without turning CI green "
                    f"— manual intervention required",
                )
            _write_counter(cycle_counter_path, cycles + 1)

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
        failing_names = ", ".join(f.get("name", "?") for f in failing) or "CI"
        self._note_ci_fix_attempt(
            ctx,
            ticket.id,
            f"🔧 ci_fix (cross-repo) attempt {attempt}/{max_attempts} for "
            f"`{repo_id}` — failing: {failing_names}",
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
                blocked = _reconcile_with_remote_pr(
                    _facade, repo_dir, remote_url, branch, token, ticket.id, repo_id
                )
                if blocked is not None:
                    return blocked

                mem_path = s.memory_file_for("ci_fix", rc.board_id)
                result = _facade.run_ci_fix_agent(
                    settings=s,
                    repo_dir=str(repo_dir),
                    branch=branch,
                    failing_summary=failing_summary,
                    memory=_facade.load_memory(mem_path),
                    ticket_id=ticket.id,
                    board_id=rc.board_id,
                    target=target_branch_for(s, rc),
                    remote_url=remote_url,
                    token=token,
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
                self._note_ci_fix_attempt(
                    ctx,
                    ticket.id,
                    f"ci_fix (cross-repo) attempt {attempt}/{max_attempts} for "
                    f"`{repo_id}`: agent reported DONE but made no commits "
                    f"— re-polling",
                )
                return Outcome(ticket.state)
            try:
                check = _facade.git_ops.post_push_check(
                    repo_dir,
                    branch=branch,
                    target=target_branch_for(s, rc),
                    remote_url=_facade._resolve_remote_url(s, rc),
                    token=_facade.github_token(s, repo_config=rc),
                )
                if check is _facade.git_ops.PostPushResult.PASS:
                    _write_counter(counter_path, 0)
                    log.info(
                        "%s: multi-repo ci fix push verified for %s — re-poll",
                        ticket.id,
                        repo_id,
                    )
                    return Outcome(ticket.state)
                _write_counter(counter_path, attempt)
                return Outcome(
                    State.BLOCKED,
                    f"ci fix for {repo_id} post-check failed: {check}",
                )
            except Exception as e:  # noqa: BLE001
                _write_counter(counter_path, attempt)
                return Outcome(
                    State.BLOCKED,
                    f"ci fix for {repo_id} post-check error: {e}",
                )

        # Agent failed — record the attempt and re-poll.
        _write_counter(counter_path, attempt)
        log.warning(
            "%s: multi-repo ci-fix attempt %d/%d failed for %s — retrying next poll",
            ticket.id,
            attempt,
            max_attempts,
            repo_id,
        )
        self._note_ci_fix_attempt(
            ctx,
            ticket.id,
            f"ci_fix (cross-repo) attempt {attempt}/{max_attempts} for "
            f"`{repo_id}` failed (agent error) — retrying next poll",
        )
        return Outcome(ticket.state)

    @staticmethod
    def _note_ci_fix_attempt(ctx: StageContext, ticket_id: str, note: str) -> None:
        """Record a per-attempt cross-repo ci-fix breadcrumb in ticket history.

        The multi-repo merge stage runs the ci-fix loop inline — the ticket
        stays in ``IMPLEMENT_COMPLETE`` for the whole loop, so (unlike the
        single-repo ``FIXING_CI`` path) there is no state transition per
        attempt to leave a trail.  Without this, a ticket that BLOCKs with
        "ci fix for <repo> failed after N attempt(s)" shows zero ``fixing_ci``
        rows in ``/history`` — which reads as a mystery to a human triaging
        it.  ``add_history_note`` appends a side-band row at the current state
        (no transition, hash chain intact).  Recording must never break the
        loop, so failures here are swallowed.
        """
        try:
            ctx.service.add_history_note(ticket_id, note)
        except Exception:  # noqa: BLE001 — history note is best-effort
            log.warning("%s: failed to record ci-fix attempt note", ticket_id)

    def _try_multi_codeql_fp_triage(  # noqa: C901 — guardrail chain is inherently branchy
        self,
        ticket: Ticket,
        ctx: StageContext,
        failing: list[dict[str, Any]],
        alerts: list[dict[str, Any]],
        changed_paths: set[str],
        repo_id: str,
        repo_dir: Path,
    ) -> Outcome | None:
        """Try the codeql_fp_triage sub-agent before blocking on CodeQL FPs.

        Mirrors ``CIFixStage._try_codeql_fp_triage`` for the multi-repo path.
        """
        s = ctx.settings

        if not s.codeql_fp_triage_enabled:
            return None

        if not _only_codeql_failing(failing):
            return None

        ws = ctx.service.workspace(ticket)
        sentinel_path = ws.artifacts_dir / _CODQL_FP_TRIAGE_SENTINEL
        if sentinel_path.exists():
            log.info(
                "%s: codeql_fp_triage already ran for this ticket — skipping",
                ticket.id,
            )
            return None
        _write_counter(sentinel_path, 1)

        eligible = _eligible_for_triage(alerts, changed_paths, max_dismissals=5)
        if not eligible:
            log.info(
                "%s: no CodeQL alerts eligible for FP triage in %s",
                ticket.id,
                repo_id,
            )
            return None

        log.info(
            "%s: attempting codeql_fp_triage on %d eligible alert(s) in %s",
            ticket.id,
            len(eligible),
            repo_id,
        )

        import json

        from ...agents.codeql_fp_triage import run_codeql_fp_triage_agent

        try:
            from ...config import get_repo_config

            rc = get_repo_config(repo_id)
            result = run_codeql_fp_triage_agent(
                settings=s,
                repo_dir=repo_dir,
                alerts_json=json.dumps(eligible),
                ticket_id=ticket.id,
                board_id=rc.board_id,
            )
        except Exception:  # noqa: BLE001
            log.warning(
                "%s: codeql_fp_triage agent crashed for %s",
                ticket.id,
                repo_id,
                exc_info=True,
            )
            return None

        # Persist verdicts for the block-note builder.
        try:
            import json as _json

            verdicts_path = ws.artifacts_dir / _CODQL_FP_TRIAGE_VERDICTS
            verdicts_path.parent.mkdir(parents=True, exist_ok=True)
            verdicts_path.write_text(
                _json.dumps([v.model_dump() for v in result.verdicts]),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001 — best-effort
            log.warning(
                "%s: failed to persist codeql_fp_triage verdicts for %s",
                ticket.id,
                repo_id,
                exc_info=True,
            )

        dismissals = [v for v in result.verdicts if v.verdict == "dismiss"]
        if not dismissals:
            log.info(
                "%s: codeql_fp_triage abstained on all %d alert(s) in %s — blocking",
                ticket.id,
                len(eligible),
                repo_id,
            )
            return None

        forge = get_forge(s, repo_config=rc)
        dismissed_count = 0
        dismissal_notes: list[str] = []
        for v in dismissals:
            ok = forge.dismiss_code_scanning_alert(
                number=v.alert_number,
                reason="false positive",
                comment=v.rationale[:4000],
            )
            if ok:
                dismissed_count += 1
                dismissal_notes.append(
                    f"- Alert #{v.alert_number} [{repo_id}]: {v.rationale[:200]}"
                )
            else:
                log.warning(
                    "%s: failed to dismiss code-scanning alert %d in %s",
                    ticket.id,
                    v.alert_number,
                    repo_id,
                )

        try:
            note_lines = [
                "## codeql_fp_triage: auto-dismissed CodeQL false positive(s)",
                "",
                f"Repo: {repo_id} — dismissed {dismissed_count} alert(s) "
                f"out of {len(eligible)} eligible:",
                "",
            ]
            note_lines.extend(dismissal_notes)
            note_lines.append("")
            note_lines.append(
                "These dismissals persist by fingerprint and will also "
                "clear the alert on `main` after merge.  A human can "
                "re-open any alert via the GitHub security tab."
            )
            ctx.service.add_history_note(ticket.id, "\n".join(note_lines))
        except Exception:  # noqa: BLE001
            log.warning("%s: failed to record codeql_fp_triage note", ticket.id)

        if dismissed_count > 0:
            log.info(
                "%s: codeql_fp_triage dismissed %d alert(s) in %s — re-poll",
                ticket.id,
                dismissed_count,
                repo_id,
            )
            return Outcome(ticket.state)

        return None
