"""Baseline gate mixin: prerequisite gate and test-baseline check.

Split out of :mod:`.validation`; :class:`~.validation.ValidationMixin`
inherits :class:`BaselineGateMixin` so the methods stay on the assembled
:class:`~.core.ImplementStage` MRO.
"""

from __future__ import annotations

import json
from pathlib import Path

from ...agents import prerequisite
from ...agents.testing import (
    is_network_dependent_failure,
    is_resource_exhaustion_failure,
)
from ...config import Settings, target_branch_for
from ...core.models import SourceKind, Ticket
from ...core.states import State
from ...scm.forge.base import get_forge
from ...scm.vcs import git_ops
from .. import dependency_fix
from ..base import Outcome, StageContext
from ._base import _ImplementStageBase
from ._shared import _should_skip_test_gate, log


def classify_baseline_verdict(
    ci_conclusion: str
    | None,  # forge commit_ci_conclusion(...)["conclusion"], or None when unavailable
    network_dependent: bool,  # is_network_dependent_failure(out)
    resource_exhausted: bool = False,  # is_resource_exhaustion_failure(out)
) -> str:
    """Pure decision helper: should the baseline gate proceed or block?

    Returns ``"proceed"`` (sandbox failure is an environment artifact) or
    ``"block"`` (real main breakage or indeterminate without network
    signature).

    Decision table:
    - sandbox run OOM-killed (*resource_exhausted*) → ``"proceed"`` (always):
      the sandbox memory cap says nothing about the base commit, and any
      other check on the commit (image publish, scan) being red is not a
      pre-existing TEST failure either — blocking here only manufactures a
      bogus baseline-fix ticket (2026-09-08: fe7b/#3188 while main was red
      on an unrelated sandbox-image build).
    - CI green  → ``"proceed"`` (always).
    - CI red    → ``"block"`` (real breakage).
    - CI unknown (``None`` / ``"pending"``):
      ``"proceed"`` iff *network_dependent*, else ``"block"``.
    """
    if resource_exhausted:
        return "proceed"
    if ci_conclusion == "success":
        return "proceed"
    if ci_conclusion == "failure":
        return "block"
    # CI unavailable or pending: proceed only when the failure looks
    # network-dependent (sandbox artifact), otherwise block.
    return "proceed" if network_dependent else "block"


