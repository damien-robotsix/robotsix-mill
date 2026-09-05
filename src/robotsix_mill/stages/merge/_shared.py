"""Shared leaf module for the merge stage package.

Pure leaf (Pattern A): holds every module-level name that more than one
merge submodule needs — constants, helper functions, and the package
``log``. Imports only **outward** (``..base``, stdlib); it must NOT
import any sibling mixin or ``core`` so the package import graph stays
an acyclic DAG.

The ``log`` here is bound to the logger name
``"robotsix_mill.stages.merge"`` so existing
``caplog.at_level(logger="robotsix_mill.stages.merge")`` assertions
keep capturing through the package split.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from ...config import ConfigError, RepoConfig, get_repo_config
from ...core.states import State
from ...core.workspace import (
    read_counter as _read_counter,
)
from ...core.workspace import (
    write_counter as _write_counter,
)
from ...vcs import git_ops
from ..base import Outcome


def _reconcile_with_remote_pr(
    facade: Any,
    repo_dir: str,
    remote_url: str,
    branch: str,
    token: str | None,
    ticket_id: str,
    repo_id: str | None = None,
) -> Outcome | None:
    """Shared reconcile guard: call ``reconcile_with_remote_pr`` and handle results.

    Returns ``Outcome(State.BLOCKED, ...)`` on DIVERGED, logs a warning
    on UNAVAILABLE and returns ``None`` (caller proceeds), and returns
    ``None`` on SYNCED (fall through).  When *repo_id* is provided, it is
    prepended to both the DIVERGED message and the UNAVAILABLE log line
    so the multi-repo callers get per-repo attribution.
    """
    reconciled = facade.git_ops.reconcile_with_remote_pr(
        Path(repo_dir), remote_url, branch, token
    )
    if reconciled is facade.git_ops.ReconcileResult.DIVERGED:
        msg = (
            "PR branch diverged from the workspace clone (a human likely pushed to "
            "it) — manual reconciliation required. The mill refuses to "
            "force-push here: push_with_lease cannot protect this case "
            "because reconcile's own fetch already advanced the tracking "
            "ref to the foreign commit, so a lease push would pass its "
            "compare-and-swap and SILENTLY OVERWRITE that commit."
        )
        return Outcome(State.BLOCKED, f"{repo_id}: {msg}" if repo_id else msg)
    if reconciled is facade.git_ops.ReconcileResult.UNAVAILABLE:
        if repo_id:
            log.warning(
                "%s: %s: could not reach the remote PR branch to reconcile "
                "— proceeding; push_with_lease backstops a stale push",
                ticket_id,
                repo_id,
            )
        else:
            log.warning(
                "%s: could not reach the remote PR branch to reconcile "
                "— proceeding; push_with_lease backstops a stale push",
                ticket_id,
            )
    return None


def _merge_rejection_outcome(
    ticket_id: str,
    artifacts_dir: Path,
    result: dict[str, Any],
    *,
    same_state: State,
) -> Outcome:
    """Translate a failed ``merge_pr`` result into the right outcome.

    A rejection the forge marked ``retryable`` is not, on its own,
    evidence that the merge will never succeed: GitHub answers 405 both
    for "merge commits are not allowed here" and for "required status
    check X is expected", the latter simply meaning a required gate has
    not reported yet. Blocking on the second reading strands a ticket
    whose PR goes green seconds later — which is exactly how seven
    robotsix-llmio tickets died at the finish line on 2026-08-01/02,
    each merge fired 24-68s before CodeQL and ``ci / tests`` reported.

    So a retryable rejection returns *same_state*, leaving the ticket in
    the merge poll to try again, up to ``_MERGE_MAX_RETRIES`` passes.
    Past that — and for any rejection the forge did not mark retryable —
    it still fails closed to ``BLOCKED``, now carrying the forge's own
    message rather than a guess about branch protection.
    """
    reason = result.get("reason", "unknown")
    if not result.get("retryable"):
        return Outcome(State.BLOCKED, f"forge merge rejected: {reason}")

    attempts = _read_counter(artifacts_dir / _MERGE_RETRY_COUNTER) + 1
    _write_counter(artifacts_dir / _MERGE_RETRY_COUNTER, attempts)
    if attempts >= _MERGE_MAX_RETRIES:
        return Outcome(
            State.BLOCKED,
            f"forge merge rejected {attempts}x (still retryable, giving up): {reason}",
        )
    log.info(
        "%s: merge rejected (retryable, attempt %d/%d): %s — re-polling",
        ticket_id,
        attempts,
        _MERGE_MAX_RETRIES,
        reason,
    )
    return Outcome(same_state, f"merge not ready yet (attempt {attempts}): {reason}")


__all__ = [
    "_APPROVED_DIFF_HASH",
    "_AUTO_FIX_CYCLES",
    "_CI_POLL_REFRESH_SHA",
    "_EMPTY_ROLLUP_COUNT",
    "_EMPTY_ROLLUP_SELF_HEAL_DONE",
    "_GREEN_UNPROMOTABLE_COUNT",
    "_LAST_AUTO_FIX_STAGE",
    "_MERGE_MAX_RETRIES",
    "_MERGE_RETRY_COUNTER",
    "_PING_PONG_COUNT",
    "_PR_MISSING_COUNT",
    "_REBASE_COUNTER",
    "_REBASE_DROPPED",
    "_REBASE_FROM_STATE",
    "_REBASE_LAST_TS",
    "_ci_truly_green",
    "_is_pr_check_run",
    "_latest_failing_workflows",
    "_merge_rejection_outcome",
    "_next_consecutive",
    "_read_counter",
    "_read_dropped_files",
    "_reconcile_with_remote_pr",
    "_refresh_branch_for_ci",
    "_reset_consecutive",
    "_verify_merge_ancestor",
    "_workspace_repo_dir",
    "_write_counter",
    "_write_dropped_files",
]

log = logging.getLogger("robotsix_mill.stages.merge")

_REBASE_COUNTER = "rebase_attempts.txt"
_REBASE_DROPPED = "rebase_dropped_files.txt"
_MERGE_REASON = "merge_reason.txt"
_REV_REV_COUNTER = "review_revision_attempts.txt"
_AUTO_FIX_CYCLES = "auto_fix_cycles.txt"
_LAST_AUTO_FIX_STAGE = "last_auto_fix_stage.txt"
_PING_PONG_COUNT = "ping_pong_count.txt"
# Consecutive merge polls that saw fully-green CI but a PR the forge still
# refuses to promote. Bounded by ``green_unpromotable_max_polls``.
_GREEN_UNPROMOTABLE_COUNT = "green_unpromotable_polls.txt"
# Consecutive merge polls where CI reports success with zero check runs
# (empty rollup) and mergeable_state is "blocked".  This is the signature
# of a PR whose pull_request event never fired.  Bounded by
# ``empty_rollup_max_polls``.
_EMPTY_ROLLUP_COUNT = "empty_rollup_polls.txt"
# Marker written after a close/reopen self-heal attempt so it runs at most
# once per PR.
_EMPTY_ROLLUP_SELF_HEAL_DONE = "empty_rollup_self_heal_done.txt"
# Consecutive merge polls where the forge reported NO PR for the ticket's
# branch / every pr_urls.json repo.  Bounded by
# ``settings.merge_pr_missing_max_polls`` so a dead lookup (e.g. a
# cross-repo PR polled against the board's own repo) escalates to BLOCKED
# instead of silently re-polling forever.  Reset whenever a PR is found.
_PR_MISSING_COUNT = "pr_missing_polls.txt"
# Bounded retries for a *retryable* forge merge rejection (see
# ``_merge_rejection_outcome``). Small on purpose: the case it exists for
# resolves within a minute or two, so anything that survives this many
# poll passes is a real refusal that a human should look at.
_MERGE_RETRY_COUNTER = "merge_retry_attempts.txt"
_MERGE_MAX_RETRIES = 5
# Head SHA produced by the last CI-refresh push. Bounds the refresh to one
# per branch head so a ticket re-entering the merge poll cannot accumulate
# empty commits. See _refresh_branch_for_ci.
_CI_POLL_REFRESH_SHA = "ci_poll_refresh_sha.txt"
_CI_FIX_MIXIN_REFRESH_SHA = "ci_fix_mixin_refresh_sha.txt"
_REBASE_LAST_TS = "last_rebase_at.txt"
# Records which state the ticket was in before entering REBASING
# (human_mr_approval or waiting_auto_merge).  The rebase handler reads
# it after a successful rebase to route back to the merge loop instead
# of falling through to IMPLEMENT_COMPLETE.
_REBASE_FROM_STATE = "rebase_from_state.txt"
# Hash of PR files (sorted filename + blob SHA pairs) stored when a human
# approves a PR blocked by the sensitive-path gate.  On subsequent
# HUMAN_MR_APPROVAL cycles after a rebase, if the hash matches the
# current PR files, the sensitive-path gate is skipped — the human
# already reviewed an identical diff.
_APPROVED_DIFF_HASH = "approved_diff_hash.txt"


def _ci_truly_green(conclusion: str | None, pr: dict[str, Any]) -> bool:
    """Return True only when CI is genuinely, completely green.

    The merge gate must not promote/auto-merge on a *premature* green: after
    a force-push, the fast checks (e.g. CodeQL) can report success and the
    forge's aggregated ``check_status`` conclusion flips to ``"success"``
    before the slow required gate (``ci / tests``) has even started. Merging
    then reddens the target branch (observed: fbf8/PR#1423).

    GitHub's ``mergeable_state`` is the authoritative combined view:
    ``"clean"`` means mergeable AND every required check passed;
    ``"unstable"`` means mergeable but a non-required status is non-green
    (the required gates passed — the PR IS mergeable). While checks are
    still settling the state is ``"blocked"``/``"behind"``/``"unknown"`` —
    those genuinely mean not-ready. So we require ``conclusion == "success"``
    AND a promotable ``mergeable_state``.

    Other forges (GitLab) omit ``mergeable_state`` (``None``); there we fall
    back to trusting the CI conclusion alone (no regression for them).
    """
    if conclusion != "success":
        return False
    mergeable_state = pr.get("mergeable_state")
    return mergeable_state in (None, "clean", "unstable")


def _load_pr_urls(ws_artifacts_dir: Path) -> list[dict[str, Any]] | None:
    """Read ``pr_urls.json``.

    Returns the list when present + parseable, ``None`` when the file
    is absent (single-repo path), or raises ``ValueError`` on a
    corrupt file so the caller can BLOCK-resumable.

    The schema mirrors what :func:`deliver._write_pr_urls` writes::

        [{"repo_id": str, "branch": str, "url": str}, ...]
    """
    path = ws_artifacts_dir / "pr_urls.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"pr_urls.json could not be parsed: {e}") from e
    if not isinstance(data, list):
        raise ValueError("pr_urls.json is not a JSON list")
    return data


def _repo_config_for_entry(entry: dict[str, Any]) -> RepoConfig:
    """Resolve a per-repo :class:`RepoConfig` from a ``pr_urls.json``
    entry. Propagates :class:`ConfigError` when the ``repo_id`` is
    missing, non-string, empty, or not registered so the caller's
    existing ``except ConfigError`` arm translates to a BLOCKED
    outcome (instead of bubbling a ``KeyError`` from ``entry['repo_id']``
    when the manifest is malformed).
    """
    repo_id = entry.get("repo_id")
    if not isinstance(repo_id, str) or not repo_id:
        raise ConfigError("pr_urls.json entry is missing a non-empty string 'repo_id'")
    return get_repo_config(repo_id)


def _next_consecutive(artifacts_dir: Path, name: str) -> int:
    """Increment and return the consecutive-cycle counter stored at *name*.

    Used by the spin guards (``_PR_MISSING_COUNT``) so a merge poll that
    keeps hitting the SAME unresolvable condition escalates to BLOCKED
    after ``settings.<ceiling>`` cycles instead of re-polling forever.
    """
    count = _read_counter(artifacts_dir / name) + 1
    _write_counter(artifacts_dir / name, count)
    return count


def _reset_consecutive(artifacts_dir: Path, name: str) -> None:
    """Zero the consecutive-cycle counter stored at *name*.

    Call when the condition the counter tracks no longer holds (e.g. a PR
    was found), so only *consecutive* same-reason cycles count.
    """
    _write_counter(artifacts_dir / name, 0)


def _build_failing_summary(
    failing: list[dict[str, Any]],
    log_text: str = "",
    alerts: list[dict[str, Any]] | None = None,
    changed_paths: set[str] | None = None,
) -> str:
    """Markdown summary of failing checks for the CI-fix agent.

    A thin wrapper over ``stages.ci_fix._build_failing_summary`` (imported
    lazily to avoid a module-load cycle) so the multi-repo path renders the
    same job-logs + code-scanning-alert detail as the single-repo path. When
    *changed_paths* is provided the alerts are partitioned against the PR's
    own diff and labelled in-scope / out-of-scope, mirroring the single-repo
    ``ci_fix._build_failure_detail`` path.
    """
    from ..ci_fix_helpers import _build_failing_summary as _ci_fix_summary

    return _ci_fix_summary(failing, log_text, alerts, changed_paths)


def _read_dropped_files(path: Path) -> list[str]:
    """Read the previously-dropped file list from *path*, returning an empty list when absent."""
    try:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return []
        return text.splitlines()
    except FileNotFoundError:
        return []


def _write_dropped_files(path: Path, files: list[str]) -> None:
    """Persist *files* as a newline-delimited list at *path*, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(files) + "\n", encoding="utf-8")


