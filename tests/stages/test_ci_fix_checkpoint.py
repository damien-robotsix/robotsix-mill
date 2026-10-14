"""Tests for the ci_fix budget-cap checkpoint persistence (save / load /
clear / resume-history builder) and the stage's budget-cap routing."""

from __future__ import annotations

import json
from pathlib import Path

from robotsix_mill.core.workspace import Workspace
from robotsix_mill.stages._ci_fix_checkpoint import (
    CiFixCheckpoint,
    build_ci_fix_checkpoint_resume_history,
    clear_ci_fix_checkpoint,
    load_ci_fix_checkpoint,
)
from robotsix_mill.stages.ci_fix import _is_budget_cap_error


def _checkpoint(ws: Workspace, **overrides) -> CiFixCheckpoint:
    """Minimal checkpoint with sensible defaults for the tests."""
    kwargs: dict = {
        "ws": ws,
        "ticket_id": "T-1",
        "failing_summary": "ruff check failed on src/a.py",
        "memory": "mem note",
        "previous_attempts": "prior attempt digest",
        "max_tool_calls": 300,
    }
    kwargs.update(overrides)
    return CiFixCheckpoint(**kwargs)


# ---------------------------------------------------------------------------
# boundary / enabled
# ---------------------------------------------------------------------------


def test_boundary_reserves_tail_of_tool_calls(tmp_path: Path) -> None:
    ws = Workspace(tmp_path, "T-1")
    ckpt = _checkpoint(ws, max_tool_calls=300)
    assert ckpt.enabled is True
    assert ckpt.boundary == 295
    assert ckpt.max_tool_calls == 300


def test_boundary_disabled_for_tiny_cap(tmp_path: Path) -> None:
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
    ckpt.record_step("read_file", 'path="src/a.py"', "contents")
    ckpt.record_step("edit_file", 'path="src/a.py"', "edited src/a.py")
    ckpt.finalize(repo_dir=tmp_path)

    checkpoint = ws.artifacts_dir / "ci_fix_checkpoint.json"
    assert checkpoint.is_file()

    data = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert data["ticket_id"] == "T-1"
    assert data["failing_summary"] == "ruff check failed on src/a.py"
    assert data["memory"] == "mem note"
    assert data["previous_attempts"] == "prior attempt digest"
    assert data["max_tool_calls"] == 300
    assert data["boundary"] == 295
    assert data["finalized"] is True
    assert data["finalized_at"] is not None
    assert len(data["steps"]) == 2
    assert data["steps"][0]["seq"] == 1
    assert data["steps"][0]["tool"] == "read_file"
    assert data["steps"][1]["tool"] == "edit_file"


def test_record_step_truncates_large_input_output(tmp_path: Path) -> None:
    ws = Workspace(tmp_path, "T-1")
    ckpt = _checkpoint(ws)
    big = "x" * 50_000
    ckpt.record_step("write_file", f'path="a.py", content="{big}"', big)
    ckpt.finalize(repo_dir=tmp_path)

    data = json.loads(
        (ws.artifacts_dir / "ci_fix_checkpoint.json").read_text(encoding="utf-8")
    )
    step = data["steps"][0]
    assert len(step["input"]) < 1000
    assert len(step["output"]) < 2000
    assert "chars elided" in step["input"]


def test_finalize_truncates_large_failing_summary(tmp_path: Path) -> None:
    """The failing-CI context (job logs + alerts) can be large — only a
    digest is persisted so the resume prompt stays bounded."""
    ws = Workspace(tmp_path, "T-1")
    ckpt = _checkpoint(ws, failing_summary="y" * 50_000)
    ckpt.record_step("read_file", "q", "a")
    ckpt.finalize(repo_dir=tmp_path)

    data = json.loads(
        (ws.artifacts_dir / "ci_fix_checkpoint.json").read_text(encoding="utf-8")
    )
    assert len(data["failing_summary"]) < 5000
    assert "chars elided" in data["failing_summary"]


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------


