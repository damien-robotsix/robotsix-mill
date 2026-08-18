"""Implementation-logic mixin: agent invocation, single pass, test/result evaluation."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from ...agents import coding
from ...agents.coding import AgentBudgetError, AgentRunError
from ...agents.coordinating import ValidationResult
from ...agents.runners.pass_runner import persist_memory
from ...agents.testing import smoke_paths_match
from ...config import Settings, target_branch_for
from ...config.repo_settings import load_repo_smoke_command
from ...core.models import Ticket
from ...core.states import State
from ...core.text_noop import spec_demands_code_change
from ...vcs import git_ops
from .. import short_circuit_verify
from ..base import Outcome, StageContext
from ..pause import (
    acknowledge_unanswered_threads,
    save_conversation_state,
)
from ._base import _ImplementStageBase
from ._shared import (
    _AgentRunOutcome,
    _ImplementContext,
    _is_config_only_change,
    _is_rename_only_change,
    _is_spec_exact_edits,
    _is_trivial_config_only_change,
    _should_skip_test_gate,
    _SinglePassResult,
    log,
)
from .implementation_editing import _ImplementationEditingMixin

# ---------------------------------------------------------------------------
# Post-summary verification gate
# ---------------------------------------------------------------------------

# Verbs the implement agent's free-text summary uses when it claims to have
# produced a file.  Only path-shaped tokens are extracted, so phrases like
# "added a test" or "created a helper" never match.
# The verb must not be the tail of a hyphenated compound: "glibc-generated
# lockfile" and "CI-generated artifact" describe something that already
# exists, not a claim to have produced it.  The gap between verb and path
# must not cross a clause boundary (``.``/``;``/em dash) either — without
# that, a verb binds to a path in an unrelated sentence 80 characters away.
_CLAIM_PATH_RE = re.compile(
    r"""
    (?<![-\w])(?:created|registered|added|wrote|written|generated)\b
    (?:(?!\.\s)[^\n`;\u2014]){0,80}?
    (?P<path>`[^`\n]+`|[\w./-]+\.(?:md|py|yaml|yml|toml|json|js|css|ts|sh|txt)|Dockerfile|Makefile)
    """,
    re.IGNORECASE | re.VERBOSE,
)

# "added X to Y" / "registered X in Y" — the file path appears after a
# prepositional phrase rather than immediately after the verb.  Captures
# things like "added a `repo: local` hook `check-trivyignore-expiry`
# to `.pre-commit-config.yaml`".
_CLAIM_X_TO_Y_RE = re.compile(
    r"""
    (?<![-\w])(?:added|registered)\b                 # trigger verb
    (?:(?!\.\s)[^\n;\u2014]){0,80}?                   # descriptive noun phrase (may include backtick-delimited tokens)
    \b(?:to|in|into|under)\s+                        # preposition
    (?P<path>`[^`\n]+`|[\w./-]+\.\w+|Dockerfile|Makefile)  # the actual path
    """,
    re.IGNORECASE | re.VERBOSE,
)

# "changelog fragment created" / "created a changelog fragment" — the claim
# names the fragment file almost never explicitly, so it needs a dedicated
# pattern.
_CHANGELOG_CLAIM_AFTER_RE = re.compile(
    r"\bchangelog(?:\s+fragment)?(?:\s+file)?\s+"
    r"(?:created|added|written|generated|registered)\b",
    re.IGNORECASE,
)
_CHANGELOG_CLAIM_BEFORE_RE = re.compile(
    r"\b(?:created|added|written|generated|registered)\s+"
    r"(?:a\s+)?(?:new\s+)?changelog(?:\s+fragment)?(?:\s+file)?\b",
    re.IGNORECASE,
)

# Explicit ``changelog.d/<file>`` mentions in the summary.
_CHANGELOG_D_PATH_RE = re.compile(r"\bchangelog\.d/[\w./-]+")

# A fragment the summary reports *removing* (typically a mis-named one from
# an earlier pass) is not a claim that it exists — matching it flags the
# tidy-up itself as a hallucinated file.
_FRAGMENT_REMOVAL_RE = re.compile(
    r"(?<![-\w])(?:deleted|removed|renamed|replaced)\b[^\n]{0,80}?$",
    re.IGNORECASE,
)

# Conventional towncrier fragment directories.  ``changelog.d`` is the
# default used by ``add_changelog_fragment``; the other two are the
# alternates accepted by ``_changelog_validate``.
_FRAGMENT_DIRS: tuple[str, ...] = ("changelog.d", "changelog", "changes")

# Common config/build files that have no file extension but are valid
# repo-relative paths (used by _looks_like_path).
_COMMON_EXTLESS_PATHS: frozenset[str] = frozenset(
    {
        "Dockerfile",
        "Makefile",
        "docker-compose",
        "docker-compose.yml",
        "docker-compose.yaml",
    }
)


def _looks_like_path(token: str) -> bool:
    """Return True when *token* is a plausible repo-relative file path."""
    # A backtick span holding whitespace is an inline command or a route,
    # not a path: `uv export --format cyclonedx1.5 -o sbom.cdx.json` and
    # `GET /wallet/value` both otherwise pass the checks below.
    if token != token.strip() or any(c.isspace() for c in token):
        return False
    if "/" in token or "\\" in token:
        return True
    if bool(re.search(r"\.\w{1,8}$", token)):
        return True
    # Allow common extensionless config/build files.
    return token in _COMMON_EXTLESS_PATHS


def _ticket_slug(ticket_id: str) -> str:
    """Filesystem-safe stem used for a ticket's changelog fragment file."""
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in ticket_id)
    return safe.strip("-") or "entry"


def _verify_summary_claims(
    summary: str,
    repo_dir: Path,
    ticket_id: str,
    target_branch: str = "main",
) -> list[str]:
    """Return repo-relative paths claimed in *summary* but missing on disk.

    The implement agent's free-text summary sometimes claims it "created" or
    "registered" files that never landed on disk (the changelog-fragment
    claim being the recurring case).  Parse those claims and check each
    against *repo_dir* so the stage can re-prompt instead of accepting a
    hallucinated completion.

    When a claimed path already exists on disk (not a new file), the check
    also cross-references ``git diff --stat origin/{target_branch}`` to
    ensure the branch actually modified that file — preventing false
    acceptances when the agent claims it "added X to Y" but made no diff.
    """
    missing: list[str] = []

    def add_missing(path: str) -> None:
        if path not in missing and not (repo_dir / path).exists():
            missing.append(path)

    for match in _CHANGELOG_D_PATH_RE.finditer(summary or ""):
        if _FRAGMENT_REMOVAL_RE.search(summary[: match.start()].rpartition("\n")[2]):
            continue
        add_missing(match.group(0).rstrip(".,;:)'\""))

    if _CHANGELOG_CLAIM_AFTER_RE.search(
        summary or ""
    ) or _CHANGELOG_CLAIM_BEFORE_RE.search(summary or ""):
        slug = _ticket_slug(ticket_id)
        if not any(list((repo_dir / d).glob(f"{slug}.*.md")) for d in _FRAGMENT_DIRS):
            add_missing(f"changelog.d/{slug}.*.md")

    # Collect paths from the direct verb-followed-by-path pattern.
    claimed_paths: list[str] = []
    for match in _CLAIM_PATH_RE.finditer(summary or ""):
        raw = match.group("path").strip().strip("`\"'").rstrip(".,;:)'\"")
        if not raw or not _looks_like_path(raw):
            continue
        if raw.startswith("changelog.d/"):
            continue
        claimed_paths.append(raw)

    # Collect paths from the "added X to Y" prepositional pattern.
    for match in _CLAIM_X_TO_Y_RE.finditer(summary or ""):
        raw = match.group("path").strip().strip("`\"'").rstrip(".,;:)'\"")
        if not raw or not _looks_like_path(raw):
            continue
        if raw.startswith("changelog.d/"):
            continue
        if raw not in claimed_paths:
            claimed_paths.append(raw)

    # A gitignored path can never show up in a diff, so the cross-check
    # below would flag every claim touching one (vendored trees, build
    # output, a `.gitignore` entry named in the summary).  Drop them.
    if claimed_paths:
        ignored = set(git_ops.ignored_paths(repo_dir, claimed_paths))
        claimed_paths = [p for p in claimed_paths if p not in ignored]

    # Check each claimed path against the filesystem and git diff.
    changed_files: set[str] | None = None
    for raw in claimed_paths:
        file_path = repo_dir / raw
        if not file_path.exists():
            missing.append(raw)
        elif file_path.is_dir():
            # A directory is never named in ``--name-only`` output, so the
            # diff cross-check cannot say anything about it.  Existing on
            # disk is all we can verify.
            continue
        else:
            # Existing file claimed as modified — verify it actually
            # has a working-tree or branch diff.  When the git diff is
            # unavailable (no git repo, no HEAD) we skip the check.
            if changed_files is None:
                changed_files = _collect_changed_files(repo_dir, target_branch)
            if changed_files is not None and raw not in changed_files:
                missing.append(raw)

    return missing


def _collect_changed_files(repo_dir: Path, target_branch: str) -> set[str] | None:
    """Return every file with an uncommitted change or a branch-introduced
    diff relative to ``origin/{target_branch}``.

    Combines ``git diff --name-only origin/{b}...HEAD`` (committed
    branch-introduced changes) and ``git diff --name-only HEAD``
    (working-tree changes).

    Returns ``None`` when git diff is not available (no git repo, no HEAD,
    or target branch ref not found).
    """
    from ...vcs import git_ops

    try:
        # First check whether origin/{target_branch} ref exists.
        ref = f"origin/{target_branch}"
        try:
            git_ops._git(repo_dir, "rev-parse", "--verify", ref)
        except Exception:
            log.debug(
                "_collect_changed_files: ref %s not found in %s — "
                "treating as no-git-diff available",
                ref,
                repo_dir,
            )
            return None

        introduced = git_ops.introduced_files(repo_dir, target_branch)
        return set(introduced)
    except Exception:
        log.warning(
            "_collect_changed_files: could not compute git diff for %s "
            "against origin/%s — treating as no-git-diff available",
            repo_dir,
            target_branch,
            exc_info=True,
        )
        return None


class ImplementationLogicMixin(_ImplementationEditingMixin, _ImplementStageBase):
    """Agent-driven coding passes for :class:`ImplementStage`.

    Special-case edit handlers (:class:`_verify_repo_changes`,
    :class:`_handle_rename_only_change`, :class:`_handle_spec_exact_edits`,
    :class:`_find_insertion_point`) are provided by
    :class:`._implementation_editing._ImplementationEditingMixin`.
    """

    @classmethod
    def _select_agent_level(
        cls,
        ic: _ImplementContext,
        settings,
        repo_dir: Path,
        target_branch: str,
    ) -> int | None:
        """Pick the cheaper level-1 model for simple tickets, or bypass LLM
        entirely for trivial config-only, rename-only, and spec-exact-code
        tickets.

        Returns ``-2`` for:
        * a trivial config-only addition — every changed file is
          config-only, the total delta is ≤ 40 lines, and at least one
          file is new (a fresh presence/config file).  Bypass the LLM
          coordinator entirely; apply deterministically.

        Returns ``-1`` for:
        * a spec-exact-code ticket — the description contains fenced code
          blocks with file paths referencing existing files, so edits can
          be applied deterministically without an LLM.

        Returns ``0`` for:
        * a rename-only change (every non-rename change is a config/doc
          stub or zero-delta file) — bypass the LLM coordinator entirely.

        Returns ``1`` for:
        * a no-change-needed re-check (the previous attempt already
          concluded ``no_change_needed`` — pure re-check with the flash
          model); or
        * a config/docs-only ticket (every changed file is ``.md``,
          ``.yaml``, ``.toml``, etc. — no code to test).

        Returns ``None`` otherwise (keep the default level-2 model).

        The LLM-bypass levels (``-2``, ``-1``, ``0``) are suppressed whenever
        ``ic.feedback`` is set: they cannot act on reviewer feedback, so
        taking one on a sendback re-emits the rejected diff unchanged.
        """
        prev = (ic.previous_attempt_summary or "") + (ic.feedback or "")
        if "no change needed" in prev.lower():
            return 1

        # A reviewer sendback carries specific requested changes. The three
        # LLM-bypass levels below (-2, 0, -1) re-derive their edit from what is
        # already in the working tree and never read ``feedback``, so on a
        # sendback they reproduce the exact diff the reviewer just rejected:
        # review sends it back, implement bypasses again, and the ticket burns
        # its cycle ceiling and blocks. That loop was the largest blocked class
        # on the board after the refine fix — four tickets, each with a review
        # asking for a concrete edit and four identical "trivial config-only
        # addition" passes that never made it.
        #
        # Level 1 stays reachable: it is a real (cheaper) LLM pass, so it can
        # act on the feedback.
        has_feedback = bool((ic.feedback or "").strip())

        if not has_feedback and _is_trivial_config_only_change(repo_dir, target_branch):
            return -2
        if _is_config_only_change(repo_dir, target_branch):
            return 1
        if not has_feedback and _is_rename_only_change(repo_dir, target_branch):
            return 0
        if not has_feedback and _is_spec_exact_edits(ic.spec, repo_dir):
            # Sentinel check: if a prior spec-exact attempt already
            # failed (no edits applied), fall through to the LLM path
            # instead of re-entering the same doomed deterministic path.
            if ic.previous_attempt_summary and ic.previous_attempt_summary.startswith(
                "spec-exact bypass: failed"
            ):
                return None
            return -1
        return None

    @classmethod
    def _invoke_implement_agent(
        cls,
        ctx: StageContext,
        ticket: Ticket,
        repo_dir: Path,
        branch: str,
        settings,
        ic: _ImplementContext,
        language_instructions: str,
        agent_level: int | None,
        resume_history: list[Any] | None,
        extra_roots: list[Path] | None,
        memory_board_id: str,
        ws=None,  # Workspace — needed for save_conversation_state on budget error
        target_branch: str = "main",
    ) -> _AgentRunOutcome:
        """Invoke ``coding.run_implement_agent`` and capture caught errors.

        Returns an ``_AgentRunOutcome`` whose mutually-exclusive
        ``success`` / ``failure`` fields let the orchestrator early-return
        cleanly on budget / agent-error paths without duplicating control
        flow.  ``success`` holds the raw 7-tuple from
        ``run_implement_agent``; ``failure`` holds the
        ``_SinglePassResult`` already finalized for return.
        """
        try:
            result = coding.run_implement_agent(
                settings=settings,
                repo_dir=repo_dir,
                spec=ic.spec,
                feedback=ic.feedback,
                memory=ic.memory_text,
                reference_files=ic.reference_files,
                previous_attempt_summary=ic.previous_attempt_summary,
                board_id=memory_board_id,
                current_ticket_id=ticket.id,
                message_history=resume_history,
                language_instructions=language_instructions,
                extra_roots=extra_roots,
                level=agent_level,
                sandbox_image=ctx.repo_config.sandbox_image
                if ctx.repo_config
                else None,
                target_branch=target_branch,
            )
        except AgentBudgetError as e:
            if e.conversation_state is not None and ws is not None:
                save_conversation_state(ws, e.conversation_state, "implement")
            cls._finalize(
                ctx,
                ticket,
                repo_dir,
                branch,
                f"budget cap hit: {e}",
                ok=False,
                extra_roots=extra_roots,
            )
            return _AgentRunOutcome(
                failure=_SinglePassResult(
                    next_action="return",
                    outcome=Outcome(
                        State.BLOCKED,
                        f"agent budget cap — resumable (move to READY): {e}",
                    ),
                )
            )
        except AgentRunError as e:
            # If the original cause is a transient infra failure
            # (OpenRouter timeout, 5xx, 429, disk-full, …), re-raise
            # the typed cause so the worker's classify_stage_error
            # picks it up and schedules a retry-with-backoff via
            # set_retry_state.  Do NOT call _finalize first — that
            # would persist a spec fingerprint and poison the next
            # pass with a false "spec unchanged" block.
            if e.cause is not None:
                from ...runtime.transient_errors import (
                    classify_stage_error,
                    is_insufficient_credit,
                    parse_credit_shortfall,
                )

                if is_insufficient_credit(e.cause):
                    from ...runtime.credit_status import record_low_credit

                    detail = parse_credit_shortfall(e.cause)
                    record_low_credit(detail=detail)

                if classify_stage_error(e.cause) == "transient":
                    raise e.cause from e
            # Non-transient agent error — record the outcome (spec-
            # determined dead-end) so the fingerprint guard can block
            # a re-spawn with an unchanged spec.
            cls._finalize(
                ctx,
                ticket,
                repo_dir,
                branch,
                f"agent error: {e}",
                ok=False,
                extra_roots=extra_roots,
            )
            return _AgentRunOutcome(
                failure=_SinglePassResult(
                    next_action="return",
                    outcome=Outcome(
                        State.BLOCKED,
                        f"agent error — resumable: {e}",
                    ),
                )
            )
        return _AgentRunOutcome(success=result)

    @classmethod
    def _persist_pass_artifacts(
        cls,
        ws,
        ticket: Ticket,
        ic: _ImplementContext,
        summary: str,
        ref_files: list[str] | None,
        updated_memory: str,
        settings,
        memory_board_id: str,
    ) -> tuple[list[Any] | None, str | None]:
        """Persist memory, ``reference_files.json`` and ``implement_summary.md``."""
        if updated_memory:
            persist_memory(
                settings.memory_file_for("implement", memory_board_id),
                updated_memory,
            )

        # Build updated reference_files for the context.
        updated_ref_files = ic.reference_files
        if ref_files:
            updated_ref_files = [{"path": p} for p in ref_files]
            try:
                ref_path = ws.artifacts_dir / "reference_files.json"
                ref_path.write_text(
                    json.dumps(updated_ref_files, indent=2),
                    encoding="utf-8",
                )
            except OSError:
                log.warning(
                    "%s: failed to write reference_files.json",
                    ticket.id,
                    exc_info=True,
                )

        # Persist summary for <previous_attempt> injection on retry.
        updated_prev_summary = ic.previous_attempt_summary
        try:
            (ws.artifacts_dir / "implement_summary.md").write_text(
                summary,
                encoding="utf-8",
            )
            updated_prev_summary = summary
        except OSError:
            log.warning(
                "%s: failed to write implement_summary.md",
                ticket.id,
                exc_info=True,
            )

        return updated_ref_files, updated_prev_summary

    @classmethod
    def _run_summary_verification(
        cls,
        ticket: Ticket,
        repo_dir: Path,
        summary: str,
        ic: _ImplementContext,
        updated_ref_files: list[Any] | None,
        updated_prev_summary: str | None,
        new_msgs: bytes | None,
        target_branch: str = "main",
    ) -> _SinglePassResult | None:
        """Verify the summary's claimed artifacts exist on disk.

        Returns ``None`` when every claim checks out (the pass may proceed),
        or a ``_SinglePassResult`` to return directly: a single ``retry``
        re-prompt on the first failure, and a ``return`` with ``BLOCKED`` on
        the second consecutive failure (the ``[VERIFY]`` feedback marker
        tells us we already re-prompted once).
        """
        missing = _verify_summary_claims(
            summary,
            repo_dir,
            ticket.id,
            target_branch=target_branch,
        )
        if not missing:
            return None

        missing_text = ", ".join(missing)
        feedback = (
            f"[VERIFY] Verification failed: {missing_text} was claimed "
            "but does not exist on disk — fix and retry."
        )
        if (ic.feedback or "").startswith("[VERIFY]"):
            log.warning(
                "%s: summary verification failed again — %s; blocking",
                ticket.id,
                missing_text,
            )
            return _SinglePassResult(
                next_action="return",
                outcome=Outcome(
                    State.BLOCKED,
                    f"summary verification failed after retry: {missing_text}",
                ),
            )

        log.warning(
            "%s: summary verification failed — %s; re-prompting",
            ticket.id,
            missing_text,
        )
        verify_ic = _ImplementContext(
            spec=ic.spec,
            memory_text=ic.memory_text,
            reference_files=updated_ref_files,
            file_map=ic.file_map,
            feedback=feedback,
            previous_attempt_summary=updated_prev_summary,
            open_thread_ids=ic.open_thread_ids,
        )
        return _SinglePassResult(
            next_action="retry",
            feedback=feedback,
            ic=verify_ic,
            new_msgs=new_msgs,
        )

    @classmethod
    def _evaluate_test_results(
        cls,
        ctx: StageContext,
        ticket: Ticket,
        repo_dir: Path,
        branch: str,
        settings,
        ic: _ImplementContext,
        new_ic: _ImplementContext,
        summary: str,
        ref_files: list[str] | None,
        new_msgs,
        no_change_needed: bool,
        no_change_rationale: str,
        resuming: bool,
        attempt: int,
        max_iters: int,
        extra_roots: list[Path] | None,
        head_before: str | None = None,
    ) -> _SinglePassResult:
        """Run the test gate, apply ``ValidationResult.decide``, route the pass."""
        target = target_branch_for(settings, ctx.repo_config)
        from robotsix_mill.stages import implement as _facade

        ticket_summary = (ic.spec or ticket.title or "")[:200]
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
            )
        # --- path-scoped smoke gate (runs ONLY after unit tests pass) ---
        # No point smoking a red build; a smoke failure folds into the
        # SAME passed/diag → ValidationResult.decide machinery as a test
        # failure (retry while iterations remain, escalate on the last,
        # BLOCKED on sandbox-unavailable). Strictly opt-in: skipped
        # entirely unless a smoke command is set (repo file wins over the
        # global fallback), and skipped when the ticket's introduced
        # files don't match the repo's smoke_paths globs.
        passed, diag = cls._run_smoke_gate(
            ctx, ticket, repo_dir, target, settings, passed, diag
        )
        if not passed and diag.startswith("sandbox unavailable"):
            cls._finalize(
                ctx,
                ticket,
                repo_dir,
                branch,
                summary,
                ok=False,
                reference_files=ref_files,
                extra_roots=extra_roots,
            )
            return _SinglePassResult(
                next_action="return",
                outcome=Outcome(State.BLOCKED, diag),
            )

        decision = ValidationResult.decide(
            passed=passed,
            iterations=attempt,
            max_iters=max_iters,
            feedback=diag,
        )

        if decision.next_action == "proceed":
            # ``no_change_needed`` → DONE works on both fresh runs and
            # resumes. The agent's signal that the spec is already
            # satisfied is meaningful regardless of how we got here; in
            # fact the resume case is exactly the bc-check
            # "remove-dead-X" flavour where a human unblocked the
            # ticket precisely because they suspect the work was
            # already landed by a sibling.
            #
            # Guard against a resume-case false positive: when the
            # branch carries commits ahead of ``origin/main`` (the
            # agent's previous iterations already produced the diff),
            # routing to DONE silently strands that work in the
            # workspace — it never reaches deliver, no PR is opened.
            # The guard lives inside _detect_no_change_contradiction:
            # the "resuming with clean working tree, branch ahead of
            # target" path now returns None (proceed to deliver) instead
            # of routing to DONE.
            # --- no-change contradiction detection ---
            # Two branches: (a) agent explicitly signalled no_change_needed,
            # (b) empty diff on a fresh run.  Both route through the same
            # edit-claim / gitignored-edit / formatter-reverted guards.
            no_change_result = cls._detect_no_change_contradiction(
                ctx,
                ticket,
                repo_dir,
                branch,
                settings,
                summary,
                ic.spec or "",
                ref_files,
                new_msgs,
                no_change_needed,
                no_change_rationale,
                resuming,
                target,
                extra_roots,
                attempt=attempt,
                max_iters=max_iters,
                new_ic=new_ic,
            )
            if no_change_result is not None:
                return no_change_result
            # --- per-claimed-file & zero-tool-call guards ---
            verify_result = cls._verify_repo_changes(
                ctx,
                ticket,
                repo_dir,
                branch,
                settings,
                summary,
                ref_files,
                new_msgs,
                new_ic,
                ic,
                target,
                extra_roots,
                resuming,
                attempt,
                max_iters,
                head_before=head_before,
            )
            if verify_result is not None:
                return verify_result

            # --- post-agent thread acknowledgment ---
            if ic.open_thread_ids and ic.feedback:
                acknowledge_unanswered_threads(ctx, ticket, ic.open_thread_ids)
            cls._finalize(
                ctx,
                ticket,
                repo_dir,
                branch,
                summary,
                ok=True,
                reference_files=ref_files,
                extra_roots=extra_roots,
            )
            next_state = (
                State.CODE_REVIEW if settings.review_enabled else State.DOCUMENTING
            )
            # Same-state step event so implement gets its own visible
            # row in history. Without this, the ticket's history shows
            # `ready -> code_review` (or `ready -> documenting`) and
            # the implement summary lives on the code_review/documenting
            # row — fine on inspection, but the row reads as the
            # downstream stage rather than what implement just did.
            # The downstream Outcome's note is a short stage-name
            # marker; the full summary lives on the step event (and
            # in artifacts/implement.md).
            ctx.service.add_step_event(
                ticket.id,
                f"implement: {summary[:400]}",
            )
            next_note = (
                "code review starting"
                if next_state is State.CODE_REVIEW
                else "documenting starting"
            )
            # Increment the ticket-lifetime implement-cycle counter
            # so the convergence backstop in phase_coordinator can
            # catch a runaway implement↔review loop.
            if next_state is State.CODE_REVIEW:
                ctx.service.set_implement_cycles(ticket.id, ticket.implement_cycles + 1)
            return _SinglePassResult(
                next_action="proceed",
                outcome=Outcome(next_state, next_note),
            )

        if decision.next_action == "escalate":
            cls._finalize(
                ctx,
                ticket,
                repo_dir,
                branch,
                summary,
                ok=False,
                reference_files=ref_files,
                extra_roots=extra_roots,
            )
            return _SinglePassResult(
                next_action="escalate",
                outcome=Outcome(
                    State.BLOCKED,
                    f"tests still failing after {max_iters} fix "
                    "attempt(s) — resumable (move to READY)",
                ),
            )

        # retry → feed the diagnosis into the next edit pass.
        new_ic.feedback = diag
        return _SinglePassResult(
            next_action="retry",
            feedback=diag,
            ic=new_ic,
            new_msgs=new_msgs,
        )

    @classmethod
    def _run_smoke_gate(
        cls,
        ctx: StageContext,
        ticket: Ticket,
        repo_dir: Path,
        target: str,
        settings: Settings,
        passed: bool,
        diag: str,
    ) -> tuple[bool, str]:
        """Run the smoke-test gate when unit tests pass.

        Opt-in per-repo via ``smoke_command`` and ``smoke_paths``;
        skipped entirely when the ticket's introduced files don't
        match the repo's ``smoke_paths`` globs.

        Returns ``(passed, diag)`` unchanged when the smoke gate is
        skipped or passes; folds a smoke failure into
        ``(False, smoke_diag)``.
        """
        if not passed:
            return passed, diag
        smoke_cmd = (
            load_repo_smoke_command(repo_dir) or settings.smoke_command
        ).strip()
        if not smoke_cmd:
            return passed, diag
        from robotsix_mill.stages import implement as _facade

        changed = git_ops.introduced_files(repo_dir, target)
        smoke_paths = _facade.load_repo_smoke_paths(repo_dir)
        if not smoke_paths_match(changed, smoke_paths):
            return passed, diag
        smoke_passed, smoke_diag = _facade.run_smoke_agent(
            settings=settings,
            repo_dir=repo_dir,
            repo_config=ctx.repo_config,
        )
        # The board browser smoke writes its screenshot to
        # ``<clone>/artifacts/board.png`` (BOARD_SMOKE_SCREENSHOT,
        # relative to the sandbox cwd = the repo clone, the only
        # writable mount). The review stage reads it from the
        # workspace artifacts dir — a sibling of the clone, outside
        # the sandbox mount — so lift it out here. Absent for
        # non-board smokes / a failed render → review stays
        # text-only, unchanged.
        # MOVE (not copy) so the screenshot never lingers in
        # the clone's working tree — otherwise ``_finalize``'s
        # ``git add -A`` would stage and commit it into the
        # feature branch (``.png`` is not a binary artifact and
        # the smoke runs past the scope guardrail).
        ws = ctx.service.workspace(ticket)
        src_png = repo_dir / "artifacts" / "board.png"
        if src_png.exists():
            shutil.move(str(src_png), str(ws.artifacts_dir / "board.png"))
        if not smoke_passed:
            return False, smoke_diag
        return passed, diag

    @classmethod
    def _detect_no_change_contradiction(
        cls,
        ctx: StageContext,
        ticket: Ticket,
        repo_dir: Path,
        branch: str,
        settings: Settings,
        summary: str,
        spec_text: str,
        ref_files: list[str] | None,
        new_msgs: bytes | None,
        no_change_needed: bool,
        no_change_rationale: str,
        resuming: bool,
        target: str,
        extra_roots: list[Path] | None,
        attempt: int = 1,
        max_iters: int = 1,
        new_ic: _ImplementContext | None = None,
    ) -> _SinglePassResult | None:
        """Check for edit-claim contradictions when the diff is empty.

        An agent that invokes file-mutating tools yet claims
        ``no_change_needed`` (or produces an empty diff on a fresh
        run) is signalling lost work — BLOCK for inspection instead
        of closing DONE.  A confirmed formatter-reverted / redundant
        edit is exempt (a true no-op).

        Returns a ``_SinglePassResult`` when a contradiction is found
        (caller must return it immediately); ``None`` when the empty
        diff is legitimate and the caller should close DONE.
        """
        _ = settings  # unused in this helper
        if not cls._any_repo_has_changes(
            repo_dir, extra_roots, target, settings=settings
        ):
            if no_change_needed and no_change_rationale.strip():
                # Agent explicitly signalled no_change_needed.
                edit_tools = short_circuit_verify.detect_edit_claim_contradiction(
                    has_changes=False, new_messages=new_msgs
                )
                if edit_tools:
                    fmt_result = cls._edits_formatter_reverted(repo_dir, new_msgs)
                    if fmt_result is not True:
                        tool_list = ", ".join(edit_tools)
                        diag = (
                            f"{no_change_rationale.strip() or summary}\n\n"
                            "[Diagnostic] implement was about to close this ticket "
                            "as ``no_change_needed`` because ``git diff`` is empty "
                            f"— but the agent invoked file-mutating tools "
                            f"({tool_list}) during the run, and replaying those "
                            "edits + formatting still produced a real change (or "
                            "could not be verified). An empty diff after real edit "
                            "calls means the work did NOT persist (edits reverted, "
                            "workspace reset mid-run, or written outside the clone). "
                            "Closing as no-change would silently lose that work, so "
                            "the ticket is BLOCKED for inspection. Re-run implement; "
                            "if the spec genuinely needs no change, the agent must "
                            "reach that conclusion WITHOUT calling "
                            "write_file/edit_file/Write/Edit."
                        )
                        cls._finalize(
                            ctx,
                            ticket,
                            repo_dir,
                            branch,
                            diag,
                            ok=False,
                            reference_files=ref_files,
                            extra_roots=extra_roots,
                        )
                        return _SinglePassResult(
                            next_action="return",
                            outcome=Outcome(
                                State.BLOCKED,
                                "edit-claim contradiction (empty diff after edit calls)",
                            ),
                        )
                # Guard: spec demands a non-empty diff — closing DONE
                # would contradict the spec.
                if spec_demands_code_change(spec_text):
                    diag = (
                        f"{no_change_rationale.strip()}\n\n"
                        "[Diagnostic] The spec explicitly mandates a non-empty diff "
                        "but the agent concluded no_change_needed with an empty diff. "
                        "The spec demands code changes that do not exist at HEAD; "
                        "closing as DONE would be a false positive."
                    )
                    cls._finalize(
                        ctx,
                        ticket,
                        repo_dir,
                        branch,
                        diag,
                        ok=False,
                        reference_files=ref_files,
                        extra_roots=extra_roots,
                    )
                    return _SinglePassResult(
                        next_action="return",
                        outcome=Outcome(
                            State.BLOCKED,
                            "spec demands code change but diff is empty "
                            "(no_change_needed)",
                        ),
                    )
                # No contradiction — close DONE.
                rationale = no_change_rationale.strip()
                short = rationale[:400] + ("…" if len(rationale) > 400 else "")
                cls._finalize(
                    ctx,
                    ticket,
                    repo_dir,
                    branch,
                    f"no change needed — {rationale}",
                    ok=True,
                    reference_files=ref_files,
                    extra_roots=extra_roots,
                )
                return _SinglePassResult(
                    next_action="return",
                    outcome=Outcome(State.DONE, f"no change needed — {short}"),
                )
            if not resuming:
                # Empty diff on a fresh run: the working tree is clean AND
                # the branch has no commits beyond ``origin/<target>`` —
                # there is genuinely nothing to merge.
                #
                # Two guards protect against silently closing real work:
                #   (a) gitignored edits — real writes into a gitignored
                #       path are invisible to ``git status`` and surface
                #       as an opaque empty diff. Closing DONE would lose
                #       deliverable work → BLOCK.
                #   (b) edit-claim contradiction — the run invoked
                #       file-mutating tools yet nothing landed (edits
                #       reverted, workspace reset mid-run, or written off
                #       clone). Closing DONE would lose that work → BLOCK.
                #       A confirmed formatter-reverted / redundant edit
                #       (``_edits_formatter_reverted`` is True) is a true
                #       no-op and is exempt.
                no_change_summary = summary or (
                    "Agent finished without producing any file edits and "
                    "without explanation. Check artifacts/implement_messages.json "
                    "for the full transcript."
                )
                # Guard (a): gitignored-edit detector.
                ignored_hits = cls._claimed_gitignored_edits(repo_dir, new_msgs)
                if ignored_hits:
                    hit_list = ", ".join(f"`{p}`" for p in ignored_hits)
                    no_change_summary = (
                        f"edits landed in gitignored path(s): {hit_list} — the "
                        "files exist on disk but git cannot see them, so this "
                        "board cannot deliver them (vcs-imported / vendored "
                        "sub-tree). The spec must target git-tracked files, or "
                        "the board needs manifest-aware delivery for that "
                        f"sub-tree.\n\n{no_change_summary}"
                    )
                    cls._finalize(
                        ctx,
                        ticket,
                        repo_dir,
                        branch,
                        no_change_summary,
                        ok=False,
                        reference_files=ref_files,
                        extra_roots=extra_roots,
                    )
                    reason = " ".join(no_change_summary.split())
                    return _SinglePassResult(
                        next_action="return",
                        outcome=Outcome(
                            State.BLOCKED,
                            f"no changes produced — {reason[:300]}"
                            + ("… (see implement.md)" if len(reason) > 300 else ""),
                        ),
                    )
                # Guard (b): edit-claim contradiction.
                edit_tools = short_circuit_verify.detect_edit_claim_contradiction(
                    has_changes=False, new_messages=new_msgs
                )
                if edit_tools and (
                    cls._edits_formatter_reverted(repo_dir, new_msgs) is not True
                ):
                    tool_list = ", ".join(edit_tools)
                    diag = (
                        f"{no_change_summary}\n\n"
                        "[Diagnostic] implement produced an empty diff, but the "
                        f"agent invoked file-mutating tools ({tool_list}) during "
                        "the run and replaying those edits + formatting still "
                        "produced a real change (or could not be verified). An "
                        "empty diff after real edit calls means the work did NOT "
                        "persist (edits reverted, workspace reset mid-run, or "
                        "written outside the clone). Closing as no-change would "
                        "silently lose that work, so the ticket is BLOCKED for "
                        "inspection."
                    )
                    cls._finalize(
                        ctx,
                        ticket,
                        repo_dir,
                        branch,
                        diag,
                        ok=False,
                        reference_files=ref_files,
                        extra_roots=extra_roots,
                    )
                    return _SinglePassResult(
                        next_action="return",
                        outcome=Outcome(
                            State.BLOCKED,
                            "edit-claim contradiction (empty diff after edit calls)",
                        ),
                    )
                # Guard: spec demands a non-empty diff — closing DONE
                # would contradict the spec.
                if spec_demands_code_change(spec_text):
                    diag = (
                        f"{no_change_summary}\n\n"
                        "[Diagnostic] The spec explicitly mandates a non-empty diff, "
                        "but implement produced an empty diff on a fresh run with no "
                        "lost-work evidence. The spec demands code changes that do "
                        "not exist at HEAD; closing as DONE would be a false positive."
                    )
                    cls._finalize(
                        ctx,
                        ticket,
                        repo_dir,
                        branch,
                        diag,
                        ok=False,
                        reference_files=ref_files,
                        extra_roots=extra_roots,
                    )
                    return _SinglePassResult(
                        next_action="return",
                        outcome=Outcome(
                            State.BLOCKED,
                            "spec demands code change but diff is empty (fresh run)",
                        ),
                    )
                # Genuine no-op: clean working tree, no commits beyond the
                # base, no gitignored writes, no lost edits. The spec is
                # already satisfied — terminate DONE instead of looping.
                done_note = "already satisfied — no changes needed (empty diff vs base)"
                cls._finalize(
                    ctx,
                    ticket,
                    repo_dir,
                    branch,
                    f"{done_note}\n\n{no_change_summary}",
                    ok=True,
                    reference_files=ref_files,
                    extra_roots=extra_roots,
                )
                log.info(
                    "%s: empty diff on fresh run with no lost work — DONE "
                    "(already satisfied)",
                    ticket.id,
                )
                return _SinglePassResult(
                    next_action="return",
                    outcome=Outcome(State.DONE, done_note),
                )
            # Resuming with empty diff: the branch already satisfied the
            # spec in a prior session and the current pass contributed
            # no new edits.  Route to DONE — just like the fresh-run
            # path above — instead of falling through to
            # _verify_repo_changes (which would block on zero tool calls
            # or route to CODE_REVIEW with an empty diff that delivers
            # nothing).
            if resuming:
                # Apply the same edit-claim contradiction guard as the
                # fresh-run path — an agent that called edit tools is
                # signalling lost work.
                edit_tools_rs = short_circuit_verify.detect_edit_claim_contradiction(
                    has_changes=False, new_messages=new_msgs
                )
                if edit_tools_rs and (
                    cls._edits_formatter_reverted(repo_dir, new_msgs) is not True
                ):
                    tool_list = ", ".join(edit_tools_rs)
                    diag = (
                        f"{summary or 'Agent finished without producing file edits.'}\n\n"
                        "[Diagnostic] implement produced an empty diff on a resuming "
                        f"run, but the agent invoked file-mutating tools ({tool_list}) "
                        "and replaying those edits + formatting still produced a real "
                        "change (or could not be verified).  Blocking for inspection."
                    )
                    cls._finalize(
                        ctx,
                        ticket,
                        repo_dir,
                        branch,
                        diag,
                        ok=False,
                        reference_files=ref_files,
                        extra_roots=extra_roots,
                    )
                    return _SinglePassResult(
                        next_action="return",
                        outcome=Outcome(
                            State.BLOCKED,
                            "edit-claim contradiction "
                            "(empty diff after edit calls on resume)",
                        ),
                    )
                # Guard: spec demands a non-empty diff — closing DONE
                # would contradict the spec.
                if spec_demands_code_change(spec_text):
                    diag = (
                        f"{summary or 'Agent found no work to do.'}\n\n"
                        "[Diagnostic] The spec explicitly mandates a non-empty diff, "
                        "but implement produced an empty diff on a resuming run. "
                        "The spec demands code changes that do not exist at HEAD; "
                        "closing as DONE would be a false positive."
                    )
                    cls._finalize(
                        ctx,
                        ticket,
                        repo_dir,
                        branch,
                        diag,
                        ok=False,
                        reference_files=ref_files,
                        extra_roots=extra_roots,
                    )
                    return _SinglePassResult(
                        next_action="return",
                        outcome=Outcome(
                            State.BLOCKED,
                            "spec demands code change but diff is empty (resume)",
                        ),
                    )
                resume_done_note = (
                    "already satisfied — no changes needed "
                    "(resuming with empty diff vs base)"
                )
                cls._finalize(
                    ctx,
                    ticket,
                    repo_dir,
                    branch,
                    f"{resume_done_note}\n\n{summary or 'Agent found no work to do.'}",
                    ok=True,
                    reference_files=ref_files,
                    extra_roots=extra_roots,
                )
                log.info(
                    "%s: empty diff on resuming run with no lost work — "
                    "DONE (already satisfied)",
                    ticket.id,
                )
                return _SinglePassResult(
                    next_action="return",
                    outcome=Outcome(State.DONE, resume_done_note),
                )
        # Resuming with a clean working tree but branch already ahead
        # of target: prior passes landed the implementation and the
        # current pass found nothing more to do.  Route to DONE instead
        # of falling through to CODE_REVIEW (which would re-review the
        # same prior work and loop back, burning spawn budget).
        _working_tree_clean = not git_ops.has_changes(repo_dir)
        if _working_tree_clean and extra_roots:
            for _rp in extra_roots:
                if _rp != repo_dir and git_ops.has_changes(_rp):
                    _working_tree_clean = False
                    break
        if resuming and _working_tree_clean:
            # Edit-claim contradiction guard: an agent that called
            # edit tools on a resume-with-ahead branch is signalling
            # lost work — those edits didn't land in the working tree.
            edit_tools_rs = short_circuit_verify.detect_edit_claim_contradiction(
                has_changes=False, new_messages=new_msgs
            )
            # ...unless every file it touched is ALREADY changed on the branch.
            # Then the edits were an idempotent re-application of work a prior
            # pass committed, which is the normal shape of a resume, not a
            # loss. Blocking those stranded 7 tickets across five boards
            # (observed 2026-08-02..08-07), five of them on this exact path.
            _target = target_branch_for(ctx.settings, ctx.repo_config)
            _branch_changed = git_ops.changed_source_files(repo_dir, _target)
            _already_landed = short_circuit_verify.claimed_edits_already_on_branch(
                new_messages=new_msgs,
                branch_changed_files=_branch_changed,
            )
            if _already_landed and edit_tools_rs:
                log.info(
                    "%s: edit-claim contradiction suppressed — every claimed "
                    "edit (%s) is already present in the branch's diff vs %s "
                    "(idempotent re-application on resume)",
                    ticket.id,
                    ", ".join(edit_tools_rs),
                    _target,
                )
            # A branch that already carries committed work is not "lost work",
            # even when one claimed path is missing from its diff.  The guard
            # below asks whether EVERY claimed edit landed; a single stray path
            # — a changelog fragment a later pass renamed away, a file the agent
            # wrote and then reverted — flipped that to False and blocked the
            # whole ticket.  Measured on 2026-08-13: four of the five tickets
            # blocked on this path had 2-4 commits and 2-6 changed files sitting
            # in their workspace, stranded (robotsix-chat a78d/a801/3f3e,
            # robotsix-mill 8b2c).  The fifth (959e) had an empty branch, which
            # is the shape the guard is actually for — so gate on that instead
            # and let a branch with real commits flow on to CODE_REVIEW →
            # deliver, exactly as the fall-through below already intends.
            # ``changed_source_files`` returns [] on any git error, so an
            # unreadable branch still blocks: the guard fails closed as before.
            if edit_tools_rs and not _already_landed and _branch_changed:
                # Name the paths that failed the all()-check, so the next
                # reader sees which edit went missing rather than only that
                # one did. ``detect_missing_claimed_files`` is deliberately
                # narrower (it reports only files the summary asserts as
                # landed), so it is the wrong lens for this log line.
                _on_branch = {os.path.basename(f) for f in _branch_changed if f}
                _absent = sorted(
                    base
                    for base in short_circuit_verify.run_claimed_edited_paths(new_msgs)
                    if base not in _on_branch
                )
                log.warning(
                    "%s: claimed edit(s) absent from the branch diff vs %s (%s), "
                    "but the branch carries %d changed file(s) across real "
                    "commits — proceeding to review rather than blocking",
                    ticket.id,
                    _target,
                    ", ".join(_absent) or "none",
                    len(_branch_changed),
                )
            if (
                edit_tools_rs
                and not _already_landed
                and not _branch_changed
                and (cls._edits_formatter_reverted(repo_dir, new_msgs) is not True)
            ):
                tool_list = ", ".join(edit_tools_rs)
                diag = (
                    f"{summary or 'Agent finished without producing file edits.'}\n\n"
                    "[Diagnostic] implement produced no new working-tree changes "
                    "on a resuming run that already has prior commits, but the "
                    f"agent invoked file-mutating tools ({tool_list}) and "
                    "replaying those edits + formatting still produced a real "
                    "change (or could not be verified).  Blocking for inspection."
                )
                cls._finalize(
                    ctx,
                    ticket,
                    repo_dir,
                    branch,
                    diag,
                    ok=False,
                    reference_files=ref_files,
                    extra_roots=extra_roots,
                )
                return _SinglePassResult(
                    next_action="return",
                    outcome=Outcome(
                        State.BLOCKED,
                        "edit-claim contradiction "
                        "(empty diff after edit calls on resume-with-ahead)",
                    ),
                )
            # Branch is ahead of target with a clean working tree on a
            # resuming run — prior passes already committed the work.
            # Instead of short-circuiting to DONE (which strands the
            # WIP commits — they never reach deliver, no PR is opened),
            # return None to let the normal flow proceed through
            # _verify_repo_changes → CODE_REVIEW → deliver. The deliver
            # stage will detect the ahead-of-target commits, push, and
            # create the PR.
            log.info(
                "%s: clean working tree on resuming run with branch ahead — "
                "proceeding to deliver (not DONE; WIP commits need to land)",
                ticket.id,
            )
            return None
        return None

    @classmethod
    def _run_single_implement_pass(
        cls,
        ctx: StageContext,
        ticket: Ticket,
        repo_dir: Path,
        branch: str,
        settings,
        ic: _ImplementContext,
        attempt: int,
        max_iters: int,
        resume_history: list[Any] | None,
        resuming: bool,
        extra_roots: list[Path] | None = None,
    ) -> _SinglePassResult:
        """Run one iteration of the fix loop: agent → guardrail → test gate."""
        ws = ctx.service.workspace(ticket)
        memory_board_id = cls._memory_board_id(ctx, ticket)

        language_instructions = cls._resolve_language_instructions(
            ctx,
            ticket,
            settings,
        )
        target = target_branch_for(settings, ctx.repo_config)
        agent_level = cls._select_agent_level(ic, settings, repo_dir, target)

        # Trivial config-only additions bypass the LLM coordinator entirely.
        if agent_level == -2:
            return cls._handle_trivial_config_change(
                ctx,
                ticket,
                repo_dir,
                branch,
                settings,
                ic,
                target,
                extra_roots,
            )

        # Rename-only changes bypass the LLM coordinator entirely.
        if agent_level == 0:
            return cls._handle_rename_only_change(
                ctx,
                ticket,
                repo_dir,
                branch,
                settings,
                ic,
                target,
                extra_roots,
            )

        # Spec-exact-code tickets bypass the LLM coordinator entirely.
        if agent_level == -1:
            return cls._handle_spec_exact_edits(
                ctx,
                ticket,
                repo_dir,
                branch,
                settings,
                ic,
                target,
                extra_roots,
            )

        head_before = git_ops.head_sha(repo_dir)

        agent_result = cls._invoke_implement_agent(
            ctx,
            ticket,
            repo_dir,
            branch,
            settings,
            ic,
            language_instructions,
            agent_level,
            resume_history,
            extra_roots,
            memory_board_id,
            ws,
            target_branch=target,
        )
        if agent_result.failure is not None:
            return agent_result.failure
        (
            summary,
            ref_files,
            updated_memory,
            conv_state,
            new_msgs,
            no_change_needed,
            no_change_rationale,
        ) = agent_result.success

        pause = cls._maybe_handle_pause(
            ctx,
            ticket,
            repo_dir,
            branch,
            ws,
            summary,
            ref_files,
            conv_state,
            new_msgs,
            extra_roots,
        )
        if pause is not None:
            return pause

        updated_ref_files, updated_prev_summary = cls._persist_pass_artifacts(
            ws,
            ticket,
            ic,
            summary,
            ref_files,
            updated_memory,
            settings,
            memory_board_id,
        )

        # --- post-summary verification gate ---
        # The LLM's free-text summary can claim artifacts that never landed
        # on disk (the changelog-fragment claim is the recurring case).
        # Verify those claims before accepting the pass; on failure re-prompt
        # once with a specific diagnosis instead of advancing with a false
        # summary.
        verification_result = cls._run_summary_verification(
            ticket=ticket,
            repo_dir=repo_dir,
            summary=summary,
            ic=ic,
            updated_ref_files=updated_ref_files,
            updated_prev_summary=updated_prev_summary,
            new_msgs=new_msgs,
            target_branch=target,
        )
        if verification_result is not None:
            return verification_result

        guardrail = cls._run_scope_guardrail(
            ctx,
            ticket,
            repo_dir,
            branch,
            summary,
            ref_files,
            ic.file_map,
            settings,
            ic.spec,
            ic.feedback,
        )
        if guardrail.action == "return":
            return _SinglePassResult(
                next_action="return",
                outcome=guardrail.outcome,
            )

        new_file_map = (
            guardrail.file_map if guardrail.file_map is not None else ic.file_map
        )
        new_feedback = (
            guardrail.feedback
            if guardrail.action in ("continue", "skip_iteration")
            else ic.feedback
        )
        new_ic = _ImplementContext(
            spec=ic.spec,
            memory_text=ic.memory_text,
            reference_files=updated_ref_files,
            file_map=new_file_map,
            feedback=new_feedback,
            previous_attempt_summary=updated_prev_summary,
            open_thread_ids=ic.open_thread_ids,
        )
        if guardrail.action == "continue":
            return _SinglePassResult(
                next_action="retry",
                feedback=None,
                ic=new_ic,
                new_msgs=new_msgs,
            )

        # guardrail.action == "skip_iteration" — fall through to test gate.
        return cls._evaluate_test_results(
            ctx,
            ticket,
            repo_dir,
            branch,
            settings,
            ic,
            new_ic,
            summary,
            ref_files,
            new_msgs,
            no_change_needed,
            no_change_rationale,
            resuming,
            attempt,
            max_iters,
            extra_roots,
            head_before=head_before,
        )

    # ------------------------------------------------------------------
    # prerequisite gate
    # ------------------------------------------------------------------
