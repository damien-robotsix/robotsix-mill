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
import re as _re
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
    "_CI_POLL_REFRESH_SHA",
    "_MERGE_MAX_RETRIES",
    "_MERGE_RETRY_COUNTER",
    "_REBASE_DROPPED",
    "_merge_rejection_outcome",
    "_read_counter",
    "_read_dropped_files",
    "_reconcile_with_remote_pr",
    "_refresh_branch_for_ci",
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


def _load_pr_urls(ws_artifacts_dir: Path) -> list[dict] | None:
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


def _repo_config_for_entry(entry: dict) -> RepoConfig:
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


def _build_failing_summary(
    failing: list[dict],
    log_text: str = "",
    alerts: list[dict] | None = None,
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


def _duplicate_changelog_fragments(
    repo_dir: str | None, target_branch: str
) -> set[str]:
    """Return the set of towncrier issue keys (ticket ids) that have MORE
    THAN ONE changelog fragment added by the PR branch vs origin/<target>.

    Empty set ⇒ no duplicates (allow merge). Best-effort: on any tooling
    error or when the repo has no ``[tool.towncrier]`` config, return
    ``set()``.

    Towncrier fragment files are named ``<issue>.<category>[.<counter>].md``
    and reside directly in the directory configured by
    ``[tool.towncrier].directory`` (default ``changes``). The dedup key is
    everything before the **first** dot — ``Path(name).name.split('.', 1)[0]``.
    """
    if repo_dir is None:
        return set()

    repo_path = Path(repo_dir)

    # -- resolve fragment directory from pyproject.toml -------------------
    pp = repo_path / "pyproject.toml"
    if not pp.is_file():
        return set()

    try:
        import tomllib

        data = tomllib.loads(pp.read_text(encoding="utf-8"))
    except Exception:
        log.warning(
            "towncrier gate: failed to parse pyproject.toml — allowing merge (best-effort)"
        )
        return set()

    tc = (data.get("tool", {}) or {}).get("towncrier")
    if not tc:
        return set()

    directory = str(tc.get("directory") or "changes").rstrip("/")

    # -- collect added paths from the PR branch ---------------------------
    try:
        added = git_ops.added_files(repo_path, target_branch)
    except Exception:
        log.warning(
            "towncrier gate: git added_files failed — allowing merge (best-effort)"
        )
        return set()

    # -- count fragment issue keys ----------------------------------------
    counts: dict[str, int] = {}
    for path_str in added:
        p = Path(path_str)
        if str(p.parent) != directory:
            continue
        name = p.name
        # Must match towncrier fragment naming: >=2 dot-separated segments,
        # ends with .md, does not start with . or _ (excludes .gitkeep,
        # _template.md).
        if not name.endswith(".md"):
            continue
        if name.startswith((".", "_")):
            continue
        if "." not in name:
            continue
        key = name.split(".", 1)[0]  # everything before the first dot
        counts[key] = counts.get(key, 0) + 1

    return {k for k, v in counts.items() if v > 1}


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


# ---------------------------------------------------------------------------
# CHANGELOG lint (merge-stage advisory, not a gate)
# ---------------------------------------------------------------------------


def _lint_changelog(repo_dir: str | None) -> list[dict[str, str]]:
    """Parse ``CHANGELOG.md`` and return advisory warnings.

    Returns a list of dicts, each with ``severity`` (``"warn"`` or
    ``"info"``) and ``message`` (human-readable).  Checks:

    * Empty bullets (bare ``"- "`` with no body).  Uses ``.lstrip()``
      (not ``.strip()``) so a bullet whose body is only whitespace is
      detected — ``.strip()`` would collapse it to ``"-"`` and miss it.
    * Overlapping entries — two distinct bullets that share a code
      identifier (underscore-containing token ≥ 8 chars, no dots).

    Returns an empty list when *repo_dir* is ``None`` or ``CHANGELOG.md``
    does not exist.
    """
    if repo_dir is None:
        return []

    changelog_path = Path(repo_dir) / "CHANGELOG.md"
    if not changelog_path.is_file():
        return []

    content = changelog_path.read_text(encoding="utf-8")

    # -- locate the ## 0.0.0 (unreleased) section -----------------------
    unreleased_start = content.find("## 0.0.0 (unreleased)")
    if unreleased_start < 0:
        return []

    # Find the next ## section header (not ### sub-headings)
    rest = content[unreleased_start + len("## 0.0.0 (unreleased)") :]
    next_section = _re.search(r"\n## [^#]", rest)
    if next_section is not None:
        rest = rest[: next_section.start()]
    unreleased_lines = rest.split("\n")

    # -- parse bullets --------------------------------------------------
    bullets: list[tuple[int, str, list[str]]] = []
    # Each entry: (line_number, body_text, continuation_lines)
    current: tuple[int, str, list[str]] | None = None

    for line in unreleased_lines:
        if line.startswith("- "):
            if current is not None:
                bullets.append(current)
            body = line[2:]  # everything after "- "
            # Use the original line's position relative to the file for
            # diagnostics — approximate by counting from the section header.
            # We don't have exact line numbers; we'll use the bullet index.
            current = (len(bullets) + 1, body, [])
        elif line.startswith("  ") and current is not None:
            # Continuation line — must be indented with exactly 2 spaces.
            # Only capture if it's a true continuation (not an empty or
            # whitespace-only line that could be inter-bullet spacing).
            stripped = line[2:]
            if (
                stripped or current[2]
            ):  # keep empty continuation lines if preceded by other continuations
                current[2].append(stripped)
        elif line == "":
            # Blank line resets continuation tracking.
            pass

    if current is not None:
        bullets.append(current)

    warnings: list[dict[str, str]] = []

    # -- detect empty bullets -------------------------------------------
    # Use .lstrip() so a bullet whose body is only whitespace
    # ("- " or "-  ") is caught — .strip() would collapse "- " to "-"
    # and miss it.
    for idx, body, cont_lines in bullets:
        if not body.lstrip() and not cont_lines:
            warnings.append(
                {
                    "severity": "warn",
                    "message": f"CHANGELOG.md: empty bullet at position {idx} "
                    f'(bare "- " with no body)',
                }
            )

    # -- detect overlapping entries (shared code identifiers) -----------
    # A "code identifier" is an underscore-containing token, ≥ 8 chars,
    # no dots — this catches function/class/variable names like
    # ``implement_pass_timeout``, ``sandbox_op_timeout``, etc. while
    # ignoring plain English phrases and short acronyms.
    #
    # Two-pass: first collect which bullets mention each identifier,
    # then only flag identifiers that appear in EXACTLY two bullets.
    # Identifiers appearing in 3+ bullets are common terms (e.g. the
    # package name ``robotsix_mill``, generic parameter names like
    # ``ticket_id``) — not editorial overlaps.
    _IDENT_RE = _re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]{7,})(?![.\w])")

    # identifier → list of bullet indices
    ident_bullets: dict[str, list[int]] = {}
    for idx, body, cont_lines in bullets:
        full_text = body + " " + " ".join(cont_lines)
        idents = {m.group(1) for m in _IDENT_RE.finditer(full_text)}
        for ident in idents:
            if "_" not in ident:
                continue  # plain English word, not a code identifier
            ident_bullets.setdefault(ident, []).append(idx)

    # Flag each identifier that appears in exactly two bullets.
    # Collect all identifiers per pair so the warning lists every shared term.
    pair_idents: dict[tuple[int, int], list[str]] = {}
    for ident, idxs in ident_bullets.items():
        if len(idxs) == 2:
            pair = (min(idxs), max(idxs))
            pair_idents.setdefault(pair, []).append(ident)

    for (a, b), shared in sorted(pair_idents.items()):
        id_list = "`, `".join(shared)
        warnings.append(
            {
                "severity": "warn",
                "message": (
                    f"CHANGELOG.md entries at positions {a} and "
                    f"{b} both mention `{id_list}` — possible overlap"
                ),
            }
        )

    return warnings


