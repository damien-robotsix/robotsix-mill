"""Repo-description-sync runner — keeps the forge description in sync with README.

This is a schedule-only periodic pass (not an LLM gap-finding agent in the
draft-ticket framework). It:

1. Clones the managed repo.
2. Fetches the current forge description via :meth:`Forge.get_repo_description`.
3. Reads the README and extracts the H1 + first paragraph (cheap deterministic
   pre-processing so the LLM doesn't waste turns on I/O).
4. Calls the LLM agent (using the built-in YAML definition) to judge whether
   the description is stale.
5. Calls :meth:`Forge.update_repo` when the agent recommends an update.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import RepoConfig, Settings, target_branch_for
from ..forge.base import get_forge
from ..vcs import git_ops
from .periodic_runner import _forge_token

log = logging.getLogger("robotsix_mill.repo_description_sync_runner")

# ---------------------------------------------------------------------------
# Deterministic README pre-processing
# ---------------------------------------------------------------------------


def _find_readme(repo_dir: Path) -> Path | None:
    """Return the path to the README file, or None."""
    for name in ("README.md", "README.rst", "README", "readme.md", "Readme.md"):
        cand = repo_dir / name
        if cand.is_file():
            return cand
    return None


def _extract_h1_and_first_paragraph(text: str) -> tuple[str, str]:
    """Return (h1, first_paragraph) from README text.

    h1: the first ``# ...`` line, stripped of the leading ``# ``.
    first_paragraph: the first non-empty, non-heading line of prose after the H1.
    """
    lines = text.splitlines()
    h1 = ""
    paragraph = ""
    found_h1 = False

    for line in lines:
        stripped = line.strip()
        if not found_h1:
            if stripped.startswith("# ") and not stripped.startswith("## "):
                h1 = stripped[2:].strip()
                found_h1 = True
            continue

        # After finding H1, look for first paragraph
        if not stripped or stripped.startswith("#"):
            continue
        paragraph = stripped
        break

    return h1, paragraph


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class RepoDescriptionSyncPassResult:
    updated_memory: str = ""
    drafts_created: list[dict[str, Any]] = field(default_factory=list)
    session_id: str = ""
    summary: str = ""
    description_updated: bool = False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_repo_description_sync_pass(
    session_id: str, repo_config: RepoConfig | None = None
) -> RepoDescriptionSyncPassResult:
    """Execute one repo-description-sync pass for *repo_config*.

    Clones the managed repo, fetches the current forge description,
    calls the LLM for a staleness judgment, and updates the forge
    description when the agent recommends it.

    Args:
        session_id: Langfuse session id from the poll loop.
        repo_config: Per-repo configuration. Required.

    Returns:
        ``RepoDescriptionSyncPassResult`` with summary and
        ``description_updated`` flag.
    """
    settings = Settings()
    if repo_config is None:
        raise ValueError(
            "run_repo_description_sync_pass: repo_config is required — "
            "configure at least one repo in config/repos.yaml."
        )

    forge_remote_url = repo_config.forge_remote_url or settings.forge_remote_url
    if not forge_remote_url:
        log.warning("repo_description_sync: no forge_remote_url — skipping")
        return RepoDescriptionSyncPassResult(
            session_id=session_id,
            summary="skipped: no forge_remote_url configured",
        )

    # ------------------------------------------------------------------
    # 1. Clone the repo
    # ------------------------------------------------------------------
    repo_data_dir = settings.data_dir / repo_config.repo_id
    clone_dir = repo_data_dir / "repo_description_sync_workspace" / "repo"
    if clone_dir.exists():
        shutil.rmtree(clone_dir, ignore_errors=True)
    try:
        git_ops.clone(
            forge_remote_url,
            clone_dir,
            target_branch_for(settings, repo_config),
            _forge_token(settings, repo_config),
        )
    except subprocess.CalledProcessError as e:
        log.warning(
            "repo_description_sync clone failed, skipping: %s",
            (e.stderr or "")[:200],
        )
        return RepoDescriptionSyncPassResult(
            session_id=session_id,
            summary=f"skipped: clone failed ({e!r})",
        )

    # ------------------------------------------------------------------
    # 2. Fetch current forge description
    # ------------------------------------------------------------------
    forge = get_forge(settings, repo_config=repo_config)
    # Parse owner/repo from the forge_remote_url.
    owner, repo_name = _parse_owner_repo(forge_remote_url)
    current_description = forge.get_repo_description(owner=owner, repo=repo_name)
    log.info(
        "repo_description_sync: current description for %s/%s: %r",
        owner,
        repo_name,
        current_description,
    )

    # ------------------------------------------------------------------
    # 3. Read README and extract H1 + first paragraph
    # ------------------------------------------------------------------
    readme_path = _find_readme(clone_dir)
    readme_h1 = ""
    readme_paragraph = ""
    if readme_path is not None:
        readme_text = readme_path.read_text(encoding="utf-8")
        readme_h1, readme_paragraph = _extract_h1_and_first_paragraph(readme_text)

    if not readme_h1 and not readme_paragraph:
        log.warning("repo_description_sync: no README found or no H1 — skipping")
        return RepoDescriptionSyncPassResult(
            session_id=session_id,
            summary="skipped: no README or no H1 heading found",
        )

    # ------------------------------------------------------------------
    # 4. Build the prompt and call the LLM agent
    # ------------------------------------------------------------------
    from .._resources import agent_definitions_dir
    from ..agents.base import _safe_close, build_agent_from_definition
    from ..agents.retry import run_agent
    from ..agents.yaml_loader import load_agent_definition

    yaml_path = agent_definitions_dir() / "periodic" / "repo_description_sync.yaml"
    definition = load_agent_definition(yaml_path)

    prompt = (
        "<forge-description>\n"
        f"{current_description or '(empty)'}\n"
        "</forge-description>\n\n"
        "<readme-h1>\n"
        f"{readme_h1}\n"
        "</readme-h1>\n\n"
        "<readme-first-paragraph>\n"
        f"{readme_paragraph}\n"
        "</readme-first-paragraph>\n\n"
        "Compare the forge description against the README content above. "
        "Return your judgment as a structured RepoDescriptionSyncResult."
    )

    agent = build_agent_from_definition(
        settings,
        definition,
        tools=[],  # No fs tools — the runner already extracted README content
    )

    try:
        result = run_agent(
            agent,
            lambda h: h.run_sync(prompt),
            what="repo_description_sync",
        )
    finally:
        _safe_close(agent)

    output = result.output
    should_update = getattr(output, "should_update", False)
    new_description = getattr(output, "new_description", "")
    summary = getattr(output, "summary", "")

    # ------------------------------------------------------------------
    # 5. Update forge description if needed
    # ------------------------------------------------------------------
    description_updated = False
    if should_update and new_description and new_description != current_description:
        try:
            ok = forge.update_repo(
                owner=owner, repo=repo_name, description=new_description
            )
            if ok:
                description_updated = True
                log.info(
                    "repo_description_sync: updated description for %s/%s → %r",
                    owner,
                    repo_name,
                    new_description,
                )
                summary = f"updated: {summary}"
            else:
                log.warning(
                    "repo_description_sync: update_repo returned False for %s/%s",
                    owner,
                    repo_name,
                )
                summary = f"update failed (API returned False): {summary}"
        except Exception as e:
            log.exception(
                "repo_description_sync: update_repo raised for %s/%s",
                owner,
                repo_name,
            )
            summary = f"update failed ({e}): {summary}"
    else:
        log.info("repo_description_sync: no update needed for %s/%s", owner, repo_name)

    return RepoDescriptionSyncPassResult(
        session_id=session_id,
        summary=summary,
        description_updated=description_updated,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_owner_repo(url: str) -> tuple[str, str]:
    """Parse owner and repo name from a forge remote URL.

    Supports HTTPS (https://<host>/owner/repo.git) and
    SSH (git@<host>:owner/repo.git) formats.

    This is intentionally forge-agnostic (unlike the GitHub-specific
    ``_parse_owner_repo`` in ``forge/github.py``). The runner may be
    called against GitHub, GitLab, or self-hosted instances, so it
    must parse any valid forge URL.
    """
    import re

    # HTTPS: https://<host>/{owner...}/repo.git
    # The owner is everything between the host and the final path segment.
    m = re.match(r"https?://[^/]+/(.+)/([^/]+?)(?:\.git)?$", url)
    if m:
        return m.group(1), m.group(2)
    # SSH: git@<host>:{owner...}/repo.git
    m = re.match(r"git@[^:]+:(.+)/([^/]+?)(?:\.git)?$", url)
    if m:
        return m.group(1), m.group(2)
    raise ValueError(f"cannot parse owner/repo from URL: {url!r}")
