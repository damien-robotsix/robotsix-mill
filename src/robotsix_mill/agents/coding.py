"""Implement seam.

``run_implement_agent`` is the single seam the implement *stage* drives
(tests monkeypatch it). It runs ONE coordinator pass (see
:mod:`.coordinating`): a capable model that explores via cheap
sub-agents and edits the repo itself. The deterministic
test→retry→escalate loop lives in the implement *stage*, which
re-invokes this seam with a distilled ``feedback`` diagnosis after a
failed test gate.

Budget/agent failures raise :class:`AgentBudgetError` /
:class:`AgentRunError` so the stage blocks-as-resumable (a resume just
re-runs the coordinator fresh — it re-explores, no transcript needed).
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import Settings

log = logging.getLogger("robotsix_mill.coding")


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
    epic_context: str = "",
) -> tuple[str, list, str]:
    """Run ONE coordinator pass for this ticket. Returns
    ``(summary, [], updated_memory)``.

    ``feedback`` — set by the implement stage when it re-invokes after a
    failed test gate — is a distilled diagnosis of that failure,
    forwarded to the coordinator so the next pass fixes exactly it.
    ``history`` is accepted for the stage seam signature but unused: a
    retry re-runs the coordinator fresh and the partial edits persist on
    disk in ``repo_dir``."""
    from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded

    from .coordinating import run_coordinator

    try:
        result = run_coordinator(
            settings=settings, repo_dir=repo_dir, spec=spec, memory=memory,
            feedback=feedback, epic_context=epic_context,
        )
    except UsageLimitExceeded as e:
        raise AgentBudgetError(str(e), []) from e
    except UnexpectedModelBehavior as e:
        log.warning(
            "implement: output retries exhausted on primary model (%s), "
            "falling back to deepseek/deepseek-v4-flash",
            settings.model,
        )
        try:
            result = run_coordinator(
                settings=settings, repo_dir=repo_dir, spec=spec, memory=memory,
                feedback=feedback, model_name="deepseek/deepseek-v4-flash",
                epic_context=epic_context,
            )
        except Exception as fallback_e:
            raise AgentRunError(
                f"output retries exhausted on primary + fallback models: "
                f"primary={e}, fallback={fallback_e}",
                [],
            ) from e
    except (AgentBudgetError, AgentRunError):
        raise
    except Exception as e:  # noqa: BLE001 — block-as-resumable
        raise AgentRunError(str(e), []) from e

    return result.summary, [], result.updated_memory



