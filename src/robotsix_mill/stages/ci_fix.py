"""CI-fix stage: FIXING_CI -> IMPLEMENT_COMPLETE (fix succeeded) | BLOCKED.

When the merge stage detects a mergeable PR with failing remote CI
checks, it transitions the ticket to FIXING_CI.  This stage invokes
the ci-fix agent to auto-resolve the failures, commits locally, and
force-pushes only the ticket branch.  On success the ticket goes back
to IMPLEMENT_COMPLETE so the merge stage re-verifies both gates before
promoting to HUMAN_MR_APPROVAL.

Failure after max attempts escalates to BLOCKED (resumable).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..agents.ci_fixing import CiFixResult, run_ci_fix_agent
from ..config import target_branch_for
from ..core.models import SourceKind, Ticket
from ..core.states import State
from ..forge import get_forge
from ..forge.auth import _resolve_remote_url, github_push_token, github_token
from ..forge.github_code_scanning import CodeScanningAlertsUnavailable
from ..runners.pass_runner import load_memory, persist_memory
from ..runtime import tracing
from ..vcs import git_ops
from . import dependency_fix
from .base import Outcome, Stage, StageContext
from .ci_fix_codeql import (
    _CODQL_CHECK_NAMES,
    _CODQL_FP_TRIAGE_VERDICTS,
    _codeql_block_note,
    _partition_open_alerts,
    _try_codeql_fp_triage,
)
from .ci_fix_helpers import (
    _CI_FAILURE_FINGERPRINT,
    _CI_IDENTICAL_FAILURE_COUNT,
    _CI_REFRESH_COUNTER,
    _FailingContext,
    _build_failing_summary,
    _ci_failure_fingerprint,
    _format_alert_refs,
    _pr_changed_paths,
    _read_counter,
    _write_counter,
    _write_text,
    _workspace_repo_dir,
)

log = logging.getLogger("robotsix_mill.stages.ci_fix")


class CIFixStage(Stage):
    """Check forge CI status and run automated fix logic to resolve CI failures on the ticket branch."""

    name = "ci_fix"
    input_state = State.FIXING_CI
    traced = False

    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Process a FIXING_CI ticket: poll forge CI status on the ticket branch and, on failure, run the automated CI-fix agent to push corrective commits."""
        # Clone phase: guards, branch resolution, and CI status routing.
        resolved = self._resolve_clone_and_status(ticket, ctx)
        if isinstance(resolved, Outcome):
            return resolved
        (
            repo_dir,
            branch,
            failing_summary,
            failing,
            alerts,
            changed_paths,
            alerts_unreadable,
            head_sha,
        ) = resolved

        # --- Early guard: CodeQL failing but alerts unreadable (403) ---
        # When CodeQL is among the failing checks and the alerts API
        # returned 403 (permission gap), block immediately with an
        # actionable note — the ci-fix agent must never reach the
        # blind-suppression path when alert details are unavailable.
        if alerts_unreadable and any(
            any(
                token in (chk.get("name") or "").lower() for token in _CODQL_CHECK_NAMES
            )
            for chk in failing
        ):
            codeql_note = _codeql_block_note(
                failing, alerts, changed_paths, alerts_unreadable=True
            )
            return Outcome(State.BLOCKED, codeql_note or "")

        # --- CodeQL FP triage: early trigger before consuming attempts ---
        # If CodeQL is the sole remaining red check, try FP triage
        # immediately.  The triage call has its own guardrails
        # (feature flag, run-once sentinel, eligible alerts, etc.) and
        # returns None when not applicable.
        triage_outcome = _try_codeql_fp_triage(
            ticket, ctx, failing, alerts, changed_paths
        )
        if triage_outcome is not None:
            return triage_outcome

        # Identical-failure gate: when the same CI failure fingerprint repeats
        # ci_fix_max_identical_failures times in a row, escalate to BLOCKED.
        identical_outcome = self._check_consecutive_identical_failure(
            ticket, ctx, failing_summary, head_sha
        )
        if identical_outcome is not None:
            return identical_outcome

        # --- Duplicate changelog fragment recovery (before LLM agent) ---
        dedup_outcome = self._try_dedup_changelog_fragments(
            ticket, ctx, repo_dir, branch
        )
        if dedup_outcome is not None:
            return dedup_outcome

        # Agent phase: the ci-fix agent now OWNS the fix→push→verify loop —
        # it fixes, pushes, and calls wait_for_ci to re-check, iterating up to
        # ci_fix_max_iterations before giving up. There is no external
        # FIXING_CI ⇄ IMPLEMENT_COMPLETE retry loop and no per-ticket cycle
        # counter: the iteration budget lives inside the wait_for_ci tool.
        log.info(
            "%s: CI failing — running ci-fix agent (owns fix/verify loop)",
            ticket.id,
        )
        return self._run_agent_and_finalize(
            ticket,
            ctx,
            repo_dir,
            branch,
            failing_summary,
            failing,
            alerts,
            changed_paths,
            head_sha,
        )

    def _resolve_clone_and_status(  # noqa: C901 — multi-step routing; each branch is simple
        self, ticket: Ticket, ctx: StageContext
    ) -> Outcome | _FailingContext:
        """Run the guards, resolve the clone, and route on CI status.

        Returns an early ``Outcome`` for every non-failure path (guards
        failing → BLOCKED; transient/None/pending/success/unknown →
        IMPLEMENT_COMPLETE). When CI is genuinely failing, returns a
        ``_FailingContext`` carrying the data the later phases need.
        """
        s = ctx.settings

        # Guard: forge configured.
        if s.forge_kind == "none":
            return Outcome(State.BLOCKED, "forge not configured")
        try:
            github_token(s)  # surfaces a clear config error early
        except RuntimeError as e:
            return Outcome(State.BLOCKED, f"forge auth not configured: {e}")

        branch = ticket.branch or f"{s.branch_prefix}{ticket.id}"

        # Guard: workspace clone must exist.
        repo_dir = _workspace_repo_dir(ctx, ticket)
        if repo_dir is None:
            return Outcome(
                State.BLOCKED,
                "workspace clone is missing; cannot fix CI. "
                "Re-run implement to recreate the clone.",
            )

        # --- Rebase onto current main before scanning CI ---
        # A stale branch can carry a CI fingerprint from an already-fixed
        # upstream issue (e.g. a resolved PYSEC advisory).  Rebase onto
        # current main and force-push so the CI re-runs against the
        # latest base — the fresh run produces a different head SHA,
        # which feeds into the failure fingerprint and prevents the
        # consecutive-identical backstop from re-blocking a ticket whose
        # upstream issue has already been resolved.
        _target = target_branch_for(s, ctx.repo_config)
        _remote_url = _resolve_remote_url(s, ctx.repo_config)
        _token = github_push_token(s, repo_config=ctx.repo_config)
        try:
            _did_rebase = git_ops.try_rebase_onto(
                Path(repo_dir),
                _target,
                remote_url=_remote_url,
                token=_token,
            )
            if _did_rebase:
                git_ops.push(Path(repo_dir), branch, _remote_url, _token)
                log.info(
                    "%s: rebased onto %s and pushed before CI scan",
                    ticket.id,
                    _target,
                )
            else:
                log.warning(
                    "%s: rebase onto %s failed or was unnecessary — "
                    "proceeding with existing branch HEAD",
                    ticket.id,
                    _target,
                )
        except Exception:
            log.warning(
                "%s: rebase step failed — proceeding with existing branch",
                ticket.id,
                exc_info=True,
            )

        # Fetch check status from the forge.
        try:
            status = get_forge(s, repo_config=ctx.repo_config).check_status(
                source_branch=branch, require_checks=True
            )
        except Exception as e:  # noqa: BLE001 — transient
            log.warning("%s: check_status failed (retry): %s", ticket.id, e)
            return Outcome(State.IMPLEMENT_COMPLETE)

        if status is None:
            # PR disappeared.
            return Outcome(State.IMPLEMENT_COMPLETE)

        conclusion = status.get("conclusion")

        if conclusion == "success":
            # CI turned green while we were waiting — re-poll; merge will
            # promote to HUMAN_MR_APPROVAL.
            #
            # Do NOT reset the hard cycle ceiling here. A flickering CI (a
            # repo with several workflows / re-runs) returns a momentary
            # "success" between failing cycles; resetting on that transient
            # green was exactly what let a runaway ci_fix loop survive ~200
            # cycles (the counter never reached the ceiling). The counter is
            # reset only on GENUINE forward progress — when merge advances the
            # ticket out of the CI gate to HUMAN_MR_APPROVAL (merge.py).
            #
            # Do reset the refresh counter, though: CI going green is genuine
            # forward progress, so a later, independent staleness can be
            # refreshed once more.
            _write_counter(
                ctx.service.workspace(ticket).artifacts_dir / _CI_REFRESH_COUNTER, 0
            )
            return Outcome(State.IMPLEMENT_COMPLETE)

        if conclusion in ("pending", None):
            # Not yet complete; re-poll from human_mr_approval.
            return Outcome(State.IMPLEMENT_COMPLETE)

        if conclusion != "failure":
            # Unknown conclusion — treat as pending, re-poll.
            return Outcome(State.IMPLEMENT_COMPLETE)

        # --- CI is failing → attempt fix ---
        failing = status.get("failing", [])
        failing_summary, alerts, changed_paths, alerts_unreadable, head_sha = (
            self._build_failure_detail(ticket, ctx, branch, failing)
        )
        # Persist the failure detail for observability.
        self._write_failing_summary_artifact(ctx, ticket, failing_summary, failing)
        return _FailingContext(
            repo_dir,
            branch,
            failing_summary,
            failing,
            alerts,
            changed_paths,
            alerts_unreadable,
            head_sha,
        )

    def _build_failure_detail(  # noqa: C901 — enrichment is inherently branchy
        self,
        ticket: Ticket,
        ctx: StageContext,
        branch: str,
        failing: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]], set[str], bool, str]:
        """Enrich the failing-check list with job logs + code-scanning alerts.

        Returns ``(failing_summary, alerts, changed_paths, alerts_unreadable,
        head_sha)`` so callers can inspect the raw alert data (e.g. for FP
        triage gating), detect when alerts were unreadable due to a 403
        permission gap, and include the branch HEAD SHA in the failure
        fingerprint.
        """
        s = ctx.settings

        # Fetch job logs + code-scanning alerts for richer context (only on
        # failure, not on every PR poll — this stage runs infrequently).
        log_text = ""
        alerts: list[dict[str, Any]] = []
        changed_paths: set[str] = set()
        alerts_unreadable = False
        head_sha = ""
        try:
            forge = get_forge(s, repo_config=ctx.repo_config)
            alerts = forge.list_code_scanning_alerts(source_branch=branch)
            changed_paths = _pr_changed_paths(forge, branch)
            pr = forge.pr_status(source_branch=branch)
            head_sha = (pr or {}).get("sha", "")
            if head_sha:
                runs = forge.list_workflow_runs(head_sha=head_sha)
                for run in runs:
                    if run.get("conclusion") == "failure":
                        logs = forge.fetch_workflow_job_logs(run_id=run["id"])
                        if logs:
                            log_text += (
                                f"\n--- {run.get('name', 'workflow')} "
                                f"(run {run['id']}) ---\n{logs}"
                            )
        except CodeScanningAlertsUnavailable:
            log.warning(
                "%s: code-scanning alerts unreadable (HTTP 403) — "
                "token lacks 'security-events' permission",
                ticket.id,
            )
            alerts_unreadable = True
            # Still try to fetch changed_paths and job logs — do not lose
            # log enrichment just because alerts are unreadable.
            try:
                forge = get_forge(s, repo_config=ctx.repo_config)
                changed_paths = _pr_changed_paths(forge, branch)
                pr = forge.pr_status(source_branch=branch)
                head_sha = (pr or {}).get("sha", "")
                if head_sha:
                    runs = forge.list_workflow_runs(head_sha=head_sha)
                    for run in runs:
                        if run.get("conclusion") == "failure":
                            logs = forge.fetch_workflow_job_logs(run_id=run["id"])
                            if logs:
                                log_text += (
                                    f"\n--- {run.get('name', 'workflow')} "
                                    f"(run {run['id']}) ---\n{logs}"
                                )
            except Exception:  # noqa: BLE001 — best-effort enrichment
                log.warning("%s: failed to fetch job logs", ticket.id)
        except Exception:  # noqa: BLE001 — best-effort enrichment
            log.warning("%s: failed to fetch job logs / alerts", ticket.id)

        return (
            _build_failing_summary(failing, log_text, alerts, changed_paths),
            alerts,
            changed_paths,
            alerts_unreadable,
            head_sha,
        )

    def _write_failing_summary_artifact(
        self,
        ctx: StageContext,
        ticket: Ticket,
        failing_summary: str,
        failing: list[dict[str, Any]],
    ) -> None:
        """Persist the failure detail to ``failing_summary.txt``.

        Best-effort: a write failure is logged, not raised.
        If *failing_summary* is empty, falls back to the raw check names
        so the file is never silently empty.
        """
        try:
            content = failing_summary.strip()
            if not content:
                names = [chk.get("name", "?") for chk in failing]
                content = f"(no detail available) failing checks: {', '.join(names)}"
            path = ctx.service.workspace(ticket).artifacts_dir / "failing_summary.txt"
            _write_text(path, content)
        except Exception:
            log.exception("%s: failed to write failing_summary.txt artifact", ticket.id)

    def _check_consecutive_identical_failure(
        self,
        ticket: Ticket,
        ctx: StageContext,
        failing_summary: str,
        head_sha: str = "",
    ) -> Outcome | None:
        """Return ``Outcome(State.BLOCKED, ...)`` when the same CI failure
        fingerprint has repeated ``ci_fix_max_identical_failures`` times in a
        row without the agent making progress, or ``None`` when the stage
        should proceed to the agent phase.

        The fingerprint is read/written from the artifacts dir.  A separate
        counter file tracks how many times the *current* fingerprint has been
        seen consecutively; it is reset to zero whenever the fingerprint
        changes (or on first run). The counter now reflects only genuine
        agent attempts on the same failure — nothing pre-seeds it before the
        agent runs.

        Short-circuits to ``None`` when ``ci_fix_max_identical_failures == 0``
        (disabled).
        """
        s = ctx.settings

        # Disabled short-circuit.
        if s.ci_fix_max_identical_failures == 0:
            return None

        repo_id = ctx.repo_config.board_id if ctx.repo_config else ""
        current_fp = _ci_failure_fingerprint(failing_summary, repo_id, head_sha)
        artifacts = ctx.service.workspace(ticket).artifacts_dir
        fp_path = artifacts / _CI_FAILURE_FINGERPRINT
        counter_path = artifacts / _CI_IDENTICAL_FAILURE_COUNT

        try:
            stored_fp = fp_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            stored_fp = ""

        if current_fp == stored_fp and stored_fp:
            # Same failure as last cycle — increment the consecutive counter.
            count = _read_counter(counter_path) + 1
            _write_counter(counter_path, count)
            if count >= s.ci_fix_max_identical_failures:
                return Outcome(
                    State.BLOCKED,
                    f"Same CI failure fingerprint ({current_fp}) repeated "
                    f"{count} consecutive times without progress — "
                    "escalating to BLOCKED. Resume to retry.",
                )
            return None

        # Fingerprint changed (or first run) — reset the counter and store
        # the new fingerprint for the next cycle's comparison.
        _write_counter(counter_path, 0)
        fp_path.parent.mkdir(parents=True, exist_ok=True)
        fp_path.write_text(current_fp, encoding="utf-8")
        return None

    def _try_dedup_changelog_fragments(  # noqa: C901 — multi-step dedup; each step is simple
        self,
        ticket: Ticket,
        ctx: StageContext,
        repo_dir: str,
        branch: str,
    ) -> Outcome | None:
        """Deduplicate towncrier changelog fragments for *ticket*.

        When the CI failure is "Duplicate changelog fragments detected for
        ticket(s): <id>", the PR branch carries two ``changes/<id>.<type>.md``
        files. This method removes the lower-priority fragment(s), merges any
        unique content into the kept fragment, commits, and pushes — all
        without invoking the LLM agent.

        Returns ``Outcome(State.IMPLEMENT_COMPLETE)`` on success, or ``None``
        when no duplicate fragments exist, the repo doesn't use towncrier, or
        any error occurs (so the LLM agent still gets a chance).
        """
        try:
            repo_path = Path(repo_dir)

            # 1. Read towncrier config from pyproject.toml.
            pp = repo_path / "pyproject.toml"
            if not pp.is_file():
                return None

            import tomllib

            data = tomllib.loads(pp.read_text(encoding="utf-8"))
            tc = (data.get("tool", {}) or {}).get("towncrier")
            if not tc:
                return None

            directory = str(tc.get("directory") or "changes")
            fragment_dir = repo_path / directory

            # 2. Glob fragments for this ticket id.
            if not fragment_dir.is_dir():
                return None
            fragments = list(fragment_dir.glob(f"{ticket.id}.*.md"))
            if len(fragments) <= 1:
                return None

            # 3. Choose the fragment to keep by priority.  Lower index =
            #    higher priority; unknown types rank below ``misc``.
            _PRIORITY_ORDER = [
                "feature",
                "bugfix",
                "removal",
                "security",
                "deprecation",
                "misc",
            ]

            def _type_from_path(p: Path) -> str:
                # Filename: <ticket_id>.<type>.md  →  type = parts[1]
                parts = p.name.split(".")
                return parts[1] if len(parts) >= 2 else ""

            def _priority(p: Path) -> int:
                t = _type_from_path(p)
                try:
                    return _PRIORITY_ORDER.index(t)
                except ValueError:
                    return len(_PRIORITY_ORDER)  # unknown → lowest priority

            fragments.sort(key=_priority)  # ascending: highest priority first
            keep = fragments[0]  # highest priority = first after ascending sort
            to_delete = fragments[1:]

            # 4. Merge content (conservative): append unique lines from each
            #    deleted fragment to the kept fragment.
            keep_content = keep.read_text(encoding="utf-8")
            for frag in to_delete:
                frag_content = frag.read_text(encoding="utf-8")
                if frag_content and frag_content not in keep_content:
                    keep_content = keep_content.rstrip("\n") + "\n" + frag_content
            if keep_content != keep.read_text(encoding="utf-8"):
                keep.write_text(keep_content, encoding="utf-8")

            # 5. Delete extra fragments.
            for frag in to_delete:
                frag.unlink()

            # 6. Commit.
            if git_ops.has_changes(repo_path):
                git_ops.commit_all(
                    repo_path,
                    f"ci: deduplicate changelog fragment for {ticket.id}",
                )

                # 7. Push.
                remote_url = _resolve_remote_url(ctx.settings, ctx.repo_config)
                token = github_push_token(ctx.settings, repo_config=ctx.repo_config)
                git_ops.push(repo_path, branch, remote_url, token)

            # 8. Success — no agent needed.
            log.info(
                "%s: deduplicated changelog fragments — kept %s, deleted %d extra(s)",
                ticket.id,
                keep.name,
                len(to_delete),
            )
            return Outcome(State.IMPLEMENT_COMPLETE)

        except Exception:
            log.warning(
                "%s: dedup changelog fragment failed — falling through to LLM agent",
                ticket.id,
                exc_info=True,
            )
            return None

    def _run_agent_and_finalize(
        self,
        ticket: Ticket,
        ctx: StageContext,
        repo_dir: str,
        branch: str,
        failing_summary: str,
        failing: list[dict[str, Any]],
        alerts: list[dict[str, Any]],
        changed_paths: set[str],
        head_sha: str = "",
    ) -> Outcome:
        """Reconcile, run the agent (which owns the loop), and route its verdict.

        The agent fixes, pushes, and verifies on real CI via wait_for_ci,
        iterating internally until CI is green (DONE) or its budget is spent
        (FAILED). There is no external retry loop here — DONE → re-poll,
        FAILED/crash → BLOCKED, OUT_OF_SCOPE → dependency fix.
        """
        s = ctx.settings

        # Reconcile with remote PR branch before running the agent so it
        # works from the latest remote state (includes any foreign commits).
        remote_url = _resolve_remote_url(s, ctx.repo_config)
        token = github_push_token(s, repo_config=ctx.repo_config)
        reconciled = git_ops.reconcile_with_remote_pr(
            Path(repo_dir), remote_url, branch, token
        )
        if reconciled is git_ops.ReconcileResult.DIVERGED:
            return Outcome(
                State.BLOCKED,
                "PR branch diverged from the workspace clone (a human likely pushed to "
                "it) — manual reconciliation required. The mill refuses to "
                "force-push here: push_with_lease cannot protect this case "
                "because reconcile's own fetch already advanced the tracking "
                "ref to the foreign commit, so a lease push would pass its "
                "compare-and-swap and SILENTLY OVERWRITE that commit.",
            )
        if reconciled is git_ops.ReconcileResult.UNAVAILABLE:
            log.warning(
                "%s: could not reach the remote PR branch to reconcile — "
                "proceeding; push_with_lease backstops a stale push",
                ticket.id,
            )

        result = self._invoke_agent(ticket, ctx, repo_dir, branch, failing_summary)

        # Write the per-cycle ci_fix.md artifact and an informative
        # history note (both best-effort) so the ticket history surfaces
        # what the agent saw and what it did.
        self._write_ci_fix_artifact(ctx, ticket, failing_summary, result)
        self._add_ci_fix_history_note(ctx, ticket, failing_summary, result)

        if result is not None and result.status == "DONE":
            return self._finalize_success(ticket, ctx, repo_dir, branch)

        if result is not None and result.status == "OUT_OF_SCOPE":
            return self._handle_out_of_scope(
                ticket, ctx, branch, result, failing_summary, head_sha
            )

        # FAILED, or None on crash — the agent could not turn CI green within
        # its iteration budget (or hit an unrecoverable error). Block so a
        # human can intervene; resume re-enters from human_mr_approval.
        #
        # Before emitting the generic message, check whether CodeQL code-
        # scanning is the blocker and, when it is, produce a specific note
        # naming every gating alert and explaining why the auto-solver
        # abstained.
        artifacts_dir = ctx.service.workspace(ticket).artifacts_dir
        verdicts: list[dict[str, Any]] | None = None
        try:
            import json as _json

            vp = artifacts_dir / _CODQL_FP_TRIAGE_VERDICTS
            if vp.exists():
                verdicts = _json.loads(vp.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001, S110 — best-effort; silent fallback
            pass

        codeql_note = _codeql_block_note(failing, alerts, changed_paths, verdicts)
        if codeql_note is not None:
            return Outcome(State.BLOCKED, codeql_note)

        return Outcome(
            State.BLOCKED,
            "ci fix agent could not turn CI green within its iteration budget "
            "— manual intervention required. "
            "Resume-blocked to retry from human_mr_approval.",
        )

    def _write_ci_fix_artifact(
        self,
        ctx: StageContext,
        ticket: Ticket,
        failing_summary: str,
        result: CiFixResult | None,
    ) -> None:
        """Write the per-cycle ``ci_fix.md`` artifact (single latest, overwrite).

        Includes the detected failure detail and, when the agent produced a
        result, a recap of what it did and its verdict.  Best-effort only.
        """
        try:
            parts: list[str] = []
            parts.append("# CI Fix Cycle\n")
            parts.append("## Detected Failure\n")
            parts.append(failing_summary.strip() or "(no detail available)")
            parts.append("\n")
            if result is not None:
                parts.append("## Agent Recap\n")
                parts.append(f"**Verdict:** {result.status}\n")
                if result.summary:
                    parts.append(result.summary)
            else:
                parts.append("## Agent Recap\n")
                parts.append("The ci-fix agent crashed before producing a result.")
            path = ctx.service.workspace(ticket).artifacts_dir / "ci_fix.md"
            _write_text(path, "\n".join(parts))
        except Exception:
            log.exception("%s: failed to write ci_fix.md artifact", ticket.id)

    def _add_ci_fix_history_note(
        self,
        ctx: StageContext,
        ticket: Ticket,
        failing_summary: str,
        result: CiFixResult | None,
    ) -> None:
        """Record one informative history note per ci-fix cycle.

        Contains the detected failure detail and the agent's recap.
        Best-effort: a failure to write the note is logged, not raised.
        """
        try:
            lines: list[str] = []
            lines.append("**CI Fix Cycle**\n")
            lines.append("### Detected Failure\n")
            lines.append(failing_summary.strip() or "(no detail available)")
            if result is not None:
                lines.append("\n### Agent Result\n")
                lines.append(f"**Verdict:** {result.status}")
                if result.summary:
                    lines.append(result.summary)
            else:
                lines.append("\n### Agent Result\n")
                lines.append("The ci-fix agent crashed before producing a result.")
            ctx.service.add_history_note(ticket.id, "\n".join(lines))
        except Exception:
            log.exception("%s: failed to write ci-fix history note", ticket.id)

    def _invoke_agent(
        self,
        ticket: Ticket,
        ctx: StageContext,
        repo_dir: str,
        branch: str,
        failing_summary: str,
    ) -> CiFixResult | None:
        """Run the ci-fix agent inside the ticket span.

        Returns the full :class:`CiFixResult` so the caller can route on
        the agent's status (DONE / FAILED / OUT_OF_SCOPE), or ``None`` when
        the agent crashes (treated as a failure by the caller).
        """
        s = ctx.settings
        try:
            # ci_fix is traced=False, so wrap the LLM agent in the
            # ticket's Langfuse session (session.id = ticket.id) — same
            # reason as the rebase agent: keep its cost/traces attributed
            # to the ticket instead of an orphan root trace.
            with tracing.start_ticket_root_span(ticket.id, "ci_fix"):
                ci_fix_memory_path = s.memory_file_for(
                    "ci_fix", ctx.memory_board_id(ticket)
                )
                memory_text = load_memory(
                    ci_fix_memory_path, max_chars=s.max_memory_chars
                )

                # Pass the per-repo remote_url and token so the agent's
                # bridged git tools can drive fetch + push host-side.
                # The token is captured in the closure and NEVER exposed
                # to the sandbox or the agent's prompt.
                remote_url = _resolve_remote_url(s, ctx.repo_config)
                token = github_push_token(s, repo_config=ctx.repo_config)
                target = target_branch_for(s, ctx.repo_config)

                result = run_ci_fix_agent(
                    settings=s,
                    repo_dir=repo_dir,
                    branch=branch,
                    failing_summary=failing_summary,
                    memory=memory_text,
                    ticket_id=ticket.id,
                    board_id=ctx.repo_config.board_id if ctx.repo_config else "",
                    target=target,
                    remote_url=remote_url,
                    token=token,
                    ci_status_fn=self._make_ci_status_fn(ticket, ctx, branch),
                    ci_log_fetch_fn=self._make_ci_log_fetch_fn(ctx, branch),
                )
                if result.updated_memory:
                    persist_memory(ci_fix_memory_path, result.updated_memory)
        except Exception as e:  # noqa: BLE001
            log.exception("%s: ci-fix agent crashed: %s", ticket.id, e)
            return None
        return result

    def _make_ci_status_fn(
        self, ticket: Ticket, ctx: StageContext, branch: str
    ) -> "Callable[[], tuple[str, str]]":
        """Build the host-side forge probe the agent's wait_for_ci tool calls.

        Returns a closure that fetches the branch's CI conclusion and returns
        ``(conclusion, failing_summary)`` where conclusion is one of
        ``success`` / ``failure`` / ``pending`` / ``gone``. On a fresh failure
        it builds the enriched failing summary (job logs + code-scanning
        alerts) so the agent gets actionable detail for its next iteration.
        Transient forge errors map to ``pending`` so the agent keeps waiting
        rather than giving up on a blip.

        The closure includes a **120 s grace period** (measured from closure
        creation via ``time.monotonic()``).  During that window ANY
        ``"success"`` verdict is downgraded to ``"pending"`` so the agent
        keeps waiting — this prevents a race where GitHub's check-runs
        endpoint returns no runs (or stale runs from a prior commit) for a
        freshly-pushed SHA and the no-CI fast-path (or a stale green) would
        otherwise produce a false ``CI_PASSED``.
        """
        import time

        s = ctx.settings
        _created_at = time.monotonic()
        _grace_s = 120.0

        def status_fn() -> tuple[str, str]:
            in_grace = (time.monotonic() - _created_at) < _grace_s

            try:
                status = get_forge(s, repo_config=ctx.repo_config).check_status(
                    source_branch=branch, require_checks=True
                )
            except Exception:  # noqa: BLE001 — transient; keep waiting
                log.warning(
                    "%s: check_status failed during CI wait — treating as pending",
                    ticket.id,
                )
                return ("pending", "")
            if status is None:
                return ("gone", "")

            conclusion = status.get("conclusion")
            sha = status.get("_sha", "")

            # During the grace period never trust "success" — check runs
            # may not be registered yet, or may be stale from a prior
            # commit.  Keep waiting.
            if conclusion == "success" and in_grace:
                return ("pending", "")

            if conclusion == "success":
                return ("success", f"CI green at {sha[:7]}" if sha else "")

            if conclusion == "failure":
                failing = status.get("failing", [])
                summary, _alerts, _changed, _unreadable, _head = (
                    self._build_failure_detail(ticket, ctx, branch, failing)
                )
                if sha:
                    summary = f"[sha: {sha[:7]}]\n{summary}"
                return ("failure", summary)
            # pending / None / unknown — not terminal yet.
            return ("pending", "")

        return status_fn

    def _make_ci_log_fetch_fn(
        self, ctx: StageContext, branch: str
    ) -> "Callable[[int, bool], str]":
        """Build the host-side forge probe the agent's ``fetch_ci_logs`` tool calls.

        Returns a closure that calls ``forge.fetch_workflow_job_logs()`` for
        a given run id and *full_log* flag, returning the log text.  Transient
        forge errors raise through to the tool's error handler.
        """
        s = ctx.settings

        def fetch_fn(run_id: int, full_log: bool) -> str:
            forge = get_forge(s, repo_config=ctx.repo_config)
            return forge.fetch_workflow_job_logs(run_id=run_id, full_log=full_log)

        return fetch_fn

    def _handle_out_of_scope(
        self,
        ticket: Ticket,
        ctx: StageContext,
        branch: str,
        result: CiFixResult,
        failing_summary: str,
        head_sha: str = "",
    ) -> Outcome:
        """Route an out-of-scope CI failure to a dedicated fix ticket.

        Before spawning, detect a *stale* branch (one behind its base, where
        the failure may already be fixed on main) and refresh it once via the
        forge's server-side update-branch primitive instead of spawning a
        dependency fix. Otherwise delegates the spawn-or-reuse + wire + park
        logic to :func:`~.dependency_fix.spawn_dependency_fix`, which is shared
        with the implement-stage baseline check (and, later, verify /
        review / merge).
        """
        s = ctx.settings

        # Deterministic in-diff guard: the LLM's OUT_OF_SCOPE verdict must not
        # be the only safety net. If ANY open code-scanning alert lives in this
        # PR's own diff, the verdict is wrong for at least those — do NOT spawn
        # a dependency fixer; route back to re-run the agent against the
        # in-scope-labelled summary instead.
        in_scope_alerts, out_of_scope_alerts = _partition_open_alerts(ctx, branch)

        if in_scope_alerts:
            # OUT_OF_SCOPE is wrong for these alerts — suppress the spawn and
            # re-poll so the ci-fix agent re-runs against the in-scope-labelled
            # failing_summary. The agent's own wait_for_ci iteration budget
            # bounds an agent that keeps refusing, so the loop stays safe.
            try:
                ctx.service.add_history_note(
                    ticket.id,
                    "ci-fix suppressed out-of-scope spawn: the following CodeQL "
                    "alert(s) are located in THIS PR's own changed files and "
                    "must be fixed in-scope: " + _format_alert_refs(in_scope_alerts),
                )
            except Exception:  # noqa: BLE001 — history note is best-effort
                log.warning("%s: failed to record in-scope-alert note", ticket.id)
            return Outcome(State.IMPLEMENT_COMPLETE)

        artifacts_dir = ctx.service.workspace(ticket).artifacts_dir
        refresh_path = artifacts_dir / _CI_REFRESH_COUNTER

        # Stale-branch backstop: when this branch is behind its base, the
        # failure may already be fixed on main (a fast-moving main races the
        # ci-fix agent). Refresh the branch once via the forge's server-side
        # update-branch and re-poll CI instead of spawning a dependency fix.
        # Use the forge's server-side "behind" signal (NOT the local-clone
        # branch_is_behind_main, which never advances after a server-side
        # refresh and would loop forever).
        if _read_counter(refresh_path) == 0:
            try:
                pr = get_forge(s, repo_config=ctx.repo_config).pr_status(
                    source_branch=branch
                )
            except Exception:  # noqa: BLE001 — treat as not-behind, fall through
                pr = None
            if (pr or {}).get("mergeable_state") == "behind":
                res = get_forge(s, repo_config=ctx.repo_config).update_branch(
                    source_branch=branch
                )
                if res.get("updated") or res.get("reason") == "already up to date":
                    _write_counter(refresh_path, 1)
                    try:
                        ctx.service.add_history_note(
                            ticket.id,
                            "branch was stale — refreshed via forge "
                            "update-branch before classifying out-of-scope; "
                            "re-running CI",
                        )
                    except Exception:  # noqa: BLE001 — history note is best-effort
                        log.warning(
                            "%s: failed to record branch-refresh note", ticket.id
                        )
                    return Outcome(State.IMPLEMENT_COMPLETE)
                # update_branch failed (PR not found / HTTP error) — fall
                # through to the normal spawn path so we don't get stuck.

        # Deterministic title so the spawn is idempotent across cycles.
        title = (
            f"ci_fix: out-of-scope CI failure — "
            f"{result.failing_check} in {result.required_change_area}"
        )
        description = (
            f"## Out-of-scope CI failure routed from {ticket.id}\n\n"
            f"**Failing check:** {result.failing_check}\n\n"
            f"**Required change area:** {result.required_change_area}\n\n"
            f"**Why out of scope:** {result.out_of_scope_reason}\n"
        )
        if out_of_scope_alerts:
            # Name the specific out-of-scope rule ids + paths so the dependency
            # fixer knows exactly which alerts to address (AC3).
            description += (
                "\n**Out-of-scope code-scanning alert(s):** "
                f"{_format_alert_refs(out_of_scope_alerts)}\n"
            )
        block_reason = "CI failure is out of scope for this ticket"

        fingerprint = _ci_failure_fingerprint(
            failing_summary,
            ctx.repo_config.board_id if ctx.repo_config else "",
            head_sha,
        )
        outcome = dependency_fix.spawn_dependency_fix(
            ticket,
            ctx,
            title=title,
            description=description,
            source_kind=SourceKind.CI_FIX_DEPENDENCY,
            block_reason_prefix=block_reason,
            priority=ticket.priority,
            dedup_labels=[f"ci_fp:{fingerprint}"],
        )

        # Clear the depends-on relationship that spawn_dependency_fix set
        # on the original ticket.  The unblocks relationship on the fix
        # ticket is sufficient — when the fix completes it auto-unblocks
        # this ticket.  Leaving depends_on set would block the operator's
        # resume-blocked: the dependency check in _process_ticket_inner
        # short-circuits before the ci_fix stage ever runs, parking the
        # ticket in FIXING_CI indefinitely.
        ctx.service.set_depends_on(ticket.id, [])

        # Reset the per-ticket refresh counter so a later re-entry (after
        # auto-unblock + a fresh pipeline pass) starts clean.
        _write_counter(artifacts_dir / _CI_REFRESH_COUNTER, 0)

        return outcome

    def _finalize_success(
        self,
        ticket: Ticket,
        ctx: StageContext,
        repo_dir: str,
        branch: str,
    ) -> Outcome:
        """On agent DONE: deterministically verify the agent's push landed
        and clobbered no foreign commits, then return to IMPLEMENT_COMPLETE so
        the merge stage re-verifies CI and promotes to HUMAN_MR_APPROVAL.

        The agent already confirmed CI green via wait_for_ci, so this is a
        cheap safety net (foreign-push / lost-push detection), not a retry
        loop. On a clean landing the refresh counter is reset so a later,
        independent staleness can rebase once more.
        """
        s = ctx.settings
        remote_url = _resolve_remote_url(s, ctx.repo_config)
        token = github_push_token(s, repo_config=ctx.repo_config)
        target = target_branch_for(s, ctx.repo_config)

        # Deterministic post-check: verify the agent's push actually
        # landed and no foreign commits were clobbered.
        check = git_ops.post_push_check(
            Path(repo_dir),
            branch=branch,
            target=target,
            remote_url=remote_url,
            token=token,
        )

        if check is git_ops.PostPushResult.PASS:
            # Genuine forward progress — allow a future staleness to refresh again.
            _write_counter(
                ctx.service.workspace(ticket).artifacts_dir / _CI_REFRESH_COUNTER, 0
            )
            log.info("%s: ci fix reported DONE, push verified", ticket.id)
            return Outcome(State.IMPLEMENT_COMPLETE)

        if check is git_ops.PostPushResult.NOT_LANDED:
            log.warning(
                "%s: ci-fix post-check failed — remote HEAD != local HEAD; "
                "push did not land",
                ticket.id,
            )
            return Outcome(
                State.BLOCKED,
                "ci fix agent reported DONE but the push did not land "
                "(remote HEAD != local HEAD). The agent may have hit a "
                "lease rejection it could not recover from. "
                "Resume-blocked to retry from human_mr_approval.",
            )

        if check is git_ops.PostPushResult.FOREIGN_DIVERGENCE:
            log.warning(
                "%s: ci-fix post-check failed — remote branch carries "
                "foreign-authored commits; a human may have pushed",
                ticket.id,
            )
            return Outcome(
                State.BLOCKED,
                "ci fix agent reported DONE but the remote branch carries "
                "foreign-authored commits — a human likely pushed to the PR "
                "branch. Manual reconciliation required. "
                "Resume-blocked to retry from human_mr_approval.",
            )

        # UNAVAILABLE — transient fetch failure, re-poll.
        log.warning(
            "%s: ci-fix post-check unavailable (fetch failed) — re-polling",
            ticket.id,
        )
        return Outcome(State.IMPLEMENT_COMPLETE)
