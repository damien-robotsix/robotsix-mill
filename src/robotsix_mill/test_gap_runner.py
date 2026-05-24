"""Test-gap runner — orchestrates a single test-gap pass.

Mirrors the health runner pattern: read memory, invoke agent,
write returned memory verbatim, collect emitted draft tickets.

Seam: tests monkeypatch ``run_test_gap_agent`` from agents.test_gap.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import Settings, get_secrets
from .core.models import SourceKind
from .core.service import TicketService

log = logging.getLogger("robotsix_mill.test_gap")


@dataclass
class TestGapPassResult:
    """Result of running a test-gap pass."""

    updated_memory: str
    drafts_created: list[dict]  # list of ticket dicts (id, title)
    session_id: str = ""        # Langfuse session.id for this test-gap run


def run_test_gap_pass(root: str | None = None) -> TestGapPassResult:
    """Execute one full test-gap pass.

    Reads the memory ledger, invokes the test-gap agent, writes the
    returned memory verbatim, and creates draft tickets for identified
    gaps.

    Args:
        root: repository root (unused directly — the agent uses
              forge_remote_url for repo context; kept for API
              compatibility).

    Returns:
        TestGapPassResult with updated memory and created draft info.
    """
    settings = Settings()
    service = TicketService(settings)
    memory_file = settings.test_gap_memory_file

    # Import here to allow monkeypatching in tests.
    from .agents import test_gap
    from .runtime import tracing
    from .vcs import git_ops

    # Clone the repo locally so the test-gap agent inspects it via
    # explore/read_file instead of web-fetching the project's own
    # files (slow + spawns untagged web_research sub-agent traces).
    # Idempotent (reuse an existing clone); best-effort (no forge or
    # clone failure -> reason from forge_url as before).
    repo_dir = None
    if settings.forge_remote_url:
        import subprocess

        cand = settings.data_dir / "test_gap_workspace" / "repo"
        if (cand / ".git").exists():
            repo_dir = cand
        else:
            try:
                git_ops.clone(
                    settings.forge_remote_url, cand,
                    settings.forge_target_branch, get_secrets().forge_token,
                )
                repo_dir = cand
            except subprocess.CalledProcessError as e:
                log.warning(
                    "test-gap clone failed, web/context-only: %s",
                    (e.stderr or "")[:200],
                )

    # One Langfuse session per test-gap run, so its model calls are
    # attributed (no untagged traces). No-op if tracing isn't ready.
    from .runtime.tracing import make_session_id

    session_id = make_session_id("test-gap")
    log.info("test-gap pass starting (session %s)", session_id)
    try:
        with tracing.start_ticket_root_span(session_id, "test-gap"):
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
    except Exception as e:  # noqa: BLE001
        log.exception("test-gap agent failed")
        raise RuntimeError(f"test-gap agent failed: {e}") from e

    return TestGapPassResult(
        updated_memory=result.updated_memory,
        drafts_created=result.drafts_created,
        session_id=session_id,
    )