class BaselineGateMixin(_ImplementStageBase):
    """Pre-agent prerequisite gate and base-branch test check."""

    @classmethod
    def _run_prerequisite_gate(
        cls,
        ctx: StageContext,
        ticket: Ticket,
        spec: str,
        repo_dir: Path,
        s,
    ) -> Outcome | None:
        """Deterministic pre-agent gate for external prerequisites.

        Verifies that symbol/import prerequisites the spec declares in a
        ````prereq```` block are satisfiable in the cloned repo's
        environment before the expensive coordinator agent runs.  This
        is the cheapest gate (regex parse + sandboxed check), so it
        runs first.

        No-op (returns ``None``) when ``prerequisite_gate_enabled`` is
        False.  When a declared prerequisite is unmet the ticket is
        BLOCKED — the work is still required once the upstream symbol
        lands (unlike the freshness gate, which routes stale findings to
        DONE).  Best-effort: any checker error logs a warning and
        proceeds (returns ``None``) rather than blocking.
        """
        if not s.prerequisite_gate_enabled:
            return None

        try:
            result = prerequisite.run_prerequisite_check(
                spec,
                repo_dir,
                settings=s,
                sandbox_image=ctx.repo_config.sandbox_image
                if ctx.repo_config
                else None,
            )
        except Exception:
            log.warning(
                "%s: prerequisite check failed, proceeding with implement",
                ticket.id,
                exc_info=True,
            )
            return None

        unmet = result.get("unmet") or []
        if unmet:
            joined = ", ".join(unmet)
            log.info(
                "%s: prerequisite gate blocked — unmet: %s",
                ticket.id,
                joined,
            )
            return Outcome(
                State.BLOCKED,
                f"prerequisite(s) not met: {joined}. Re-run implement "
                "(resume-blocked) once the prerequisite is available.",
            )
        return None

    # ------------------------------------------------------------------
    # test-baseline check
    # ------------------------------------------------------------------

    @classmethod
    def _run_baseline_check(
        cls,
        ctx: StageContext,
        ticket: Ticket,
        repo_dir: Path,
        branch: str,
        resuming: bool,
        settings,
    ) -> Outcome | None:
        """Run the test gate on the base branch BEFORE the agent loop.

        Returns ``Outcome`` to short-circuit (BLOCKED), or ``None`` to
        proceed.  The result is cached at ``artifacts/baseline_check.json``
        keyed by base-branch SHA so retries don't re-execute.
        """
        ws = ctx.service.workspace(ticket)
        cache_path = ws.artifacts_dir / "baseline_check.json"
        target = target_branch_for(settings, ctx.repo_config)

        # Resolve the current base-branch SHA. Prefer the remote ref
        # (origin/<branch>) — the local branch may be stale, and we must test
        # the SAME commit we report as base_sha (see checkout below).
        remote_sha = git_ops.remote_branch_sha(repo_dir, target)
        base_sha = remote_sha or git_ops.head_sha(repo_dir)

        # --- idempotency guard (per ticket, per base commit) ---
        # If a baseline-fix this ticket already depends on has completed for
        # THIS base_sha (same title), the gate is satisfied — re-running it
        # would re-spawn a duplicate fix (the prior DONE fix is invisible to
        # spawn_dependency_fix's open-only dedup), wedging the ticket in an
        # operator-only re-spawn cycle. Placed before the cache read so it
        # covers BOTH the cache-hit-failing and fresh-fail paths and avoids
        # re-running the test agent on re-entry. Proceed instead; any genuine
        # residual failure is caught downstream as a normal gate result.
        fix_title = cls._baseline_fix_title(settings, base_sha, target)
        resolved_fix_id = cls._baseline_fix_already_resolved(ctx, ticket, fix_title)
        if resolved_fix_id is not None:
            try:
                ctx.service.add_history_note(
                    ticket.id,
                    f"baseline gate already satisfied by completed fix "
                    f"{resolved_fix_id} for base {base_sha[:8]} — proceeding.",
                )
            except Exception:
                log.warning(
                    "%s: failed to record baseline-gate-satisfied note",
                    ticket.id,
                )
            return None

        # --- cache lookup ---
        if cache_path.exists():
            try:
                cache = json.loads(cache_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError, OSError:
                cache = None
            if isinstance(cache, dict):
                cached_sha = cache.get("base_sha")
                cached_passed = cache.get("passed")
                if cached_sha == base_sha:
                    # Same base commit → reuse cached result.
                    if cached_passed:
                        return None
                    diag = cache.get("diagnosis", "pre-existing test failures")
                    return cls._spawn_baseline_fix(
                        ctx, ticket, diag, base_sha, settings
                    )
                if cached_passed:
                    # Base advanced but cached result was passing — a
                    # passing baseline stays valid (AC7).
                    return None
                # Base advanced AND cached result was failing → re-run
                # (operator may have fixed the branch between retries).

        # --- execute baseline check ---
        # Check out the EXACT base commit (origin/<branch>), not the local
        # branch ref — the clone's local main is often stale, which made the
        # baseline run old code while labelling it with the fresh remote SHA,
        # producing phantom "pre-existing failures on main" (e.g. a fix that
        # already landed reported as still-broken) that poison the gate. When
        # the remote branch is absent, fall back to the branch name.
        git_ops.checkout(repo_dir, remote_sha or target)
        try:
            # retry_on_failure: one flaky test on main must not fabricate
            # "pre-existing test failures", block the ticket, and spawn a
            # bogus dependency-fix ticket — re-run once before believing it.
            from robotsix_mill.stages import implement as _facade

            ticket_summary = (getattr(ticket, "title", "") or "")[:200]
            skip, skip_diag = _should_skip_test_gate(
                repo_dir, target, settings, ticket_summary
            )
            if skip:
                passed, diag = True, skip_diag
            else:
                passed, diag = _facade.run_test_agent(
                    settings=settings,
                    repo_dir=repo_dir,
                    repo_config=ctx.repo_config,
                    retry_on_failure=True,
                )
        finally:
            git_ops.checkout(repo_dir, branch)

        # A "sandbox unavailable" baseline result means the sandbox container
        # could not be launched (docker run failed — e.g. the docker-py
        # auto-remove race that surfaces as "No such container"):
        # infrastructure, not a pre-existing failure on the base. Raise it as
        # a transient SandboxError so the worker retries with backoff instead
        # of caching a bogus "passed: False", blocking the ticket, and
        # spawning a phantom baseline-fix ticket.
        if not passed and diag.startswith("sandbox unavailable"):
            from ...sandbox import SandboxError

            raise SandboxError(diag)

        cache_data: dict[str, object] = {
            "passed": passed,
            "diagnosis": diag,
            "base_sha": base_sha,
        }
        cache_path.write_text(json.dumps(cache_data, indent=2), encoding="utf-8")

        if passed:
            return None

        # --- baseline CI cross-check ---
        # The sandbox blocks egress, so network-dependent tests fail even
        # when main is green on GitHub CI. Cross-check the forge's commit-
        # CI status to avoid false-blocking every ticket on a repo whose
        # main is actually green.
        try:
            forge = get_forge(settings, repo_config=ctx.repo_config)
            status = forge.commit_ci_conclusion(sha=base_sha)
        except Exception:
            log.debug(
                "%s: forge.commit_ci_conclusion raised — treating CI as unavailable",
                ticket.id,
                exc_info=True,
            )
            status = None
        ci_conclusion = status.get("conclusion") if status else None
        network_dependent = is_network_dependent_failure(diag)
        resource_exhausted = is_resource_exhaustion_failure(diag)
        verdict = classify_baseline_verdict(
            ci_conclusion, network_dependent, resource_exhausted=resource_exhausted
        )

        if verdict == "proceed":
            # Sandbox artifact — record a warning and proceed.
            names_or_diag = diag[:300]
            if resource_exhausted:
                warning_msg = (
                    f"baseline suite on {base_sha[:8]} was killed by the "
                    f"sandbox memory cap (exit 137 / OOM) — not a pre-existing "
                    f"test failure; proceeding. Raise `sandbox_memory` or narrow "
                    f"`test_command` for this repo: {names_or_diag}"
                )
            elif ci_conclusion == "success":
                warning_msg = (
                    f"sandbox suite failed but GitHub CI on {base_sha[:8]} "
                    f"is green — proceeding; failing tests likely "
                    f"network-dependent: {names_or_diag}"
                )
            else:
                warning_msg = (
                    f"CI status unavailable for {base_sha[:8]}; sandbox "
                    f"failure matched network-error signature — proceeding; "
                    f"failing tests: {names_or_diag}"
                )
            log.warning("%s: %s", ticket.id, warning_msg)
            try:
                ctx.service.add_history_note(ticket.id, warning_msg)
            except Exception:
                log.warning(
                    "%s: failed to record baseline-warning history note",
                    ticket.id,
                )
            # Overwrite the baseline cache as passing so retries don't
            # re-run and re-block.
            cache_data = {
                "passed": True,
                "diagnosis": warning_msg,
                "base_sha": base_sha,
            }
            cache_path.write_text(json.dumps(cache_data, indent=2), encoding="utf-8")
            return None

        # Write the implement.md artifact so the blocked ticket has a
        # matching diagnostic (AC8 / existing BLOCKED pattern).
        # Don't persist a spec fingerprint — the baseline check is a
        # pre-condition, not a real implement attempt.  Writing one
        # would cause the stale-re-spawn guard in preflight to block
        # every resume even though no implement agent has run yet.
        cls._finalize(
            ctx,
            ticket,
            repo_dir,
            branch,
            f"pre-existing test failures on {target} ({base_sha[:8]}): {diag[:400]}",
            ok=False,
            extra_roots=None,
            write_spec_fingerprint=False,
        )
        return cls._spawn_baseline_fix(ctx, ticket, diag, base_sha, settings)

    @classmethod
    def _baseline_fix_title(cls, settings, base_sha: str, target_branch: str) -> str:
        """Deterministic title for the baseline-fix ticket of *base_sha*.

        Shared by :meth:`_run_baseline_check` (idempotency guard) and
        :meth:`_spawn_baseline_fix` (spawn/dedup) so the two cannot drift.
        """
        return f"baseline: pre-existing test failures — {target_branch} {base_sha[:8]}"

    @classmethod
    def _baseline_fix_already_resolved(cls, ctx, ticket, fix_title) -> str | None:
        """Return the id of an already-completed baseline-fix this ticket
        depends on (same title => same base_sha), else None.
        """
        for dep_id in ctx.service._parse_depends_on(ticket):
            dep = ctx.service.get(dep_id)
            if (
                dep is not None
                and dep.source == SourceKind.IMPLEMENT_BASELINE_DEPENDENCY
                and dep.title == fix_title
                and dep.state in (State.DONE, State.CLOSED)
            ):
                return dep.id
        return None

    @classmethod
    def _spawn_baseline_fix(
        cls,
        ctx: StageContext,
        ticket: Ticket,
        diag: str,
        base_sha: str,
        settings: Settings,
    ) -> Outcome:
        """Spawn (or reuse) a fix ticket for pre-existing baseline failures.

        Uses the shared :func:`~.dependency_fix.spawn_dependency_fix`
        helper so the current ticket auto-resumes when the fix reaches
        DONE instead of dead-ending on ``BLOCKED``.
        """
        target = target_branch_for(settings, ctx.repo_config)
        block_reason = (
            f"pre-existing test failures on {target} ({base_sha[:8]}): {diag[:400]}"
        )
        title = cls._baseline_fix_title(settings, base_sha, target)
        description = (
            f"## Pre-existing test failures on {target}\n\n"
            f"**Base SHA:** {base_sha}\n\n"
            f"**Diagnosis:** {diag}\n\n"
            f"**Detected by:** implement baseline check for {ticket.id}\n"
        )
        return dependency_fix.spawn_dependency_fix(
            ticket,
            ctx,
            title=title,
            description=description,
            source_kind=SourceKind.IMPLEMENT_BASELINE_DEPENDENCY,
            block_reason_prefix=block_reason,
            priority=ticket.priority,
        )
