"""Budget-cap checkpoint persistence for the implement stage.

A single implement pass can exhaust the ``coordinator_max_tool_calls``
tool-call budget mid-flight.  pydantic-ai's ``UsageLimitExceeded``
discards the partial transcript (``run_implement_agent`` has no
``conversation_state`` to persist), so a resume-from-BLOCKED used to
restart from scratch and re-exhaust the cap.

This module persists a minimal checkpoint *as the pass runs* — ticket id,
the current agent goal, and the completed tool-call steps — and finalizes
it (with the working-tree diff) when the pass approaches the cap.  On the
next pass the checkpoint is loaded and distilled into a compact resume
history so the agent skips already-completed steps instead of re-running
them.  The checkpoint lifecycle is one pass: it is created fresh at the
start of every LLM implement pass and cleared when the pass completes.

The cap/boundary and persistence machinery is shared with the ci_fix
checkpoint via :class:`robotsix_mill.stages._checkpoint_base._CheckpointBase`;
only the task-specific fields (goal / feedback / reference files) and the
load/resume helpers live here.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass
from typing import Any, ClassVar

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

from ...core.workspace import Workspace
from .._checkpoint_base import _CheckpointBase

log = logging.getLogger(__name__)

# Filename of the checkpoint artifact, under ``ws.artifacts_dir``.
_CHECKPOINT_FILENAME = "implement_checkpoint.json"

# Only the last N completed steps are expanded in detail in the resume
# history; earlier steps are summarized by tool-name counts so the resume
# prompt stays compact regardless of how many calls the prior pass made.
_CHECKPOINT_RESUME_DETAIL_STEPS = 15


@dataclass(kw_only=True)
class ImplementCheckpoint(_CheckpointBase):
    """Minimal per-pass state needed to resume an implement pass after the
    tool-call budget cap.

    Persisted incrementally by the coordinator's tool wrapper: every
    completed tool call appends a step (``record_step``) and the file is
    finalized (diff stat + ``finalized`` marker) when the pass reaches the
    boundary or is otherwise torn down at the cap.
    """

    _checkpoint_filename: ClassVar[str] = _CHECKPOINT_FILENAME
    _checkpoint_label: ClassVar[str] = "implement checkpoint"

    goal: str
    feedback: str | None = None
    reference_files: list[Any] | None = None
    previous_attempt_summary: str | None = None

    def _payload_extra(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "feedback": self.feedback,
            "reference_files": self.reference_files or [],
            "previous_attempt_summary": self.previous_attempt_summary,
        }

    # ------------------------------------------------------------------
    # load / clear
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, ws: Workspace) -> ImplementCheckpoint | None:
        """Load a saved implement checkpoint, or ``None`` when absent /
        corrupt / not finalized.

        Only a *finalized* checkpoint (written at the cap boundary) is a
        valid resume point; a half-written scratch file from a pass that was
        torn down another way is ignored.
        """
        path = ws.artifacts_dir / _CHECKPOINT_FILENAME
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        # NOTE: comma-form `except A, B:` is INTENTIONAL. The repo pins
        # requires-python >=3.14 and ruff target-version py314, where
        # PEP 758 gives the comma form TUPLE semantics — equivalent to
        # `except (json.JSONDecodeError, OSError):` (both caught; a
        # TypeError is not). It is not the Python 2 binding form. It is
        # also ruff-format canonical here: ruff 0.16.0 + py314 rewrites
        # the parenthesized form back to this comma form, so the
        # parenthesized form is not `ruff format --check`-clean.
        except json.JSONDecodeError, OSError:
            log.warning("implement checkpoint corrupt — ignoring")
            return None
        if not data.get("finalized"):
            return None
        checkpoint = cls(
            ws=ws,
            ticket_id=data.get("ticket_id", ""),
            goal=data.get("goal", ""),
            feedback=data.get("feedback"),
            memory=data.get("memory", ""),
            reference_files=data.get("reference_files") or None,
            previous_attempt_summary=data.get("previous_attempt_summary"),
            max_tool_calls=data.get("max_tool_calls", 300),
            steps=data.get("steps", []),
            diff_stat=data.get("diff_stat"),
            finalized=True,
            finalized_at=data.get("finalized_at"),
            saved_at=data.get("saved_at"),
        )
        return checkpoint

    @classmethod
    def clear(cls, ws: Workspace) -> None:
        """Remove the checkpoint artifact when a pass completes (or starts
        a fresh pass), so a stale file is never mistaken for a cap resume.
        """
        path = ws.artifacts_dir / _CHECKPOINT_FILENAME
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.warning(
                "failed to clear implement checkpoint (%s)", path, exc_info=True
            )


def load_implement_checkpoint(ws: Workspace) -> ImplementCheckpoint | None:
    """Convenience wrapper for :meth:`ImplementCheckpoint.load`."""
    return ImplementCheckpoint.load(ws)


def clear_implement_checkpoint(ws: Workspace) -> None:
    """Convenience wrapper for :meth:`ImplementCheckpoint.clear`."""
    ImplementCheckpoint.clear(ws)


def build_checkpoint_resume_history(
    checkpoint: ImplementCheckpoint,
) -> list[Any]:
    """Build a compact 3-message resume history from a budget-cap
    checkpoint.

    Unlike the full-transcript budget resume (``conversation_state``, only
    available for the explore-sub-agent cap), the tool-call cap leaves no
    transcript behind — the checkpoint's completed-step log is all we have.
    The resume prompt tells the model what the prior pass already did and
    that those effects are committed on the branch, so it continues rather
    than re-exploring from scratch.

    Returns a ``list[ModelMessage]`` of exactly 3 messages, mirroring
    :func:`robotsix_mill.stages.pause.build_compact_resume_message_history`.
    """
    steps = checkpoint.steps
    total = len(steps)
    lines: list[str] = [
        (
            f"The previous implement pass for this ticket was stopped by the "
            f"tool-call budget cap after {total} tool call(s)."
        ),
    ]
    counts = Counter(s.get("tool", "?") for s in steps)
    if counts:
        lines.append(
            "Completed steps by tool: "
            + ", ".join(f"{name}×{n}" for name, n in counts.most_common())
        )
    detail = steps[-_CHECKPOINT_RESUME_DETAIL_STEPS:]
    if detail:
        lines.append("Most recent completed steps (do NOT redo these):")
        for s in detail:
            seq = s.get("seq", "?")
            tool = s.get("tool", "?")
            inp = (s.get("input") or "").strip() or "(no args)"
            lines.append(f"- #{seq} {tool}: {inp[:120]}")
    if checkpoint.feedback:
        lines.append(
            f"The pass was working on this feedback:\n{checkpoint.feedback[:500]}"
        )

    return [
        ModelRequest(
            parts=[
                UserPromptPart(
                    content="Context from previous session (budget-cap checkpoint):",
                ),
            ]
        ),
        ModelResponse(
            parts=[
                TextPart(
                    content=(
                        "Prior session summary:\n"
                        + "\n".join(lines)
                        + "\n\nModified files during that session "
                        "(git diff --stat HEAD):\n"
                        + (checkpoint.diff_stat or "(unavailable)")
                    ),
                ),
            ]
        ),
        ModelRequest(
            parts=[
                UserPromptPart(
                    content=(
                        "Continue implementing this ticket from where the "
                        "previous pass stopped.  The steps listed above were "
                        "already completed and their file edits are already "
                        "committed on the branch — do NOT re-run or redo "
                        "them.  Read the current state of the tree, then "
                        "continue with the remaining work to satisfy the "
                        "ticket's acceptance criteria."
                    ),
                ),
            ]
        ),
    ]
