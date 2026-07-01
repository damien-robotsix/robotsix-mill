"""Changelog-autofill runner — commits changelog entries for bot PRs.

A ``schedule_only`` periodic runner that targets repos with changelog CI
enforcement.  For each open PR whose CI shows a failing ``changelog``
check (and which hasn't already been touched), it formats an entry from
the PR title, clones the branch, inserts the entry into CHANGELOG.md,
and pushes with a lease so concurrent edits on the PR branch are
detected.

No LLM call — entries are pure ``- {title}.`` formatting.  Good enough
for dependabot/renovate PRs; humans can edit.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from typing import Any

from robotsix_mill.agents.changelog_tool import _insert_changelog_entry
from robotsix_mill.config.repos import RepoConfig
from robotsix_mill.config.settings import Settings
from robotsix_mill.forge.auth import github_token
from robotsix_mill.forge.base import Forge, get_forge
from robotsix_mill.vcs import git_ops

log = logging.getLogger(__name__)


def run_changelog_autofill_pass(
    *,
    session_id: str,
    repo_config: RepoConfig | None = None,
) -> None:
    """Execute one changelog-autofill pass for *repo_config*.

    Returns immediately when *repo_config* is ``None``.
    """
    if repo_config is None:
        return

    settings = Settings()
    forge = get_forge(settings, repo_config=repo_config)
    prs = forge.list_open_prs()
    if not prs:
        return

    repo_url = repo_config.forge_remote_url
    if not repo_url:
        log.warning(
            "changelog_autofill: no forge_remote_url for %s — skipping",
            repo_config.repo_id,
        )
        return

    try:
        token = github_token(settings, repo_config=repo_config)
    except RuntimeError as exc:
        log.warning(
            "changelog_autofill: no forge token for %s — skipping (%s)",
            repo_config.repo_id,
            exc,
        )
        return

    log.info(
        "changelog_autofill: scanning %d open PR(s) on %s",
        len(prs),
        repo_config.repo_id,
    )

    for p in prs:
        try:
            _process_pr(forge, p, repo_url, token)
        except Exception:
            log.exception(
                "changelog_autofill: error processing PR #%d on %s",
                p.get("number", "?"),
                repo_config.repo_id,
            )


def _process_pr(
    forge: Forge,
    p: dict[str, Any],
    repo_url: str,
    token: str,
) -> None:
    """Process a single PR: gate checks, format entry, clone + commit + push."""
    pr_number = p["number"]
    branch = p["branch"]
    title = p.get("title", "")

    # --- Label gate ---
    labels = forge.get_pr_labels(pr_number)
    if "Skip-Changelog" in labels:
        log.debug(
            "changelog_autofill: PR #%d has Skip-Changelog label — skip",
            pr_number,
        )
        return

    # --- CI gate ---
    status = forge.check_status(source_branch=branch)
    if status is None:
        log.debug(
            "changelog_autofill: PR #%d has no CI status — skip",
            pr_number,
        )
        return

    failing_names = {f["name"].casefold() for f in status.get("failing", [])}
    if "changelog" not in failing_names:
        log.debug(
            "changelog_autofill: PR #%d has no failing 'changelog' "
            "check (failing=%s) — skip",
            pr_number,
            failing_names,
        )
        return

    # --- Idempotency gate ---
    changed = forge.pr_files(source_branch=branch)
    if any(f["path"] == "CHANGELOG.md" for f in changed):
        log.debug(
            "changelog_autofill: PR #%d already has CHANGELOG.md in diff — skip",
            pr_number,
        )
        return

    # --- Format entry ---
    entry = _format_entry(title)

    # --- Clone, insert, commit, push ---
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        git_ops.clone(repo_url, tmp_path, branch, token)
        _insert_changelog_entry(tmp_path, entry)
        committed = git_ops.commit_file(
            tmp_path,
            "CHANGELOG.md",
            f"chore: add changelog entry for #{pr_number}",
        )
        if not committed:
            log.debug(
                "changelog_autofill: commit_file was no-op for PR #%d — skip push",
                pr_number,
            )
            return
        git_ops.push_with_lease(tmp_path, branch, repo_url, token)
        log.info(
            "changelog_autofill: committed entry for PR #%d",
            pr_number,
        )


def _format_entry(title: str) -> str:
    """Normalize *title* to a changelog bullet entry.

    Removes leading/trailing whitespace, strips a trailing period (if
    any), then appends exactly one period.  The bullet marker ``"- "``
    is always prepended.
    """
    cleaned = title.strip().rstrip(". ").strip()
    return f"- {cleaned}."
