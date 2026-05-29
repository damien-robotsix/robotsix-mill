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
    """Agent raised / could not converge — block-as-resumable.

    ``cause`` carries the original exception so the implement stage
    can classify it (e.g. via ``transient_errors.classify_stage_exc``)
    and route transient infra failures — OpenRouter timeouts, 5xx,
    429 — to a retry-with-backoff instead of hard-BLOCK. Without the
    typed cause, the str-coerced wrap loses the exception's identity
    and every failure looks the same.
    """

    def __init__(
        self,
        message: str,
        messages: list,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.messages = messages
        self.cause = cause


def run_implement_agent(
    *,
    settings: Settings,
    repo_dir: Path,
    spec: str,
    feedback: str | None = None,
    reference_files: list[dict] | None = None,
    message_history: list | None = None,
    memory: str = "",
    epic_context: str = "",
    previous_attempt_summary: str | None = None,
    file_map: set[str] | None = None,
    board_id: str = "",
    language_instructions: str = "",
) -> tuple[str, list[str], str, bytes | None, bytes | None, bool, str]:
    """Run ONE coordinator pass for this ticket. Returns
    ``(summary, reference_files, updated_memory, conversation_state,
    new_messages, no_change_needed, no_change_rationale)``.
    ``conversation_state`` is the full transcript (for resume);
    ``new_messages`` is only what this run added (for pause detection).
    The last two fields let implement signal "no edits needed, ticket
    is already satisfied" — see :class:`ImplementResult` for the
    routing contract.

    ``feedback`` — set by the implement stage when it re-invokes after a
    failed test gate — is a distilled diagnosis of that failure,
    forwarded to the coordinator so the next pass fixes exactly it.
    ``reference_files`` — pre-loaded file content from the refine stage
    (``reference_files.json``), injected as synthetic read_file messages
    on the first coordinator pass so the model starts with those files
    already "read."
    ``message_history`` — advanced/test use: pre-built pydantic-ai
    message history passed through to the coordinator unchanged.
    ``previous_attempt_summary`` — the coordinator's summary from a
    prior pass, injected as a ``<previous_attempt>`` block on retries
    so the model doesn't undo its prior correct work.
    ``file_map`` — kept on the signature for backward compatibility
    with the stage runner (which loads it from ``file_map.json`` for
    *scope enforcement*); not used for routing. The implement agent
    is always the primary worker and can delegate to per-domain
    expert sub-agents via its ``consult_expert`` tool, with each
    expert keeping its own memory ledger."""
    from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded

    from .coordinating import run_coordinator

    def _run_primary():
        return run_coordinator(
            settings=settings,
            repo_dir=repo_dir,
            spec=spec,
            memory=memory,
            feedback=feedback,
            epic_context=epic_context,
            reference_files=reference_files,
            message_history=message_history,
            previous_attempt_summary=previous_attempt_summary,
            board_id=board_id,
            language_instructions=language_instructions,
        )

    try:
        result = _run_primary()
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
                settings=settings,
                repo_dir=repo_dir,
                spec=spec,
                memory=memory,
                feedback=feedback,
                model_name="deepseek/deepseek-v4-flash",
                epic_context=epic_context,
                reference_files=reference_files,
                message_history=message_history,
                previous_attempt_summary=previous_attempt_summary,
                board_id=board_id,
                language_instructions=language_instructions,
            )
        except Exception as fallback_e:
            raise AgentRunError(
                f"output retries exhausted on primary + fallback models: "
                f"primary={e}, fallback={fallback_e}",
                [],
            ) from e
    except AgentBudgetError, AgentRunError:
        raise
    except Exception as e:  # noqa: BLE001 — block-as-resumable
        raise AgentRunError(str(e), [], cause=e) from e

    from .explore import is_explore_budget_exhausted, reset_explore_budget_exhausted

    if is_explore_budget_exhausted():
        reset_explore_budget_exhausted()
        raise AgentBudgetError(
            f"explore sub-agent exceeded request_limit="
            f"{settings.explore_request_limit}; "
            f"coordinator could not proceed without exploration",
            [],
        )

    return (
        result.summary,
        result.reference_files,
        result.updated_memory,
        result.conversation_state,
        result.new_messages,
        result.no_change_needed,
        result.no_change_rationale,
    )
