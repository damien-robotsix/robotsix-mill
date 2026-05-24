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

from .config import Settings, get_secrets
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


def run_survey_pass() -> SurveyPassResult:
    """Execute one full survey pass.

    Reads the memory ledger, invokes the survey agent, writes the
    returned memory verbatim, and creates draft tickets for identified
    improvements.

    Returns:
        SurveyPassResult with updated memory and created draft info.
    """
    settings = Settings()
    service = TicketService(settings)
    memory_file = settings.survey_memory_file

    from .agents import surveying
    from .runtime import tracing
    from .vcs import git_ops

    # Clone the repo locally so the survey agent can inspect it.
    # Idempotent (reuse an existing clone); best-effort (clone failure
    # → proceed web-only).
    repo_dir = None
    if settings.forge_remote_url:
        import subprocess

        cand = settings.data_dir / "survey_workspace" / "repo"
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
                    "survey clone failed, web/context-only: %s",
                    (e.stderr or "")[:200],
                )

    from .runtime.tracing import make_session_id

    session_id = make_session_id("survey")
    log.info("survey pass starting (session %s)", session_id)
    try:
        with tracing.start_ticket_root_span(session_id, "survey"):
            agent_fn = partial(surveying.run_survey_agent, repo_dir=repo_dir)
            result = run_agent_pass(
                agent_fn=agent_fn,
                memory_file=memory_file,
                source_label=SourceKind.SURVEY,
                service=service,
                settings=settings,
                origin_session=session_id,
            )
    except Exception as e:  # noqa: BLE001
        log.exception("survey agent failed")
        raise RuntimeError(f"survey agent failed: {e}") from e

    return SurveyPassResult(
        updated_memory=result.updated_memory,
        drafts_created=result.drafts_created,
        session_id=session_id,
    )
