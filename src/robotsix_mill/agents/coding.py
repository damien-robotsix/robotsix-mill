"""Implement seam.

``run_implement_agent`` is the single seam the implement *stage* drives
(tests monkeypatch it). It delegates to the **coordinator** (see
:mod:`.coordinating`): a capable model that explores via cheap
sub-agents, plans, delegates the edit to the stateless implement
sub-agent with precise instructions, runs the cheap test sub-agent,
and loops — keeping its own history short.

Budget/agent failures raise :class:`AgentBudgetError` /
:class:`AgentRunError` so the stage blocks-as-resumable (a resume just
re-runs the coordinator fresh — it re-explores, no transcript needed).
``dump_history``/``load_history`` are kept for the stage's artifact
plumbing (the transcript is now empty — resume = fresh coordinator).
"""

from __future__ import annotations

from pathlib import Path

from ..config import Settings


class AgentBudgetError(RuntimeError):
    """Usage/budget cap hit — operationally retryable."""

    def __init__(self, message: str, messages: list) -> None:
        super().__init__(message)
        self.messages = messages


class AgentRunError(RuntimeError):
    """Agent raised / could not converge — block-as-resumable."""

    def __init__(self, message: str, messages: list) -> None:
        super().__init__(message)
        self.messages = messages


def run_implement_agent(
    *,
    settings: Settings,
    repo_dir: Path,
    spec: str,
    feedback: str | None = None,
    history: list | None = None,
    memory: str = "",
) -> tuple[str, list, str]:
    """Drive the coordinator for this ticket. Returns
    ``(summary, [], updated_memory)``. ``feedback``/``history`` are
    accepted for the stage's signature but unused — the coordinator
    owns the explore→implement→test loop and a resume re-runs it
    fresh."""
    from pydantic_ai.exceptions import UsageLimitExceeded

    from .coordinating import run_coordinator

    try:
        result = run_coordinator(
            settings=settings, repo_dir=repo_dir, spec=spec, memory=memory,
        )
    except UsageLimitExceeded as e:
        raise AgentBudgetError(str(e), []) from e
    except (AgentBudgetError, AgentRunError):
        raise
    except Exception as e:  # noqa: BLE001 — block-as-resumable
        raise AgentRunError(str(e), []) from e

    summary = result.summary
    if summary.strip().upper().startswith("UNRESOLVED"):
        raise AgentRunError(
            f"coordinator could not converge: {summary[:300]}", []
        )
    return summary, [], result.updated_memory



