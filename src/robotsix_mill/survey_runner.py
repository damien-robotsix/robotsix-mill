"""Survey runner — orchestrates a single survey pass.

Clones the repo (best-effort), reads the memory ledger, invokes the
survey agent, writes returned memory verbatim, and creates draft
tickets for identified improvements.

Seam: tests monkeypatch ``run_survey_agent`` from agents.surveying.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from .config import RepoConfig, Settings
from .forge.auth import github_token


def _clone_token(settings, repo_config):
    """Resolve a clone token via github_token; return None when no
    credentials configured (clone will fail and be handled)."""
    try:
        return github_token(settings, repo_config=repo_config)
    except RuntimeError:
        return None

from .core.models import SourceKind
from .core.service import TicketService
from .pass_runner import run_agent_pass

log = logging.getLogger("robotsix_mill.survey")


@dataclass
class SurveyPassResult:
    """Result of running a survey pass."""

    updated_memory: str
    drafts_created: list[dict]  # list of ticket dicts (id, title)
    session_id: str = ""        # Langfuse session.id for this survey run


def run_survey_pass(session_id: str, repo_config: RepoConfig | None = None) -> SurveyPassResult:
    """Execute one full survey pass.

    Reads the memory ledger, invokes the survey agent, writes the
    returned memory verbatim, and creates draft tickets for identified
    improvements.

    Args:
        session_id: Langfuse session id from the poll loop.
        repo_config: Optional per-repo configuration for multi-repo
            serve. When provided, ticket creation and memory files
            are scoped to this repo.

    Returns:
        SurveyPassResult with updated memory and created draft info.
    """
    settings = Settings()
    memory_file = settings.memory_file_for('survey', repo_config.board_id if repo_config else '')
    clone_dir: Path | None = None
    forge_remote_url = settings.forge_remote_url

    if repo_config is not None:
        service = TicketService(settings, board_id=repo_config.board_id)
        repo_data_dir = settings.data_dir / repo_config.repo_id
        repo_data_dir.mkdir(parents=True, exist_ok=True)
        memory_file = repo_data_dir / "survey_memory.md"
        if repo_config.forge_remote_url:
            forge_remote_url = repo_config.forge_remote_url
            clone_dir = repo_data_dir / "survey_workspace" / "repo"
    else:
        service = TicketService(settings)

    from .agents import surveying
    from .vcs import git_ops

    # Clone the repo locally so the survey agent can inspect it.
    # Idempotent (reuse an existing clone); best-effort (clone failure
    # → proceed web-only).
    repo_dir = None
    if forge_remote_url:
        import subprocess

        cand = clone_dir or (settings.data_dir / "survey_workspace" / "repo")
        if (cand / ".git").exists():
            repo_dir = cand
        else:
            try:
                git_ops.clone(
                    forge_remote_url, cand,
                    settings.forge_target_branch, _clone_token(settings, repo_config),
                )
                repo_dir = cand
            except subprocess.CalledProcessError as e:
                log.warning(
                    "survey clone failed, web/context-only: %s",
                    (e.stderr or "")[:200],
                )

    log.info("survey pass starting (session %s)", session_id)
    agent_fn = partial(surveying.run_survey_agent, repo_dir=repo_dir)
    result = run_agent_pass(
        agent_fn=agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.SURVEY,
        service=service,
        settings=settings,
        origin_session=session_id,
    )

    return SurveyPassResult(
        updated_memory=result.updated_memory,
        drafts_created=result.drafts_created,
        session_id=session_id,
    )