def test_load_round_trips_saved_checkpoint(tmp_path: Path) -> None:
    ws = Workspace(tmp_path, "T-1")
    ckpt = _checkpoint(ws)
    ckpt.record_step("read_file", "q", "a")
    ckpt.finalize(repo_dir=tmp_path)

    loaded = load_ci_fix_checkpoint(ws)
    assert loaded is not None
    assert loaded.ticket_id == "T-1"
    assert loaded.failing_summary == "ruff check failed on src/a.py"
    assert loaded.finalized is True
    assert len(loaded.steps) == 1
    assert loaded.steps[0]["tool"] == "read_file"


def test_load_returns_none_when_no_checkpoint(tmp_path: Path) -> None:
    ws = Workspace(tmp_path, "T-1")
    assert load_ci_fix_checkpoint(ws) is None


def test_load_returns_none_for_corrupt_json(tmp_path: Path) -> None:
    ws = Workspace(tmp_path, "T-1")
    path = ws.artifacts_dir / "ci_fix_checkpoint.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json {{{", encoding="utf-8")
    assert load_ci_fix_checkpoint(ws) is None


def test_load_returns_none_for_unfinalized_scratch(tmp_path: Path) -> None:
    """A scratch checkpoint from a pass torn down WITHOUT reaching the cap
    boundary is not a valid resume point."""
    ws = Workspace(tmp_path, "T-1")
    ckpt = _checkpoint(ws)
    ckpt.record_step("read_file", "q", "a")  # persisted but not finalized
    assert load_ci_fix_checkpoint(ws) is None


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------


def test_clear_deletes_file(tmp_path: Path) -> None:
    ws = Workspace(tmp_path, "T-1")
    ckpt = _checkpoint(ws)
    ckpt.record_step("read_file", "q", "a")
    assert (ws.artifacts_dir / "ci_fix_checkpoint.json").is_file()

    clear_ci_fix_checkpoint(ws)
    assert not (ws.artifacts_dir / "ci_fix_checkpoint.json").exists()
    # idempotent
    clear_ci_fix_checkpoint(ws)


# ---------------------------------------------------------------------------
# build_ci_fix_checkpoint_resume_history
# ---------------------------------------------------------------------------


def test_resume_history_built_from_steps_and_diff(tmp_path: Path) -> None:
    ws = Workspace(tmp_path, "T-1")
    ckpt = _checkpoint(ws)
    ckpt.record_step("read_file", 'path="src/a.py"', "contents")
    ckpt.record_step("edit_file", 'path="src/a.py"', "edited")
    ckpt.finalize(repo_dir=tmp_path)

    history = build_ci_fix_checkpoint_resume_history(ckpt)
    assert len(history) == 3

    first_text = _all_text(history)
    assert "2 tool call(s)" in first_text
    assert "edit_file×1" in first_text
    assert "read_file×1" in first_text
    assert "do NOT redo" in first_text or "do NOT re-run" in first_text
    assert "turn CI green" in first_text


# ---------------------------------------------------------------------------
# _is_budget_cap_error
# ---------------------------------------------------------------------------


def test_is_budget_cap_error_detects_usage_limit() -> None:
    from pydantic_ai.exceptions import UsageLimitExceeded

    assert _is_budget_cap_error(UsageLimitExceeded("the limit was exceeded")) is True


def test_is_budget_cap_error_detects_agent_budget_error() -> None:
    from robotsix_mill.agents.coding import AgentBudgetError

    assert _is_budget_cap_error(AgentBudgetError("cap hit", [])) is True


def test_is_budget_cap_error_detects_message_marker() -> None:
    assert _is_budget_cap_error(RuntimeError("resumable (move to READY)")) is True


def test_is_budget_cap_error_rejects_plain_error() -> None:
    assert _is_budget_cap_error(ValueError("some other failure")) is False


def _all_text(history) -> str:
    """Concatenate every TextPart / UserPromptPart content in the history."""
    parts: list[str] = []
    for m in history:
        for p in getattr(m, "parts", []):
            if getattr(p, "part_kind", None) == "text" and hasattr(p, "content"):
                parts.append(p.content)
            if getattr(p, "part_kind", None) == "user-prompt" and hasattr(p, "content"):
                parts.append(p.content)
    return "\n".join(parts)
