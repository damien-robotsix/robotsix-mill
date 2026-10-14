"""Tests for the implement budget-cap checkpoint persistence (save / load /
clear / resume-history builder)."""

from __future__ import annotations

import json
from pathlib import Path

from robotsix_mill.core.workspace import Workspace
from robotsix_mill.stages.implement._checkpoint import (
    ImplementCheckpoint,
    build_checkpoint_resume_history,
    clear_implement_checkpoint,
    load_implement_checkpoint,
)


def _checkpoint(ws: Workspace, **overrides) -> ImplementCheckpoint:
    """Minimal checkpoint with sensible defaults for the tests."""
    kwargs: dict = {
        "ws": ws,
        "ticket_id": "T-1",
        "goal": "Add feature X",
        "feedback": None,
        "memory": "mem note",
        "reference_files": [{"path": "src/a.py"}],
        "previous_attempt_summary": None,
        "max_tool_calls": 300,
    }
    kwargs.update(overrides)
    return ImplementCheckpoint(**kwargs)


# ---------------------------------------------------------------------------
# boundary / enabled
# ---------------------------------------------------------------------------


def test_boundary_reserves_tail_of_tool_calls(tmp_path: Path) -> None:
    """The effective cap is ``coordinator_max_tool_calls`` minus the
    reserve, and the hard cap is left untouched for pydantic-ai."""
    ws = Workspace(tmp_path, "T-1")
    ckpt = _checkpoint(ws, max_tool_calls=300)
    assert ckpt.enabled is True
    assert ckpt.boundary == 295
    assert ckpt.max_tool_calls == 300


def test_boundary_disabled_for_tiny_cap(tmp_path: Path) -> None:
    """A cap too small to reserve a tail disables checkpointing — the pass
    falls back to pydantic-ai's plain UsageLimitExceeded."""
    ws = Workspace(tmp_path, "T-1")
    ckpt = _checkpoint(ws, max_tool_calls=3)
    assert ckpt.enabled is False
    assert ckpt.boundary is None


# ---------------------------------------------------------------------------
# record_step / finalize persistence
# ---------------------------------------------------------------------------


def test_record_step_and_finalize_write_expected_json(tmp_path: Path) -> None:
    ws = Workspace(tmp_path, "T-1")
    ckpt = _checkpoint(ws)
    ckpt.record_step("explore", 'question="where is X"', "found X in src/a.py")
    ckpt.record_step("edit_file", 'path="src/a.py"', "edited src/a.py")
    ckpt.finalize(repo_dir=tmp_path)

    checkpoint = ws.artifacts_dir / "implement_checkpoint.json"
    assert checkpoint.is_file()

    data = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert data["ticket_id"] == "T-1"
    assert data["goal"] == "Add feature X"
    assert data["memory"] == "mem note"
    assert data["reference_files"] == [{"path": "src/a.py"}]
    assert data["max_tool_calls"] == 300
    assert data["boundary"] == 295
    assert data["finalized"] is True
    assert data["finalized_at"] is not None
    assert len(data["steps"]) == 2
    assert data["steps"][0]["seq"] == 1
    assert data["steps"][0]["tool"] == "explore"
    assert "where is X" in data["steps"][0]["input"]
    assert data["steps"][1]["seq"] == 2
    assert data["steps"][1]["tool"] == "edit_file"


def test_record_step_truncates_large_input_output(tmp_path: Path) -> None:
    """A ``write_file`` input can be a whole file — the checkpoint must only
    persist a truncated digest, never the full content."""
    ws = Workspace(tmp_path, "T-1")
    ckpt = _checkpoint(ws)
    big = "x" * 50_000
    ckpt.record_step("write_file", f'path="a.py", content="{big}"', big)
    ckpt.finalize(repo_dir=tmp_path)

    data = json.loads(
        (ws.artifacts_dir / "implement_checkpoint.json").read_text(encoding="utf-8")
    )
    step = data["steps"][0]
    assert len(step["input"]) < 1000
    assert len(step["output"]) < 2000
    assert "chars elided" in step["input"]


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------


def test_load_round_trips_saved_checkpoint(tmp_path: Path) -> None:
    ws = Workspace(tmp_path, "T-1")
    ckpt = _checkpoint(ws)
    ckpt.record_step("explore", "q", "a")
    ckpt.finalize(repo_dir=tmp_path)

    loaded = load_implement_checkpoint(ws)
    assert loaded is not None
    assert loaded.ticket_id == "T-1"
    assert loaded.goal == "Add feature X"
    assert loaded.finalized is True
    assert len(loaded.steps) == 1
    assert loaded.steps[0]["tool"] == "explore"


def test_load_returns_none_when_no_checkpoint(tmp_path: Path) -> None:
    ws = Workspace(tmp_path, "T-1")
    assert load_implement_checkpoint(ws) is None


def test_load_returns_none_for_corrupt_json(tmp_path: Path) -> None:
    ws = Workspace(tmp_path, "T-1")
    path = ws.artifacts_dir / "implement_checkpoint.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json {{{", encoding="utf-8")
    assert load_implement_checkpoint(ws) is None


def test_load_returns_none_for_unfinalized_scratch(tmp_path: Path) -> None:
    """A scratch checkpoint from a pass that was torn down WITHOUT reaching
    the cap boundary is not a valid resume point."""
    ws = Workspace(tmp_path, "T-1")
    ckpt = _checkpoint(ws)
    ckpt.record_step("explore", "q", "a")  # persisted but not finalized
    assert load_implement_checkpoint(ws) is None


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------


def test_clear_deletes_file(tmp_path: Path) -> None:
    ws = Workspace(tmp_path, "T-1")
    ckpt = _checkpoint(ws)
    ckpt.record_step("explore", "q", "a")
    assert (ws.artifacts_dir / "implement_checkpoint.json").is_file()

    clear_implement_checkpoint(ws)
    assert not (ws.artifacts_dir / "implement_checkpoint.json").exists()
    # idempotent
    clear_implement_checkpoint(ws)


# ---------------------------------------------------------------------------
# build_checkpoint_resume_history
# ---------------------------------------------------------------------------


def test_resume_history_built_from_steps_and_diff(tmp_path: Path) -> None:
    ws = Workspace(tmp_path, "T-1")
    ckpt = _checkpoint(ws, feedback="fix test_x")
    ckpt.record_step("explore", 'question="find X"', "found X")
    ckpt.record_step("edit_file", 'path="src/a.py"', "edited")
    ckpt.finalize(repo_dir=tmp_path)

    history = build_checkpoint_resume_history(ckpt)
    assert len(history) == 3

    first_text = _all_text(history)
    assert "2 tool call(s)" in first_text
    assert "edit_file×1" in first_text
    assert "explore×1" in first_text
    assert "edit_file" in first_text  # most-recent-step detail
    assert "fix test_x" in first_text  # in-pass feedback folded in
    assert "do NOT redo" in first_text or "do NOT re-run" in first_text


def _all_text(history) -> str:
    """Concatenate every TextPart content across the resume history."""
    parts: list[str] = []
    for m in history:
        for p in getattr(m, "parts", []):
            if getattr(p, "part_kind", None) == "text" and hasattr(p, "content"):
                parts.append(p.content)
            if getattr(p, "part_kind", None) == "user-prompt" and hasattr(p, "content"):
                parts.append(p.content)
    return "\n".join(parts)
