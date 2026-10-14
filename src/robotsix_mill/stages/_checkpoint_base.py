"""Shared base for the budget-cap checkpoint dataclasses.

Both the ci_fix stage
(:class:`robotsix_mill.stages._ci_fix_checkpoint.CiFixCheckpoint`) and the
implement stage
(:class:`robotsix_mill.stages.implement._checkpoint.ImplementCheckpoint`)
persist a minimal per-pass checkpoint so a budget-capped pass can resume
without re-doing completed work.  The cap/boundary and persistence
machinery is identical between the two passes; it lives here once.

A concrete subclass adds its task-specific fields (ci_fix persists the
failing-CI context; implement persists the agent goal / feedback /
reference files), sets the two :class:`~typing.ClassVar` hooks
(``_checkpoint_filename`` and ``_checkpoint_label``), and overrides
:meth:`_CheckpointBase._payload_extra` to contribute its distinct keys to
the serialized payload.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any, ClassVar

from ..core.workspace import Workspace

log = logging.getLogger(__name__)

# Tool-call budget reserved at the tail of a pass for checkpoint
# finalization.  The effective tool-call cap becomes
# ``coordinator_max_tool_calls - _CHECKPOINT_TOOL_CALL_RESERVE``: when the
# agent reaches that boundary the checkpoint is finalized (diff stat +
# finalized marker) and the pass is stopped as budget-capped.  The reserve
# guarantees the checkpoint is persisted BEFORE the run tears down, which
# pydantic-ai's own hard usage limit (which discards the partial state)
# cannot do.
_CHECKPOINT_TOOL_CALL_RESERVE = 5

# Per-step arg/output summary caps written to the checkpoint file (a
# ``write_file``/``edit_file`` input can be a whole file — never persist
# more than a digest).
_CHECKPOINT_INPUT_MAX_CHARS = 200
_CHECKPOINT_OUTPUT_MAX_CHARS = 800


def _truncate(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"…[{len(text) - max_chars} chars elided]"


def _diff_stat(repo_dir: Path) -> str | None:
    """Return ``git diff --stat HEAD`` for *repo_dir*, or ``None`` when the
    diff cannot be computed (best-effort — never fatal to the pass).
    """
    import subprocess

    try:
        out = subprocess.run(
            ["git", "diff", "--stat", "HEAD"],  # noqa: S607 (git from PATH)
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return None
    return out.stdout.strip() or None


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(kw_only=True)
class _CheckpointBase:
    """Common cap/boundary and persistence machinery for the budget-cap
    checkpoints.

    Persisted incrementally by the shared tool wrapper: every completed
    tool call appends a step (:meth:`record_step`) and the file is
    finalized (diff stat + ``finalized`` marker) when the pass reaches the
    boundary or is otherwise torn down at the cap.
    """

    # Overridden by each concrete subclass.
    _checkpoint_filename: ClassVar[str]
    _checkpoint_label: ClassVar[str]

    ws: Workspace
    ticket_id: str
    memory: str = ""
    max_tool_calls: int = 300
    steps: list[dict[str, Any]] = field(default_factory=list)
    diff_stat: str | None = None
    finalized: bool = False
    finalized_at: str | None = None
    saved_at: str | None = None
    # Serializes concurrent tool-call writes (pydantic-ai executes function
    # tool calls of one step in parallel, possibly across threads).
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ------------------------------------------------------------------
    # cap / boundary
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """Checkpointing is only armed when the cap is large enough to
        reserve a tail of tool calls for finalization.  A tiny cap (e.g. a
        test config) falls back to the plain pydantic-ai hard limit.
        """
        return self.max_tool_calls > _CHECKPOINT_TOOL_CALL_RESERVE

    @property
    def boundary(self) -> int | None:
        """Tool-call count at which the pass is checkpoint-finalized and
        stopped as budget-capped, or ``None`` when checkpointing is
        disabled.
        """
        if not self.enabled:
            return None
        return self.max_tool_calls - _CHECKPOINT_TOOL_CALL_RESERVE

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    @property
    def path(self) -> Path:
        return self.ws.artifacts_dir / self._checkpoint_filename

    def record_step(
        self,
        tool: str,
        input_summary: str,
        output_summary: str,
    ) -> None:
        """Append one completed tool call and persist the checkpoint."""
        with self._lock:
            self.steps.append(
                {
                    "seq": len(self.steps) + 1,
                    "tool": tool,
                    "input": _truncate(input_summary, _CHECKPOINT_INPUT_MAX_CHARS),
                    "output": _truncate(output_summary, _CHECKPOINT_OUTPUT_MAX_CHARS),
                }
            )
            self.saved_at = _now_iso()
            self._write_unlocked()

    def finalize(self, repo_dir: Path) -> None:
        """Write the final checkpoint at the cap boundary: capture the
        working-tree diff so the resume pass knows what is already on disk,
        and mark the checkpoint as finalized (a valid resume point).

        Idempotent — the tool-call boundary finalizes here, and the stage's
        budget-cap exception handler may finalize again (e.g. when the
        request-limit cap fires before the tool-call boundary).
        """
        with self._lock:
            self.diff_stat = _diff_stat(repo_dir)
            self.finalized = True
            self.finalized_at = _now_iso()
            self.saved_at = self.finalized_at
            self._write_unlocked()

    def _payload_extra(self) -> dict[str, Any]:
        """Task-specific keys serialized alongside the common payload.

        Overridden by each concrete subclass to contribute its distinct
        persisted schema (e.g. ci_fix's ``failing_summary`` vs implement's
        ``goal``/``feedback``/``reference_files``).
        """
        raise NotImplementedError

    def _write_unlocked(self) -> None:
        data: dict[str, Any] = {
            "ticket_id": self.ticket_id,
            **self._payload_extra(),
            "memory": self.memory,
            "max_tool_calls": self.max_tool_calls,
            "boundary": self.boundary,
            "steps": self.steps,
            "diff_stat": self.diff_stat,
            "finalized": self.finalized,
            "finalized_at": self.finalized_at,
            "saved_at": self.saved_at,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            # A checkpoint write failure must never break a tool call — the
            # pass just continues without a resume point.
            log.warning(
                "%s: failed to write %s (%s)",
                self.ticket_id,
                self._checkpoint_label,
                self.path,
                exc_info=True,
            )
