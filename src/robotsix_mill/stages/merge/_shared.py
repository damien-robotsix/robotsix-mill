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
from ...core.workspace import (
    read_counter as _read_counter,
    write_counter as _write_counter,
)
from ...vcs import git_ops

__all__ = ["_read_counter", "_write_counter"]

log = logging.getLogger("robotsix_mill.stages.merge")

_REBASE_COUNTER = "rebase_attempts.txt"
_MERGE_REASON = "merge_reason.txt"
_REV_REV_COUNTER = "review_revision_attempts.txt"
_AUTO_FIX_CYCLES = "auto_fix_cycles.txt"
_LAST_AUTO_FIX_STAGE = "last_auto_fix_stage.txt"
_PING_PONG_COUNT = "ping_pong_count.txt"


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
    when the manifest is malformed)."""
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
        if name.startswith(".") or name.startswith("_"):
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
    ``conclusion`` is ``"failure"`` (blank names are dropped)."""
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
