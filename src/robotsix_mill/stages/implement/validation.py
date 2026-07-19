"""Validation mixin: prerequisite gate, baseline check, scope guardrail."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ...agents import prerequisite
from ...agents.testing import is_network_dependent_failure
from ...config import Settings, target_branch_for
from ...core.models import SourceKind, Ticket
from ...core.states import State
from ...forge.base import get_forge
from ...vcs import git_ops
from .. import dependency_fix
from ..base import Outcome, StageContext
from ._base import _ImplementStageBase
from ._shared import (
    MODULES_YAML,
    _BACKTICK_RE,
    _FLOOD_SAMPLE_SIZE,
    _ScopeGuardrailResult,
    _is_binary_artifact,
    _modules_yaml_added_paths,
    _should_skip_test_gate,
    _vendored_dep_roots,
    log,
)


def classify_baseline_verdict(
    ci_conclusion: str
    | None,  # forge commit_ci_conclusion(...)["conclusion"], or None when unavailable
    network_dependent: bool,  # is_network_dependent_failure(out)
) -> str:
    """Pure decision helper: should the baseline gate proceed or block?

    Returns ``"proceed"`` (sandbox failure is an environment artifact) or
    ``"block"`` (real main breakage or indeterminate without network
    signature).

    Decision table:
    - CI green  → ``"proceed"`` (always).
    - CI red    → ``"block"`` (real breakage).
    - CI unknown (``None`` / ``"pending"``):
      ``"proceed"`` iff *network_dependent*, else ``"block"``.
    """
    if ci_conclusion == "success":
        return "proceed"
    if ci_conclusion == "failure":
        return "block"
    # CI unavailable or pending: proceed only when the failure looks
    # network-dependent (sandbox artifact), otherwise block.
    return "proceed" if network_dependent else "block"


class ValidationMixin(_ImplementStageBase):
    """Pre-agent gating and scope enforcement for :class:`ImplementStage`."""

    @classmethod
    def _run_scope_guardrail(
        cls,
        ctx: StageContext,
        ticket: Ticket,
        repo_dir: Path,
        branch: str,
        summary: str,
        ref_files: list[str] | None,
        file_map: set[str] | None,
        settings,
        spec: str,
        current_feedback: str | None,
    ) -> _ScopeGuardrailResult:
        """Check every changed file against the ticket's file_map.

        When ``scope_triage_enabled`` is True an LLM classifier
        decides whether out-of-scope changes are legitimate expansions,
        scope creep (REJECT), or ambiguous (ESCALATE).  Otherwise any
        out-of-scope file immediately blocks the ticket.
        """
        target = target_branch_for(settings, ctx.repo_config)
        if not file_map:
            return _ScopeGuardrailResult(
                action="skip_iteration",
                file_map=file_map,
                feedback=current_feedback,
            )

        changed = git_ops.introduced_files(repo_dir, target)
        # Directory entries in the file_map (trailing "/") cover every
        # file under them. Without this, a map entry like ".deps/" never
        # matches the individual ".deps/<pkg>/..." paths and a directory
        # removal floods the scope check (live case: auto-mail 6624, 118
        # "out-of-scope" files that WERE the ticket's deliverable).
        dir_prefixes = tuple(e for e in file_map if e.endswith("/"))
        out_of_scope = [
            f
            for f in changed
            if f not in file_map and not f.startswith(dir_prefixes or ("\0",))
        ]
        if not out_of_scope:
            log.info(
                "%s: scope check passed — %d file(s) changed, "
                "all in file_map (%d allowed)",
                ticket.id,
                len(changed),
                len(file_map),
            )
            return _ScopeGuardrailResult(
                action="skip_iteration",
                file_map=file_map,
                feedback=current_feedback,
            )

        # --- deterministic auto-EXPAND for docs/modules.yaml re-pathing ---
        # A package-split/move refactor MUST re-path the moved files in
        # docs/modules.yaml (CI enforces it). When that diff only re-paths
        # entries whose target files are ALREADY in the file_map, the edit
        # is a mechanical consequence of an in-scope move — auto-add it so
        # we neither ESCALATE nor pay for an LLM round-trip. When the diff
        # registers NEW paths that are NOT in the file_map (an unrelated
        # new module), leave it out_of_scope so it is still flagged.
        if MODULES_YAML in out_of_scope:
            added = _modules_yaml_added_paths(repo_dir, target)
            if added and added <= file_map:
                file_map.add(MODULES_YAML)
                out_of_scope = [f for f in out_of_scope if f != MODULES_YAML]
                log.info(
                    "%s: auto-EXPAND docs/modules.yaml — re-paths only "
                    "in-scope modules (%d added path(s), all in file_map)",
                    ticket.id,
                    len(added),
                )
                ctx.service.add_step_event(
                    ticket.id,
                    "scope-triage auto-EXPAND: `docs/modules.yaml` re-paths "
                    "only in-scope modules — registry sync, not scope creep",
                )
                if not out_of_scope:
                    return _ScopeGuardrailResult(
                        action="skip_iteration",
                        file_map=file_map,
                        feedback=current_feedback,
                    )
            # When ``added`` is empty (e.g. a deletion-only diff that
            # removes paths but adds none), do NOT auto-EXPAND — let it
            # fall through to the existing scope-triage LLM, which is no
            # worse than today.

        log.warning(
            "%s: scope violation — %d out-of-scope file(s): %s",
            ticket.id,
            len(out_of_scope),
            ", ".join(out_of_scope),
        )

        # --- binary-artifact auto-cleanup ---
        out_of_scope, binary_skip = cls._clean_binary_artifacts(
            ctx, ticket, repo_dir, target, out_of_scope, file_map, current_feedback
        )
        if binary_skip is not None:
            return binary_skip

        # --- vendored-dep install-directory auto-exclusion ---
        out_of_scope, vendor_skip = cls._filter_vendored_deps(
            ctx, ticket, repo_dir, target, out_of_scope, file_map, current_feedback
        )
        if vendor_skip is not None:
            return vendor_skip

        # --- flood guard (deterministic prompt-size cap) ---
        # When an implement pass leaves an abnormally large out-of-scope
        # TEXT tree (a build-artifact flood the binary detector misses —
        # node_modules/, dist/, generated sources, sourcemaps, minified
        # JS/CSS), do NOT build per-file diffs or call the scope-triage
        # LLM: its prompt would balloon to thousands of diff summaries,
        # overflow the context window, and fall through to auto-ESCALATE.
        # Skip the LLM entirely (zero tokens) and BLOCK deterministically
        # for human review. Fires before the scope_triage_enabled check so
        # the disabled path (which also joins the whole list) can't flood.
        # Non-destructive — files are left for human inspection (matches
        # the ESCALATE path).
        if (
            settings.scope_triage_max_files
            and len(out_of_scope) > settings.scope_triage_max_files
        ):
            ordered = sorted(out_of_scope)
            sample = ordered[:_FLOOD_SAMPLE_SIZE]
            sample_str = ", ".join(f"`{f}`" for f in sample)
            remaining = len(ordered) - len(sample)
            if remaining > 0:
                sample_str += f", … (+{remaining} more)"
            message = (
                f"scope-triage flood guard: {len(out_of_scope)} out-of-scope "
                f"text file(s) exceed cap of {settings.scope_triage_max_files} "
                f"— likely a build-artifact flood. Skipped the scope-triage "
                f"LLM and BLOCKED for human review. Sample: {sample_str}"
            )
            log.warning("%s: %s", ticket.id, message)
            ctx.service.add_step_event(ticket.id, message)
            cls._finalize(
                ctx,
                ticket,
                repo_dir,
                branch,
                message,
                ok=False,
                reference_files=ref_files,
                extra_roots=None,
            )
            return _ScopeGuardrailResult(
                action="return",
                outcome=Outcome(State.BLOCKED, message),
            )

        if not settings.scope_triage_enabled:
            cls._finalize(
                ctx,
                ticket,
                repo_dir,
                branch,
                summary,
                ok=False,
                reference_files=ref_files,
                extra_roots=None,
            )
            return _ScopeGuardrailResult(
                action="return",
                outcome=Outcome(
                    State.BLOCKED,
                    f"scope violation: {len(out_of_scope)} file(s) "
                    f"outside ticket scope — "
                    f"{', '.join(out_of_scope)}",
                ),
            )

        # --- scope-triage enabled path ---
        return cls._run_scope_triage_classification(
            ctx,
            ticket,
            repo_dir,
            branch,
            summary,
            ref_files,
            target,
            settings,
            spec,
            out_of_scope,
            file_map,
            changed,
        )

    @classmethod
    def _clean_binary_artifacts(
        cls,
        ctx: StageContext,
        ticket: Ticket,
        repo_dir: Path,
        target: str,
        out_of_scope: list[str],
        file_map: set[str] | None,
        current_feedback: str | None,
    ) -> tuple[list[str], _ScopeGuardrailResult | None]:
        """Split out binary artifacts, clean them, and return text-only files.

        Returns ``(text_out_of_scope, None)`` when text files remain for
        further processing, or ``(text_out_of_scope, skip_result)`` when
        ALL out-of-scope files were binary artifacts (caller must return
        ``skip_result`` immediately).
        """
        binary_artifacts: list[str] = []
        text_out_of_scope: list[str] = []
        for f in out_of_scope:
            (
                binary_artifacts
                if _is_binary_artifact(repo_dir, f, target)
                else text_out_of_scope
            ).append(f)

        if binary_artifacts:
            cleaned: list[str] = []
            for path in binary_artifacts:
                # Restore tracked version first (no-op for untracked).
                try:
                    subprocess.run(
                        ["git", "-C", str(repo_dir), "checkout", "--", path],
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                except subprocess.CalledProcessError:
                    log.debug(
                        "_clean_binary_artifacts: git checkout failed for %s "
                        "— ignoring git failure",
                        path,
                        exc_info=True,
                    )
                # If the file still exists on disk, it was untracked
                # — remove it.
                file_path = repo_dir / path
                try:
                    if file_path.exists():
                        file_path.unlink()
                except OSError:
                    log.warning(
                        "%s: failed to unlink binary artifact: %s",
                        ticket.id,
                        path,
                        exc_info=True,
                    )
                log.warning(
                    "%s: auto-cleaned binary artifact: %s",
                    ticket.id,
                    path,
                )
                cleaned.append(path)

            ctx.service.add_step_event(
                ticket.id,
                "scope-triage auto-REJECT (binary artifacts): removed "
                + ", ".join(f"`{f}`" for f in cleaned)
                + " — runtime artifacts, not real work",
            )

        if not text_out_of_scope:
            log.info(
                "%s: all out-of-scope files were binary artifacts — "
                "skipping scope-triage LLM call",
                ticket.id,
            )
            return text_out_of_scope, _ScopeGuardrailResult(
                action="skip_iteration",
                file_map=file_map,
                feedback=current_feedback,
            )

        return text_out_of_scope, None

    @classmethod
    def _filter_vendored_deps(
        cls,
        ctx: StageContext,
        ticket: Ticket,
        repo_dir: Path,
        target: str,
        out_of_scope: list[str],
        file_map: set[str] | None,
        current_feedback: str | None,
    ) -> tuple[list[str], _ScopeGuardrailResult | None]:
        """Exclude vendored-dependency install directories by content signature.

        Pip/uv/npm vendored-dependency install dirs (``.pip-packages/``,
        ``local-deps/``, ``.deps/``, …) are UNtracked, have no durable
        name, and repeatedly flood the out-of-scope set with
        ``*.dist-info/METADATA``-style files. Detect them by CONTENT
        SIGNATURE, gate on UNtracked status, and log every auto-ignored
        dir non-silently.

        Returns ``(filtered, None)`` when text files remain for further
        processing, or ``(filtered, skip_result)`` when ALL out-of-scope
        files were in vendored-dep dirs.
        """
        vendored_roots = _vendored_dep_roots(repo_dir, out_of_scope, target)
        if not vendored_roots:
            return out_of_scope, None

        excluded: dict[str, int] = {}
        for root in vendored_roots:
            excluded[root] = sum(1 for f in out_of_scope if f.startswith(root + "/"))
        filtered = [f for f in out_of_scope if f.split("/", 1)[0] not in vendored_roots]
        for root, count in sorted(excluded.items()):
            msg = (
                f"scope-triage auto-ignored vendored-dep dir by content "
                f"signature: `{root}/` ({count} files) — untracked "
                f"install target, not scope creep"
            )
            log.info("%s: %s", ticket.id, msg)
            ctx.service.add_step_event(ticket.id, msg)
        if not filtered:
            log.info(
                "%s: all out-of-scope files were in vendored-dep dirs — "
                "skipping scope-triage LLM call",
                ticket.id,
            )
            return filtered, _ScopeGuardrailResult(
                action="skip_iteration",
                file_map=file_map,
                feedback=current_feedback,
            )
        return filtered, None

    @classmethod
    def _run_scope_triage_classification(
        cls,
        ctx: StageContext,
        ticket: Ticket,
        repo_dir: Path,
        branch: str,
        summary: str,
        ref_files: list[str] | None,
        target: str,
        settings,
        spec: str,
        out_of_scope: list[str],
        file_map: set[str],
        changed: list[str],
    ) -> _ScopeGuardrailResult:
        """Run the scope-triage LLM and route its verdict.

        Builds per-file diff summaries for the out-of-scope files,
        calls the scope-triage agent, and routes EXPAND / REJECT /
        ESCALATE verdicts (including finalization for terminal
        outcomes).
        """
        # --- build per-file diff summaries ---
        diff_summaries: dict[str, str] = {}
        for path in out_of_scope:
            raw = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_dir),
                    "diff",
                    f"origin/{target}",
                    "--",
                    path,
                ],
                capture_output=True,
                text=True,
            ).stdout
            if not raw.strip():
                # NEW (untracked) files produce an EMPTY ``git diff`` — the
                # triage agent then sees "no visible content", cannot judge
                # the file, and ESCALATEs to a human. Show the file head
                # instead so the agent gets the same 40-line budget of real
                # content.
                file_path = repo_dir / path
                if file_path.is_file():
                    try:
                        head = file_path.read_text(
                            encoding="utf-8", errors="replace"
                        ).split("\n")[:38]
                        raw = "NEW FILE (untracked — no diff vs base):\n" + "\n".join(
                            head
                        )
                    except OSError:
                        raw = "NEW FILE (untracked — unreadable)"
            lines = raw.split("\n")
            diff_summaries[path] = "\n".join(lines[:40])

        from robotsix_mill.agents import scope_triage as st

        triage_error: str | None = None
        try:
            verdict = st.run_scope_triage_agent(
                settings=settings,
                ticket_spec=spec,
                file_map=sorted(file_map),
                out_of_scope_files=out_of_scope,
                diff_summaries=diff_summaries,
            )
        except Exception as exc:
            log.error("%s: scope-triage agent failed: %s", ticket.id, exc)
            triage_error = f"{type(exc).__name__}: {exc}"
            verdict = None  # fall through to ESCALATE

        # --- route verdict ---
        if verdict is not None and verdict.action == "EXPAND":
            new_files = [f for f in verdict.expand_files if f not in file_map]
            if not new_files:
                log.info(
                    "%s: scope-triage EXPAND — all %d file(s) already in file_map; skipping",
                    ticket.id,
                    len(verdict.expand_files),
                )
                return _ScopeGuardrailResult(
                    action="skip_iteration",
                    file_map=file_map,
                    feedback=None,
                )
            for f in new_files:
                file_map.add(f)
            log.info(
                "%s: scope-triage EXPAND — %s",
                ticket.id,
                verdict.justification,
            )
            ctx.service.add_step_event(
                ticket.id,
                f"scope-triage EXPAND: {verdict.justification} "
                f"(added: {', '.join(new_files)})",
            )
            # Retroactive short-circuit: when every expand-file was
            # already modified in this pass, fall through to the test
            # gate instead of re-running the agent.
            if set(new_files).issubset(set(changed)):
                log.info(
                    "%s: scope-triage EXPAND retroactive — "
                    "all expanded files already modified; "
                    "skipping agent re-run",
                    ticket.id,
                )
                return _ScopeGuardrailResult(
                    action="skip_iteration",
                    file_map=file_map,
                    feedback=None,
                )
            else:
                return _ScopeGuardrailResult(
                    action="continue",
                    file_map=file_map,
                    feedback=None,
                )

        if verdict is not None and verdict.action == "REJECT":
            # Dedup guard: if ALL current out-of-scope files were
            # already REJECTed by a prior scope-triage step on this
            # ticket, the agent has seen this diff before and the
            # operator already has the signal. Don't emit another
            # event / bounce back to READY — treat as implicit
            # EXPAND so the implement loop can make actual progress.
            prior_rejects = [
                ev
                for ev in ctx.service.history(ticket.id)
                if ev.note and ev.note.startswith("scope-triage REJECT")
            ]
            already_rejected: set[str] = set()
            for ev in prior_rejects:
                for m in _BACKTICK_RE.findall(ev.note or ""):
                    already_rejected.add(m)
            new_oos = [f for f in out_of_scope if f not in already_rejected]
            if not new_oos:
                # The agent re-created files a prior REJECT already
                # cleaned. Don't bounce to READY again (that ping-pongs
                # forever) — but DON'T add them to file_map either, which
                # used to silently ship previously-REJECTed scope creep.
                # Clean them out of the tree again and fall through to the
                # test gate so the in-scope work can still make progress.
                log.warning(
                    "%s: duplicate scope-triage REJECT — all %d out-of-scope "
                    "file(s) re-created after a prior REJECT cleanup: %s. "
                    "Removing them again; not shipping without an explicit "
                    "EXPAND verdict.",
                    ticket.id,
                    len(out_of_scope),
                    ", ".join(out_of_scope),
                )
                git_ops.restore_paths(repo_dir, target, out_of_scope)
                return _ScopeGuardrailResult(
                    action="skip_iteration",
                    file_map=file_map,
                    feedback=None,
                )

            log.info(
                "%s: scope-triage REJECT — %s",
                ticket.id,
                verdict.justification,
            )
            git_ops.restore_paths(repo_dir, target, out_of_scope)
            cls._finalize(
                ctx,
                ticket,
                repo_dir,
                branch,
                summary,
                ok=False,
                reference_files=ref_files,
                extra_roots=None,
            )
            file_list = ", ".join(f"`{f}`" for f in out_of_scope)
            return _ScopeGuardrailResult(
                action="return",
                outcome=Outcome(
                    State.READY,
                    f"scope-triage REJECT: {verdict.justification[:200]} "
                    f"— out-of-scope: {file_list}",
                ),
            )

        # ESCALATE (or agent error fall-through).
        reason = (
            f"scope-triage ESCALATE: {verdict.justification}"
            if verdict is not None
            else (
                f"scope-triage agent error ({(triage_error or 'unknown')[:160]}) "
                "— escalated for human review; resume-blocked re-runs the triage"
            )
        )
        log.warning("%s: %s", ticket.id, reason)
        cls._finalize(
            ctx,
            ticket,
            repo_dir,
            branch,
            summary,
            ok=False,
            reference_files=ref_files,
            extra_roots=None,
        )
        file_list = ", ".join(f"`{f}`" for f in out_of_scope)
        return _ScopeGuardrailResult(
            action="return",
            outcome=Outcome(
                State.BLOCKED,
                f"{reason} — out-of-scope: {file_list}",
            ),
        )

    @classmethod
    def _resolve_language_instructions(
        cls, ctx: StageContext, ticket: Ticket, settings
    ) -> str:
        """Resolve the concatenated per-language instruction block, or
        ``""``. The repo's own ``.robotsix-mill/config.yaml`` ``languages``
        declaration (+ optional ``.robotsix-mill/language_instructions/``
        overrides) win over the central ``repos.yaml`` ``language``."""
        from ...config.repo_settings import resolve_language_instructions

        repo_dir = ctx.service.workspace(ticket).dir / "repo"
        return resolve_language_instructions(
            settings, repo_dir if repo_dir.exists() else None
        )

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
            except Exception:  # noqa: BLE001 — history note is best-effort
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
        verdict = classify_baseline_verdict(ci_conclusion, network_dependent)

        if verdict == "proceed":
            # Sandbox artifact — record a warning and proceed.
            names_or_diag = diag[:300]
            if ci_conclusion == "success":
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
            except Exception:  # noqa: BLE001 — history note is best-effort
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
        cls._finalize(
            ctx,
            ticket,
            repo_dir,
            branch,
            f"pre-existing test failures on {target} ({base_sha[:8]}): {diag[:400]}",
            ok=False,
            extra_roots=None,
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
        depends on (same title => same base_sha), else None."""
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