def _read_reason(path) -> set[str]:
    try:
        return set(path.read_text(encoding="utf-8").splitlines())
    except FileNotFoundError:
        return set()


def _write_reason(path, reason: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(reason + "\n")


def _workspace_repo_dir(ctx, ticket) -> str | None:
    """Return the ticket's workspace clone dir, or None if missing."""
    ws = ctx.service.workspace(ticket)
    repo = ws.dir / "repo"
    if not (repo / ".git").exists():
        return None
    return str(repo)


def _refresh_branch_for_ci(
    repo_dir: str,
    branch: str,
    target: str,
    remote_url: str,
    token: str | None,
    ticket_id: str,
    sentinel_path: Path | None = None,
) -> bool:
    """Force a fresh CI run by rebasing onto *target* or pushing an empty commit.

    When the branch is already current (rebase is a no-op) and the remote
    HEAD matches local, a new commit is needed to trigger a fresh
    pull_request CI run — a stale SHA pins old check-runs that can never
    turn green.  Call this BEFORE evaluating CI status so a resume from
    BLOCKED on a transient flake un-sticks in one cycle.

    *sentinel_path* bounds the refresh to **once per branch head**. Without
    it a ticket that keeps re-entering the caller's poll gets a fresh empty
    commit every time: observed 2026-08-02 on ``robotsix-http`` ...-d320,
    three empty commits in 22 seconds from two call sites, because the
    ticket bounced IMPLEMENT_COMPLETE -> FIXING_CI -> IMPLEMENT_COMPLETE and
    each transition re-polled. The sentinel stores the head SHA produced by
    the last refresh, so the next call is a no-op until something real
    pushes and moves the branch on.

    Returns ``True`` when a commit was pushed (new CI run triggered).
    Errors are logged and return ``False`` (caller proceeds with existing
    HEAD).
    """
    repo_path = Path(repo_dir)
    pushed = False

    if sentinel_path is not None:
        try:
            if sentinel_path.exists() and sentinel_path.read_text(
                encoding="utf-8"
            ).strip() == git_ops.head_sha(repo_path):
                log.info(
                    "%s: CI already refreshed for the current head — "
                    "not pushing another empty commit",
                    ticket_id,
                )
                return False
        except Exception:
            log.warning(
                "%s: could not read the CI-refresh sentinel — refreshing",
                ticket_id,
                exc_info=True,
            )

    # 1. Rebase onto target so the branch is current.
    try:
        did_rebase = git_ops.try_rebase_onto(
            repo_path,
            target,
            remote_url=remote_url,
            token=token,
        )
        if did_rebase:
            git_ops.push(repo_path, branch, remote_url, token)
            pushed = True
            log.info(
                "%s: rebased onto %s and pushed before CI scan",
                ticket_id,
                target,
            )
        else:
            log.info(
                "%s: rebase onto %s was a no-op or unnecessary — "
                "proceeding with existing branch HEAD",
                ticket_id,
                target,
            )
    except Exception:
        log.warning(
            "%s: rebase step failed — proceeding with existing branch",
            ticket_id,
            exc_info=True,
        )

    # 2. When the branch HEAD hasn't changed, push an empty commit to
    #    produce a fresh SHA so the forge triggers a new pull_request run.
    #    This un-sticks tickets whose original CI failure was a transient
    #    flake that has since resolved (the old, failing check-runs are
    #    pinned to the stale SHA and can never turn green).
    #    Skipped when the rebase above already pushed: that push produced a
    #    new head SHA and therefore a fresh run, so an empty commit on top
    #    would only invalidate the run we just triggered.
    try:
        if not pushed:
            local_sha = git_ops.head_sha(repo_path)
            remote_sha = git_ops.ls_remote_sha(
                remote_url, f"refs/heads/{branch}", token
            )
            if remote_sha is not None and local_sha == remote_sha:
                git_ops.empty_commit(
                    repo_path,
                    "ci: trigger fresh CI run (no-op commit to un-stick transient failure)",
                )
                git_ops.push(repo_path, branch, remote_url, token)
                pushed = True
                log.info(
                    "%s: pushed empty commit to force fresh CI run "
                    "(branch was already current)",
                    ticket_id,
                )
    except Exception:
        log.warning(
            "%s: empty-commit push failed — proceeding with existing HEAD",
            ticket_id,
            exc_info=True,
        )

    if pushed and sentinel_path is not None:
        # Record the head we just produced so the next poll is a no-op until
        # something real moves the branch on.
        try:
            sentinel_path.parent.mkdir(parents=True, exist_ok=True)
            sentinel_path.write_text(git_ops.head_sha(repo_path), encoding="utf-8")
        except Exception:
            log.warning(
                "%s: could not write the CI-refresh sentinel", ticket_id, exc_info=True
            )

    return pushed


def _verify_merge_ancestor(
    repo_dir: str | None,
    sha: str,
    ticket_id: str,
    target_branch: str = "main",
) -> bool:
    """Verify that commit *sha* is an ancestor of origin/<target_branch>.

    Fetches origin/<target_branch> to ensure the local ref is current,
    then runs ``git merge-base --is-ancestor <sha> origin/<target_branch>``.
    When the direct ancestry check fails (exit 1), falls back to:

    1. Squash-merge detection: greps the origin/<target_branch> log for
       *ticket_id*.
    2. Content-level verification: diffs *sha* against
       origin/<target_branch> and checks whether each changed file on
       origin/<target_branch> contains *ticket_id* (catches squash and
       rebase merges where the log message does not mention the ticket).

    Returns True when the merge is confirmed (ancestor, squash-merge
    found, or content present).  Returns False only when the check runs
    and confirms the commit is NOT on origin/<target_branch>.  When the
    repo is unavailable or a git error occurs, returns True
    (best-effort — do not block the pipeline on transient tooling
    issues).
    """
    if repo_dir is None or not sha:
        # Nothing to verify — best-effort allow.
        return True
    try:
        subprocess.run(
            ["git", "-C", repo_dir, "fetch", "origin", target_branch],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError:
        log.warning(
            "%s: git fetch origin %s failed — allowing merge (best-effort)",
            ticket_id,
            target_branch,
        )
        return True

    result = subprocess.run(
        [
            "git",
            "-C",
            repo_dir,
            "merge-base",
            "--is-ancestor",
            sha,
            f"origin/{target_branch}",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return True  # sha is an ancestor of origin/<target_branch>
    if result.returncode == 1:
        # Not a direct ancestor — maybe it was a squash-merge.
        grep = subprocess.run(
            [
                "git",
                "-C",
                repo_dir,
                "log",
                f"origin/{target_branch}",
                "--oneline",
                "--fixed-strings",
                f"--grep={ticket_id}",
            ],
            capture_output=True,
            text=True,
        )
        if grep.returncode == 0 and grep.stdout.strip():
            log.info(
                "%s: commit %s is not an ancestor of origin/%s, "
                "but a commit referencing this ticket was found on "
                "origin/%s — treating as squash-merged",
                ticket_id,
                sha[:8],
                target_branch,
                target_branch,
            )
            return True

        # Fallback 2: content-level verification — the commit may have
        # landed via squash or rebase without the ticket id in the log
        # message.  Diff the feature commit against origin/<target> and
        # check whether concrete content from the diff is present on
        # origin/<target>.
        try:
            diff_files = subprocess.run(
                [
                    "git",
                    "-C",
                    repo_dir,
                    "diff",
                    "--name-only",
                    f"origin/{target_branch}..{sha}",
                ],
                capture_output=True,
                text=True,
            )
        except Exception:
            log.info(
                "%s: commit %s is NOT an ancestor of origin/%s — merge not confirmed",
                ticket_id,
                sha[:8],
                target_branch,
            )
            return False
        if diff_files.returncode == 0:
            changed = [f for f in diff_files.stdout.strip().split("\n") if f]
            for path in changed:
                try:
                    show = subprocess.run(
                        [
                            "git",
                            "-C",
                            repo_dir,
                            "show",
                            f"origin/{target_branch}:{path}",
                        ],
                        capture_output=True,
                        text=True,
                    )
                except Exception:
                    log.debug(
                        "%s: git show origin/%s:%s failed — skipping content check",
                        ticket_id,
                        target_branch,
                        path,
                    )
                    continue
                if show.returncode == 0 and ticket_id in show.stdout:
                    log.info(
                        "%s: commit %s is not an ancestor of origin/%s, "
                        "but content from the diff was found in "
                        "origin/%s:%s — treating as squash/rebase-merged",
                        ticket_id,
                        sha[:8],
                        target_branch,
                        target_branch,
                        path,
                    )
                    return True

        log.info(
            "%s: commit %s is NOT an ancestor of origin/%s — merge not confirmed",
            ticket_id,
            sha[:8],
            target_branch,
        )
        return False
    # Any other exit code — git error, best-effort allow.
    log.warning(
        "%s: git merge-base --is-ancestor failed for %s — allowing merge (best-effort)",
        ticket_id,
        sha[:8],
    )
    return True


def _latest_failing_workflows(runs: list[dict[str, Any]]) -> set[str]:
    """Reduce a list of workflow-run dicts to the set of currently
    failing workflow names.

    The latest **completed** run per ``workflow_id`` wins (compared by the
    ``created_at`` string), so a later green run supersedes an earlier
    red one for the same workflow (and vice-versa). Runs with a ``None``
    conclusion (in-progress) are ignored entirely — they cannot mask a
    completed failure, preventing false "green" reads during a
    main-CI-in-flight window.

    Returns the names of those latest-per-workflow runs whose
    ``conclusion`` is ``"failure"`` (blank names are dropped).
    """
    latest: dict[Any, dict[str, Any]] = {}
    for run in runs:
        if run.get("conclusion") is None:
            continue  # skip in-progress runs — only completed runs count
        wid = run.get("workflow_id")
        if wid not in latest or run.get("created_at", "") > latest[wid].get(
            "created_at", ""
        ):
            latest[wid] = run
    return {
        (r.get("name") or "").strip()
        for r in latest.values()
        if r.get("conclusion") == "failure" and (r.get("name") or "").strip()
    }


def _is_pr_check_run(run: dict[str, Any]) -> bool:
    """True iff this workflow run is the kind that appears as a check ON the PR.

    Excludes release/tag-only (``on: push: <tags>``), ``workflow_dispatch``-only,
    and scheduled workflows, which never produce a PR check and must not count
    as target-branch debt. A run whose ``event`` key is absent (legacy/test
    data) is treated as a PR check to preserve prior behaviour.
    """
    event = run.get("event")
    if event is None:  # provenance unknown — preserve old behaviour
        return True
    event = event.strip()
    if event in {"pull_request", "pull_request_target", "merge_group"}:
        return True
    if event == "push":
        # Branch push (head_branch set) = PR check; tag push (head_branch null) = release.
        return bool((run.get("head_branch") or "").strip())
    return False  # release, schedule, workflow_dispatch, …
