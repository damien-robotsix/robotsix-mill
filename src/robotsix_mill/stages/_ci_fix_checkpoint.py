"""Budget-cap checkpoint persistence for the ci_fix stage.

A single ci_fix pass can exhaust the coordinator tool-call budget
(``coordinator_max_tool_calls``) mid-flight on a broad CI investigation —
the 2026-09-10 CI-red-on-main tickets did exactly this.  pydantic-ai's
``UsageLimitExceeded`` discards the partial transcript, so a
resume-from-BLOCKED used to restart from scratch and re-exhaust the cap
without net progress.

This module mirrors the implement stage's budget-cap checkpoint
(:mod:`robotsix_mill.stages.implement._checkpoint`) for the ci_fix pass:
it persists a minimal checkpoint *as the pass runs* — ticket id, the
failing CI context, and the completed tool-call steps — and finalizes it
(with the working-tree diff) when the pass approaches the cap.  On the
next pass the checkpoint is loaded and distilled into a compact resume
history so the ci_fix agent skips already-completed steps instead of
re-running them.  The checkpoint lifecycle is one pass: created fresh at
the start of every ci_fix pass and cleared when the pass completes without
hitting the cap.

The cap/boundary and persistence machinery is shared with the implement
checkpoint via :class:`robotsix_mill.stages._checkpoint_base._CheckpointBase`;
only the task-specific fields (the failing-CI context and prior-attempt
digest) and the load/resume helpers live here.

The step-recording contract is duck-typed (``record_step``, ``finalize``,
``boundary``, ``enabled``) so the shared tool wrapper
(:func:`robotsix_mill.agents.coordinating._wrap_tools_with_checkpoint`)
drives it exactly as it drives the implement checkpoint.
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

from ..core.workspace import Workspace
from ._checkpoint_base import _CheckpointBase, _truncate

log = logging.getLogger(__name__)

# Filename of the checkpoint artifact, under ``ws.artifacts_dir``.
_CHECKPOINT_FILENAME = "ci_fix_checkpoint.json"

# Only the last N completed steps are expanded in detail in the resume
# history; earlier steps are summarized by tool-name counts so the resume
# prompt stays compact regardless of how many calls the prior pass made.
_CHECKPOINT_RESUME_DETAIL_STEPS = 15

# The failing-CI context can be large (job logs + alerts); keep only a
# digest in the checkpoint so the resume prompt stays bounded.
_CHECKPOINT_FAILING_MAX_CHARS = 4000


@dataclass(kw_only=True)
class CiFixCheckpoint(_CheckpointBase):
    """Minimal per-pass state needed to resume a ci_fix pass after the
    tool-call budget cap.

    Persisted incrementally by the shared tool wrapper: every completed
    tool call appends a step (``record_step``) and the file is finalized
    (diff stat + ``finalized`` marker) when the pass reaches the boundary
    or is otherwise torn down at the cap.
    """

    _checkpoint_filename: ClassVar[str] = _CHECKPOINT_FILENAME
    _checkpoint_label: ClassVar[str] = "ci_fix checkpoint"

    failing_summary: str = ""
    previous_attempts: str = ""

    def _payload_extra(self) -> dict[str, Any]:
        return {
            "failing_summary": _truncate(
                self.failing_summary, _CHECKPOINT_FAILING_MAX_CHARS
            ),
            "previous_attempts": self.previous_attempts,
        }

    # ------------------------------------------------------------------
    # load / clear
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, ws: Workspace) -> CiFixCheckpoint | None:
        """Load a saved ci_fix checkpoint, or ``None`` when absent /
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
        # NOTE: comma-form `except A, B:` is INTENTIONAL (PEP 758, py314
        # tuple semantics — equivalent to `except (JSONDecodeError, OSError)`
        # and ruff-format canonical here). See the implement checkpoint for
        # the full rationale.
        except json.JSONDecodeError, OSError:
            log.warning("ci_fix checkpoint corrupt — ignoring")
            return None
        if not data.get("finalized"):
            return None
        return cls(
            ws=ws,
            ticket_id=data.get("ticket_id", ""),
            failing_summary=data.get("failing_summary", ""),
            memory=data.get("memory", ""),
            previous_attempts=data.get("previous_attempts", ""),
            max_tool_calls=data.get("max_tool_calls", 300),
            steps=data.get("steps", []),
            diff_stat=data.get("diff_stat"),
            finalized=True,
            finalized_at=data.get("finalized_at"),
            saved_at=data.get("saved_at"),
        )

    @classmethod
    def clear(cls, ws: Workspace) -> None:
        """Remove the checkpoint artifact when a pass completes (or starts
        a fresh pass), so a stale file is never mistaken for a cap resume.
        """
        path = ws.artifacts_dir / _CHECKPOINT_FILENAME
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.warning("failed to clear ci_fix checkpoint (%s)", path, exc_info=True)


def load_ci_fix_checkpoint(ws: Workspace) -> CiFixCheckpoint | None:
    """Convenience wrapper for :meth:`CiFixCheckpoint.load`."""
    return CiFixCheckpoint.load(ws)


def clear_ci_fix_checkpoint(ws: Workspace) -> None:
    """Convenience wrapper for :meth:`CiFixCheckpoint.clear`."""
    CiFixCheckpoint.clear(ws)


def build_ci_fix_checkpoint_resume_history(
    checkpoint: CiFixCheckpoint,
) -> list[Any]:
    """Build a compact 3-message resume history from a budget-cap
    checkpoint.

    The tool-call cap leaves no transcript behind — the checkpoint's
    completed-step log is all we have.  The resume prompt tells the model
    what the prior ci_fix pass already did and that those effects are
    committed on the branch, so it continues rather than re-investigating
    from scratch.

    Returns a ``list[ModelMessage]`` of exactly 3 messages, mirroring
    :func:`robotsix_mill.stages.implement._checkpoint.build_checkpoint_resume_history`.
    """
    steps = checkpoint.steps
    total = len(steps)
    lines: list[str] = [
        (
            f"The previous ci_fix pass for this ticket was stopped by the "
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
                        "Continue fixing the failing CI for this ticket from "
                        "where the previous pass stopped.  The steps listed "
                        "above were already completed and any file edits are "
                        "already on the branch — do NOT re-run or redo them.  "
                        "Read the current state of the tree, then continue "
                        "with the remaining work to turn CI green."
                    ),
                ),
            ]
        ),
    ]
