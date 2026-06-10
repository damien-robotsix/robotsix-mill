"""File-system and git-state operations for the implement stage.

Holds the binary-artifact detection helpers, the internal dataclasses
shared across the implement loop, and :class:`FileOperationsMixin` — the
clone/branch, change-detection, and artifact-persistence staticmethods
mixed into :class:`ImplementStage` (assembled in ``phase_coordinator``).
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from ...core.models import Ticket
from ...core.states import State
from ...forge.auth import _resolve_remote_url, github_token
from ...runners.pass_runner import persist_memory
from ...vcs import git_ops
from .. import short_circuit_verify
from ..base import Outcome, StageContext
from ..pause import check_for_pause, save_conversation_state

if TYPE_CHECKING:
    from .phase_coordinator import ImplementStage

log = logging.getLogger("robotsix_mill.stages.implement")

# --- binary-artifact detection --------------------------------------------

BINARY_ARTIFACT_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".db",
        ".sqlite",
        ".sqlite3",
        ".pyc",
        ".so",
        ".dylib",
        ".dll",
        ".o",
        ".a",
        ".bin",
        ".exe",
    }
)


def _is_binary_artifact(repo_dir: Path, path: str, target_branch: str) -> bool:
    """Return True if *path* is a binary artifact.

    Uses two orthogonal signals; either is sufficient:

    1. **Extension-based**: the path suffix matches a known binary
       extension (``.db``, ``.pyc``, ``.so``, …).
    2. **Git-based**: ``git diff --numstat origin/<target> -- <path>``
       returns ``-\t-\t<path>`` — the canonical binary marker.
    """
    # Extension-based check (fast path).
    suffix = Path(path).suffix.lower()
    if suffix in BINARY_ARTIFACT_EXTENSIONS:
        return True

    # Git-based check for misnamed binaries.
    try:
        numstat = subprocess.run(
            [
                "git",
                "-C",
                str(repo_dir),
                "diff",
                "--numstat",
                f"origin/{target_branch}",
                "--",
                path,
            ],
            capture_output=True,
            text=True,
        ).stdout.strip()
        if numstat:
            parts = numstat.split("\t")
            if len(parts) >= 2 and parts[0] == "-" and parts[1] == "-":
                return True
    except subprocess.CalledProcessError:
        pass

    return False


@dataclass
class _ImplementContext:
    """Artifact bundle loaded once before the fix loop starts."""

    spec: str
    memory_text: str
    reference_files: list | None
    file_map: set[str] | None
    feedback: str | None
    previous_attempt_summary: str | None
    open_thread_ids: set[int] | None = None


@dataclass
class _ScopeGuardrailResult:
    """Returned by :meth:`_run_scope_guardrail`."""

    action: Literal["continue", "skip_iteration", "return"]
    outcome: Outcome | None = None
    file_map: set[str] | None = None
    feedback: str | None = None


@dataclass
class _SinglePassResult:
    """Returned by :meth:`_run_single_implement_pass`."""

    next_action: Literal["proceed", "retry", "escalate", "return", "pause", "skip"]
    outcome: Outcome | None = None
    feedback: str | None = None
    ic: _ImplementContext | None = None


@dataclass
class _AgentRunOutcome:
    """Result of the agent invocation phase.

    Exactly one of ``success`` / ``failure`` is non-None.  ``success``
    holds the 7-tuple returned by ``coding.run_implement_agent``
    (summary, ref_files, updated_memory, conv_state, new_msgs,
    no_change_needed, no_change_rationale); ``failure`` holds the
    ``_SinglePassResult`` the orchestrator should return when the agent
    call raised a caught error.  Used only inside ``implement.py`` to
    let the orchestrator early-return cleanly without leaking the
    dual-path complexity.
    """

    success: tuple | None = None
    failure: _SinglePassResult | None = None


class FileOperationsMixin:
    """File-system / git-state staticmethods mixed into :class:`ImplementStage`."""

    @staticmethod
    def _clone_and_branch(ctx, ticket, settings):
        ws = ctx.service.workspace(ticket)
        repo_dir = ws.dir / "repo"
        branch = f"{settings.branch_prefix}{ticket.id}"
        remote_url = _resolve_remote_url(settings, ctx.repo_config)

        # Resume iff a prior run left this ticket's clone + branch behind.
        resuming = (repo_dir / ".git").exists() and git_ops.branch_exists(
            repo_dir, branch
        )
        if resuming:
            git_ops.checkout(repo_dir, branch)
        else:
            if repo_dir.exists():
                shutil.rmtree(repo_dir)
            try:
                try:
                    token = github_token(settings, repo_config=ctx.repo_config)
                except RuntimeError:
                    token = None
                git_ops.clone(
                    remote_url,
                    repo_dir,
                    settings.forge_target_branch,
                    token,
                )
            except subprocess.CalledProcessError as e:
                return Outcome(State.BLOCKED, f"clone failed: {e.stderr[:300]}")
            git_ops.create_branch(repo_dir, branch)

        # Refresh against current origin/<target> so the agent never
        # edits stale source — a branch based on even slightly outdated
        # origin/<target> can silently revert newer commits.
        # Pass a freshly minted token so try_rebase_onto's fetch
        # doesn't fall back to origin's stored (and likely expired)
        # GitHub App token — see git_ops.try_rebase_onto for the full
        # rationale. Token resolution can raise when the forge is
        # unconfigured (tests, file:// remotes); fall back to no token
        # and let try_rebase_onto use origin as-is.
        try:
            _rebase_token = github_token(
                settings,
                repo_config=ctx.repo_config,
            )
        except Exception:
            _rebase_token = None
        if not git_ops.try_rebase_onto(
            repo_dir,
            settings.forge_target_branch,
            remote_url=_resolve_remote_url(settings, ctx.repo_config),
            token=_rebase_token,
        ):
            return Outcome(
                State.REBASING,
                f"rebase onto origin/{settings.forge_target_branch} "
                "failed — handing to rebase agent",
            )

        # Hard invariant: NEVER run the agent / sandbox without a
        # materialized clone.
        if not (repo_dir / ".git").exists():
            log.warning(
                "%s: clone missing before agent run — re-cloning",
                ticket.id,
            )
            if repo_dir.exists():
                shutil.rmtree(repo_dir, ignore_errors=True)
            try:
                try:
                    token = github_token(settings, repo_config=ctx.repo_config)
                except RuntimeError:
                    token = None
                git_ops.clone(
                    remote_url,
                    repo_dir,
                    settings.forge_target_branch,
                    token,
                )
                git_ops.create_branch(repo_dir, branch)
            except subprocess.CalledProcessError as e:
                return Outcome(
                    State.BLOCKED,
                    "repo clone missing and re-clone failed — "
                    f"resumable: {(e.stderr or '')[:200]}",
                )
        ctx.service.set_branch(ticket.id, branch)
        return (repo_dir, branch, resuming)

    @staticmethod
    def _any_repo_has_changes(repo_dir: Path, extra_roots: list[Path] | None) -> bool:
        """Return True if any repo has uncommitted changes or is ahead of main.

        Used by the two exit-path guards so multi-repo tickets don't
        misroute to DONE/BLOCKED when only the primary repo was checked.
        """
        if git_ops.has_changes(repo_dir) or git_ops.branch_is_ahead_of_main(repo_dir):
            return True
        if extra_roots:
            for repo_path in extra_roots:
                if repo_path == repo_dir:
                    continue
                if git_ops.has_changes(repo_path) or git_ops.branch_is_ahead_of_main(
                    repo_path
                ):
                    return True
        return False

    @staticmethod
    def _claimed_gitignored_edits(
        repo_dir: Path, new_messages: bytes | str | None
    ) -> list[str]:
        """Repo-relative paths this run's edit tool-calls targeted that exist
        on disk but are gitignored (so the diff stays empty).

        Normalizes Claude-SDK absolute paths to repo-relative; paths outside
        the clone are skipped (a different failure mode with its own guard).
        Fail-open: errors yield ``[]`` — this only ENRICHES the blocked note,
        it never decides the outcome.
        """
        try:
            raw_paths = short_circuit_verify.run_claimed_edited_rawpaths(new_messages)
            rels: list[str] = []
            seen: set[str] = set()
            for raw in raw_paths:
                p = Path(raw)
                if p.is_absolute():
                    try:
                        p = p.relative_to(repo_dir)
                    except ValueError:
                        continue  # outside the clone
                rel = str(p)
                # Dedupe AFTER normalization — the same file can be claimed
                # both repo-relative (mill tools) and absolute (Claude SDK).
                if rel not in seen:
                    seen.add(rel)
                    rels.append(rel)
            return git_ops.ignored_existing_paths(repo_dir, rels)
        except Exception:  # noqa: BLE001 — diagnostic enrichment only
            log.warning(
                "gitignored-edit detection failed; emitting plain note",
                exc_info=True,
            )
            return []

    @staticmethod
    def _persist_pass_artifacts(
        ws,
        ticket: Ticket,
        ic: _ImplementContext,
        summary: str,
        ref_files: list[str] | None,
        updated_memory: str,
        settings,
        memory_board_id: str,
    ) -> tuple[list | None, str | None]:
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

    @staticmethod
    def _maybe_handle_pause(
        ctx: StageContext,
        ticket: Ticket,
        repo_dir: Path,
        branch: str,
        ws,
        summary: str,
        ref_files: list[str] | None,
        conv_state,
        new_msgs,
        extra_roots: list[Path] | None,
    ) -> _SinglePassResult | None:
        """Persist conv_state and route to AWAITING_USER_REPLY on pause."""
        if not check_for_pause(new_msgs):
            return None
        save_conversation_state(ws, conv_state, "implement")
        ImplementStage._finalize(
            ctx,
            ticket,
            repo_dir,
            branch,
            summary or "paused",
            ok=False,
            reference_files=ref_files,
            extra_roots=extra_roots,
        )
        ctx.service.transition(
            ticket.id,
            State.AWAITING_USER_REPLY,
            note="paused — agent asked a clarifying question",
        )
        log.info(
            "%s: paused implement — agent invoked ask_user",
            ticket.id,
        )
        return _SinglePassResult(
            next_action="pause",
            outcome=Outcome(State.AWAITING_USER_REPLY),
        )