def _changelog_warnings_for_ticket(
    repo_dir: str | None, ticket_id: str
) -> list[dict[str, str]]:
    """Filter ``_lint_changelog`` warnings for *ticket_id* and add a
    missing-entry advisory when the ticket has no CHANGELOG bullet.

    Returns a list of dicts with ``severity`` (``"warn"`` or ``"info"``)
    and ``message``.  The ``"info"`` missing-entry advisory is only
    appended when ``CHANGELOG.md`` exists and the ticket id string does
    not appear anywhere in the ``## 0.0.0 (unreleased)`` section.
    """
    all_warnings = _lint_changelog(repo_dir)

    if repo_dir is not None:
        changelog_path = Path(repo_dir) / "CHANGELOG.md"
        if changelog_path.is_file():
            content = changelog_path.read_text(encoding="utf-8")
            unreleased_start = content.find("## 0.0.0 (unreleased)")
            if unreleased_start >= 0:
                rest = content[unreleased_start:]
                next_section = _re.search(r"\n## [^#]", rest)
                unreleased = rest[: next_section.start()] if next_section else rest
                if ticket_id not in unreleased:
                    all_warnings.append(
                        {
                            "severity": "info",
                            "message": (
                                f"No CHANGELOG entry found for ticket "
                                f"{ticket_id} — consider adding one"
                            ),
                        }
                    )

    return all_warnings
