"""Test-gap runner — orchestrates a single test-gap pass.

Mirrors the health runner pattern: read memory, invoke agent,
write returned memory verbatim, collect emitted draft tickets.

Seam: tests monkeypatch ``run_test_gap_agent`` from agents.test_gap.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .config import RepoConfig, Settings, get_secrets
from .core.models import SourceKind
from .core.service import TicketService

log = logging.getLogger("robotsix_mill.test_gap")


@dataclass
class TestGapPassResult:
    """Result of running a test-gap pass."""

    updated_memory: str
    drafts_created: list[dict]  # list of ticket dicts (id, title)
    session_id: str = ""        # Langfuse session.id for this test-gap run


def run_test_gap_pass(session_id: str, repo_config: RepoConfig | None = None) -> TestGapPassResult:
    """Execute one full test-gap pass.

    Reads the memory ledger, invokes the test-gap agent, writes the
    returned memory verbatim, and creates draft tickets for identified
    gaps.

    Args:
        session_id: Langfuse session id from the poll loop.
        repo_config: Optional per-repo configuration for multi-repo
            serve. When provided, ticket creation and memory files
            are scoped to this repo.

    Returns:
        TestGapPassResult with updated memory and created draft info.
    """
    settings = Settings()
    memory_file = settings.test_gap_memory_file
    clone_dir: Path | None = None
    forge_remote_url = settings.forge_remote_url

    if repo_config is not None:
        service = TicketService(settings, board_id=repo_config.board_id)
        repo_data_dir = settings.data_dir / repo_config.repo_id
        repo_data_dir.mkdir(parents=True, exist_ok=True)
        memory_file = repo_data_dir / "test_gap_memory.md"
        if repo_config.forge_remote_url:
            forge_remote_url = repo_config.forge_remote_url
            clone_dir = repo_data_dir / "test_gap_workspace" / "repo"
    else:
        service = TicketService(settings)

    # Import here to allow monkeypatching in tests.
    from .agents import test_gap
    from .vcs import git_ops

    # Clone the repo locally so the test-gap agent inspects it via
    # explore/read_file instead of web-fetching the project's own
    # files (slow + spawns untagged web_research sub-agent traces).
    # Idempotent (reuse an existing clone); best-effort (no forge or
    # clone failure -> reason from forge_url as before).
    repo_dir = None
    if forge_remote_url:
        import subprocess

        cand = clone_dir or (settings.data_dir / "test_gap_workspace" / "repo")
        if (cand / ".git").exists():
            repo_dir = cand
        else:
            try:
                git_ops.clone(
                    forge_remote_url, cand,
                    settings.forge_target_branch, get_secrets().forge_token,
                )
                repo_dir = cand
            except subprocess.CalledProcessError as e:
                log.warning(
                    "test-gap clone failed, web/context-only: %s",
                    (e.stderr or "")[:200],
                )

    log.info("test-gap pass starting (session %s)", session_id)
    from functools import partial
    from .pass_runner import run_agent_pass

    agent_fn = partial(test_gap.run_test_gap_agent, repo_dir=repo_dir)
    result = run_agent_pass(
        agent_fn=agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.TEST_GAP,
        service=service,
        settings=settings,
        origin_session=session_id,
    )

    return TestGapPassResult(
        updated_memory=result.updated_memory,
        drafts_created=result.drafts_created,
        session_id=session_id,
    )
