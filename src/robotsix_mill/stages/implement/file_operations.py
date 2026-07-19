"""File-operations mixin: clone/branch, repo-change and gitignore checks."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ...config import (
    ConfigError,
    Settings,
    effective_target_branch,
    get_repo_config,
    target_branch_for,
)
from ...core.states import State
from ...forge.auth import _resolve_remote_url, github_token
from ...vcs import git_ops
from .. import short_circuit_verify
from ..base import Outcome
from ._base import _ImplementStageBase
from ._shared import (
    log,
)


def _decode_stderr(stderr: str | bytes | None) -> str:
    """Return *stderr* as a decoded string, or ``""`` when *stderr* is ``None``."""
    if stderr is None:
        return ""
    if isinstance(stderr, bytes):
        return stderr.decode("utf-8", errors="replace")
    return stderr


class FileOperationsMixin(_ImplementStageBase):
    """Clone/branch and repo-change inspection for :class:`ImplementStage`."""

    @classmethod
    def _any_repo_has_changes(
        cls,
        repo_dir: Path,
        extra_roots: list[Path] | None,
        target_branch: str = "main",
        settings: Settings | None = None,
    ) -> bool:
        """Return True if any repo has uncommitted changes or is ahead of main.

        Used by the two exit-path guards so multi-repo tickets don't
        misroute to DONE/BLOCKED when only the primary repo was checked.

        When *settings* is provided, each ``extra_roots`` repo resolves
        its own target branch via :func:`target_branch_for` —
        ``target_branch`` is used only for the primary repo (or as a
        fallback when *settings* is ``None``, keeping the single-repo
        callers working).
        """
        if git_ops.has_changes(repo_dir) or git_ops.branch_is_ahead_of_main(
            repo_dir, target_branch
        ):
            return True
        if extra_roots:
            for repo_path in extra_roots:
                if repo_path == repo_dir:
                    continue
                repo_target = target_branch
                if settings is not None:
                    try:
                        rc = get_repo_config(repo_path.name)
                    except ConfigError:
                        rc = None
                    repo_target = target_branch_for(settings, rc)
                if git_ops.has_changes(repo_path) or git_ops.branch_is_ahead_of_main(
                    repo_path, repo_target
                ):
                    return True
        return False

    @staticmethod
    def _resolve_in_repo(repo_dir: Path, raw_path: str) -> Path:
        """Map an edit tool-call's verbatim *raw_path* to an absolute path
        inside *repo_dir*, raising ``ValueError`` if it escapes the clone.

        mill fs tools emit repo-relative paths; the Claude SDK editors emit
        absolute paths (which, for the agent's own clone, live under
        *repo_dir*). Anything resolving outside *repo_dir* is rejected so the
        replay never touches the host filesystem."""
        root = repo_dir.resolve()
        p = Path(raw_path)
        cand = (p if p.is_absolute() else root / p).resolve()
        if cand != root and root not in cand.parents:
            raise ValueError(f"path escapes clone: {raw_path}")
        return cand

    @classmethod
    def _edits_formatter_reverted(
        cls, repo_dir: Path, new_messages: bytes | str | None
    ) -> bool | None:
        """Distinguish a *redundant / formatter-reverted* empty-diff run from a
        *lost-work* one, for the edit-claim guard.

        Re-applies the run's recorded edit tool-calls onto the (clean) working
        tree, runs the project formatter (``ruff format``) on the touched
        Python files, and inspects the net diff:

        * ``True``  — nothing changed: the edits were redundant or the
          formatter normalised them away (e.g. ``except (A, B):`` →
          ``except A, B:`` on a 3.14 target). A genuine no-op — safe to honour
          ``no_change_needed`` and close DONE.
        * ``False`` — a real change survived: the edits represent work that the
          live run lost (reverted / reset / written off-clone). BLOCK.
        * ``None``  — can't decide safely (un-replayable edit kind, a path
          outside the clone, an edit whose ``old_string`` no longer matches, or
          any error). The caller MUST treat this like ``False`` and BLOCK,
          preserving the work-loss guard whenever the check is inapplicable.

        PRECONDITION: the working tree is clean (the caller already verified
        ``_any_repo_has_changes`` is False), so the replay is fully reverted
        afterward to restore a pristine tree.
        """
        ops = short_circuit_verify.extract_replayable_edits(new_messages)
        if not ops:  # None (un-replayable) or [] (no edits) → fail closed
            return None
        try:
            resolved = [(op, cls._resolve_in_repo(repo_dir, op["path"])) for op in ops]
        except ValueError:
            return None  # an edit targets a path outside this clone

        created: list[Path] = []
        py_touched: list[Path] = []
        try:
            for op, abs_path in resolved:
                existed = abs_path.exists()
                kind = op["kind"]
                if kind == "delete":
                    if existed:
                        abs_path.unlink()
                elif kind == "write":
                    if not existed:
                        created.append(abs_path)
                    abs_path.parent.mkdir(parents=True, exist_ok=True)
                    abs_path.write_text(op["content"], encoding="utf-8")
                else:  # edit
                    if not existed:
                        return None  # edit target vanished — can't replay
                    text = abs_path.read_text(encoding="utf-8")
                    if op["old"] not in text:
                        return None  # old_string no longer present — ambiguous
                    abs_path.write_text(
                        text.replace(op["old"], op["new"], 1), encoding="utf-8"
                    )
                if kind != "delete" and abs_path.suffix == ".py":
                    py_touched.append(abs_path)
            if py_touched:
                cls._run_project_formatter(repo_dir, py_touched)
            return not git_ops.has_changes(repo_dir)
        except Exception:  # noqa: BLE001 — any replay failure → fail closed (BLOCK)
            log.warning(
                "_edits_formatter_reverted: replay failed; failing closed",
                exc_info=True,
            )
            return None
        finally:
            # Restore the pristine tree: revert tracked edits + drop the files
            # the replay newly created.
            try:
                subprocess.run(
                    ["git", "checkout", "--", "."],
                    cwd=str(repo_dir),
                    check=False,
                    capture_output=True,
                    timeout=60,
                )
            except OSError, subprocess.SubprocessError:
                log.warning(
                    "_edits_formatter_reverted: worktree reset failed", exc_info=True
                )
            for p in created:
                try:
                    if p.exists():
                        p.unlink()
                except OSError:
                    pass

    @classmethod
    def _build_edit_claim_diagnostic(
        cls,
        *,
        tool_list: str,
        fmt_result: bool | None,
        no_change_summary: str,
        new_msgs: bytes | str | None,
    ) -> str:
        """Build a diagnostic for an edit-claim contradiction.

        *fmt_result* is the return value of :meth:`_edits_formatter_reverted`:
        ``None`` means the replay could not be attempted; ``False`` means
        the replayed edits produced a real change that survived formatting.

        Inspects the raw *new_msgs* to determine WHY the replay could not
        be attempted (missing args, un-replayable tool kind, etc.) so the
        agent gets actionable feedback instead of a generic BLOCKED message.
        """
        ops = short_circuit_verify.extract_replayable_edits(new_msgs)
        if ops is None:
            reason = (
                "the edit tool-calls could not be extracted from the run "
                "transcript — the message payload may be malformed or "
                "contain an un-replayable edit kind (MultiEdit, NotebookEdit)."
            )
        elif not ops:
            reason = (
                "no replayable edit operations were found in the run "
                "transcript — the edit tool-calls may be missing required "
                "arguments (path, old_string, new_string, content)."
            )
        elif fmt_result is False:
            reason = (
                "the edits WERE replayed onto a clean tree and produced a "
                "real diff that survived formatting — the agent's changes "
                "were lost (reverted, workspace reset, or written outside "
                "the clone) before the diff was checked."
            )
        else:
            reason = (
                "the replay could not be completed — an edit target file "
                "may have vanished, the old_string anchor may no longer "
                "match the base file, or a path may escape the clone."
            )
        return (
            f"{no_change_summary}\n\n"
            "[Diagnostic] implement produced an empty diff, but the "
            f"agent invoked file-mutating tools ({tool_list}) during "
            f"the run.  Root cause: {reason}\n\n"
            "Action: review the edit tool-calls in the run transcript. "
            "If the agent used ``edit_file`` with an old_string anchor "
            "that does not exist in the target file, switch to "
            "``write_file`` to write the full file content instead. "
            "If the file path was wrong, correct it.  If the edit was "
            "reverted by a subsequent tool call (e.g. ``git checkout``), "
            "remove that call."
        )

    @staticmethod
    def _run_project_formatter(repo_dir: Path, files: list[Path]) -> None:
        """Run ``ruff format`` on *files* with ``cwd=repo_dir`` so the repo's
        own ``pyproject`` config (e.g. ``target-version``) applies — mirroring
        what CI and the agent run. Best-effort: a missing/failing ruff leaves
        the files as the raw replay wrote them (the caller's diff check then
        treats a surviving raw edit conservatively as work)."""
        rels = [str(f) for f in files]
        try:
            subprocess.run(
                ["ruff", "format", *rels],
                cwd=str(repo_dir),
                check=False,
                capture_output=True,
                timeout=120,
            )
        except OSError, subprocess.SubprocessError:
            log.warning("_run_project_formatter: ruff format failed", exc_info=True)

    @classmethod
    def _claimed_gitignored_edits(
        cls, repo_dir: Path, new_messages: bytes | str | None
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

    @classmethod
    def _clone_and_branch(cls, ctx, ticket, settings):
        ws = ctx.service.workspace(ticket)
        repo_dir = ws.dir / "repo"
        branch = f"{settings.branch_prefix}{ticket.id}"

        # When cross_repo_target is set, clone the fork/target repo so
        # the implement agent sees the target's file system — not the
        # managed repo's.  File-existence checks, config analysis, and
        # scope decisions then correctly target the repo that will
        # receive the PR.
        cross = ctx.repo_config.cross_repo_target
        if cross:
            remote_url = cross.fork_remote_url
            target = cross.base_branch
        else:
            remote_url = _resolve_remote_url(settings, ctx.repo_config)
            target = effective_target_branch(settings, ctx.repo_config)

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
                    target,
                    token,
                )
            except subprocess.CalledProcessError as e:
                from ...runtime.transient_errors import reraise_if_transient

                reraise_if_transient(e)
                stderr_str = _decode_stderr(e.stderr)
                bootstrapped = False
                if "Remote branch" in stderr_str and "not found" in stderr_str:
                    from ...vcs import (
                        _bootstrap_empty_repo,
                        _remote_has_branches,
                    )

                    if not _remote_has_branches(remote_url, token):
                        log.info(
                            "%s: remote has no branches — bootstrapping empty repo",
                            ticket.id,
                        )
                        try:
                            _bootstrap_empty_repo(
                                remote_url,
                                repo_dir,
                                target,
                                token,
                                ctx.repo_config.repo_id,
                            )
                            bootstrapped = True
                        except Exception as bootstrap_err:
                            log.error(
                                "%s: bootstrap failed: %s",
                                ticket.id,
                                bootstrap_err,
                            )
                            return Outcome(
                                State.BLOCKED,
                                "clone failed (empty repo bootstrap also failed): "
                                + git_ops.redact_credentials(str(bootstrap_err))[:250],
                            )
                if not bootstrapped:
                    return Outcome(
                        State.BLOCKED,
                        "clone failed: " + git_ops.redact_credentials(stderr_str)[:300],
                    )
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
            target,
            remote_url=remote_url,
            token=_rebase_token,
        ):
            return Outcome(
                State.REBASING,
                f"rebase onto origin/{target} failed — handing to rebase agent",
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
                    target,
                    token,
                )
                git_ops.create_branch(repo_dir, branch)
            except subprocess.CalledProcessError as e:
                from ...runtime.transient_errors import reraise_if_transient

                reraise_if_transient(e)
                stderr_str = _decode_stderr(e.stderr)
                bootstrapped = False
                if "Remote branch" in stderr_str and "not found" in stderr_str:
                    from ...vcs import (
                        _bootstrap_empty_repo,
                        _remote_has_branches,
                    )

                    if not _remote_has_branches(remote_url, token):
                        log.info(
                            "%s: remote has no branches — bootstrapping empty repo (invariant fallback)",
                            ticket.id,
                        )
                        try:
                            _bootstrap_empty_repo(
                                remote_url,
                                repo_dir,
                                target,
                                token,
                                ctx.repo_config.repo_id,
                            )
                            bootstrapped = True
                            git_ops.create_branch(repo_dir, branch)
                        except Exception as bootstrap_err:
                            log.error(
                                "%s: bootstrap failed (invariant fallback): %s",
                                ticket.id,
                                bootstrap_err,
                            )
                            return Outcome(
                                State.BLOCKED,
                                "repo clone missing, re-clone failed, and bootstrap also failed: "
                                + git_ops.redact_credentials(str(bootstrap_err))[:200],
                            )
                if not bootstrapped:
                    return Outcome(
                        State.BLOCKED,
                        "repo clone missing and re-clone failed — resumable: "
                        + git_ops.redact_credentials(stderr_str)[:200],
                    )
        ctx.service.set_branch(ticket.id, branch)
        return (repo_dir, branch, resuming)
