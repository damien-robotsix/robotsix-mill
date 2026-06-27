"""Tests for refine checkpoint persistence (save / load / clear)."""

from __future__ import annotations

import json
from pathlib import Path

from robotsix_mill.agents.refining import ChildSpec, FileMapEntry, RefineResult
from robotsix_mill.core.workspace import Workspace
from robotsix_mill.stages.refine._checkpoint import (
    clear_refine_checkpoint,
    load_refine_checkpoint,
    save_refine_checkpoint,
)


def _make_result(**overrides) -> RefineResult:
    """Minimal RefineResult with sensible defaults for checkpoint tests."""
    kwargs: dict = {
        "spec_markdown": "# Test spec",
        "split": True,
        "children": [
            ChildSpec(title="child-1", spec_markdown="## Child 1", depends_on=[]),
            ChildSpec(
                title="child-2", spec_markdown="## Child 2", depends_on=[1]
            ),
        ],
        "promote_to_epic": False,
        "epic_body": None,
        "updated_memory": "memory note",
        "file_map": [
            FileMapEntry(file="src/a.py", note="entry point"),
            FileMapEntry(file="tests/test_a.py", note="tests"),
        ],
        "title": "Test ticket",
        "reference_files": ["src/a.py", "tests/test_a.py"],
        "conversation_state": b"binary-state-data",
    }
    kwargs.update(overrides)
    return RefineResult(**kwargs)


# ---------------------------------------------------------------------------
# save_refine_checkpoint
# ---------------------------------------------------------------------------


def test_save_stores_expected_json_fields(tmp_path: Path) -> None:
    """save_refine_checkpoint writes the expected top-level keys and
    base64-encodes conversation_state."""
    ws = Workspace(tmp_path, "T-1")
    result = _make_result()
    save_refine_checkpoint(ws, result)

    checkpoint = ws.artifacts_dir / "refine_checkpoint.json"
    assert checkpoint.is_file()

    data = json.loads(checkpoint.read_text(encoding="utf-8"))

    assert data["spec_markdown"] == "# Test spec"
    assert data["split"] is True
    assert data["promote_to_epic"] is False
    assert data["epic_body"] is None
    assert data["updated_memory"] == "memory note"
    assert data["title"] == "Test ticket"
    assert data["reference_files"] == ["src/a.py", "tests/test_a.py"]
    assert data["conversation_state_b64"] is not None
    assert isinstance(data["conversation_state_b64"], str)

    # children
    assert data["children"] is not None
    assert len(data["children"]) == 2
    assert data["children"][0]["title"] == "child-1"
    assert data["children"][1]["depends_on"] == [1]

    # file_map
    assert data["file_map"] is not None
    assert len(data["file_map"]) == 2
    assert data["file_map"][0]["file"] == "src/a.py"

    # conversation_state is base64-encoded
    import base64

    decoded = base64.b64decode(data["conversation_state_b64"])
    assert decoded == b"binary-state-data"


def test_save_with_none_conversation_state(tmp_path: Path) -> None:
    """conversation_state_b64 is None when conversation_state is None."""
    ws = Workspace(tmp_path, "T-2")
    result = _make_result(conversation_state=None)
    save_refine_checkpoint(ws, result)
    checkpoint = ws.artifacts_dir / "refine_checkpoint.json"
    data = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert data["conversation_state_b64"] is None


def test_save_with_empty_children_and_file_map(tmp_path: Path) -> None:
    """None children / file_map serialize as null in JSON."""
    ws = Workspace(tmp_path, "T-3")
    result = _make_result(children=None, file_map=None, reference_files=[])
    save_refine_checkpoint(ws, result)
    checkpoint = ws.artifacts_dir / "refine_checkpoint.json"
    data = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert data["children"] is None
    assert data["file_map"] is None
    assert data["reference_files"] == []


# ---------------------------------------------------------------------------
# load_refine_checkpoint
# ---------------------------------------------------------------------------


def test_load_round_trips_saved_checkpoint(tmp_path: Path) -> None:
    """A saved checkpoint can be loaded back and produces an equivalent
    RefineResult."""
    ws = Workspace(tmp_path, "T-4")
    original = _make_result()
    save_refine_checkpoint(ws, original)

    result, conv_state = load_refine_checkpoint(ws)
    assert result is not None
    assert conv_state == b"binary-state-data"

    assert result.spec_markdown == original.spec_markdown
    assert result.split == original.split
    assert result.promote_to_epic == original.promote_to_epic
    assert result.epic_body == original.epic_body
    assert result.updated_memory == original.updated_memory
    assert result.title == original.title
    assert result.reference_files == original.reference_files
    assert result.conversation_state == original.conversation_state

    assert result.children is not None
    assert len(result.children) == 2
    assert result.children[0].title == "child-1"
    assert result.children[1].depends_on == [1]

    assert result.file_map is not None
    assert len(result.file_map) == 2
    assert result.file_map[0].file == "src/a.py"


def test_load_returns_none_when_no_checkpoint_file(tmp_path: Path) -> None:
    """load_refine_checkpoint returns (None, None) when the checkpoint
    file does not exist."""
    ws = Workspace(tmp_path, "T-5")
    result, conv_state = load_refine_checkpoint(ws)
    assert result is None
    assert conv_state is None


def test_load_handles_corrupt_json(tmp_path: Path) -> None:
    """load_refine_checkpoint returns (None, None) for a corrupt JSON
    file instead of raising."""
    ws = Workspace(tmp_path, "T-6")
    checkpoint = ws.artifacts_dir / "refine_checkpoint.json"
    checkpoint.write_text("not valid json {{{", encoding="utf-8")
    result, conv_state = load_refine_checkpoint(ws)
    assert result is None
    assert conv_state is None


def test_load_handles_missing_keys(tmp_path: Path) -> None:
    """load_refine_checkpoint tolerates JSON missing some expected keys
    (defaults are applied)."""
    ws = Workspace(tmp_path, "T-7")
    checkpoint = ws.artifacts_dir / "refine_checkpoint.json"
    checkpoint.write_text(
        json.dumps({"spec_markdown": "minimal"}),
        encoding="utf-8",
    )
    result, conv_state = load_refine_checkpoint(ws)
    assert result is not None
    assert result.spec_markdown == "minimal"
    assert result.split is False
    assert result.children is None
    assert result.title is None


def test_load_handles_invalid_base64(tmp_path: Path) -> None:
    """load_refine_checkpoint returns None conversation_state when
    base64 decoding fails (corrupt data)."""
    ws = Workspace(tmp_path, "T-8")
    checkpoint = ws.artifacts_dir / "refine_checkpoint.json"
    checkpoint.write_text(
        json.dumps({"conversation_state_b64": "!!!not-valid-base64!!!"}),
        encoding="utf-8",
    )
    result, conv_state = load_refine_checkpoint(ws)
    assert result is not None
    assert conv_state is None


# ---------------------------------------------------------------------------
# clear_refine_checkpoint
# ---------------------------------------------------------------------------


def test_clear_deletes_file(tmp_path: Path) -> None:
    """clear_refine_checkpoint removes the checkpoint file."""
    ws = Workspace(tmp_path, "T-9")
    checkpoint = ws.artifacts_dir / "refine_checkpoint.json"
    checkpoint.write_text("{}", encoding="utf-8")
    assert checkpoint.exists()
    clear_refine_checkpoint(ws)
    assert not checkpoint.exists()


def test_clear_idempotent_when_file_absent(tmp_path: Path) -> None:
    """clear_refine_checkpoint does not raise when the file is already
    absent."""
    ws = Workspace(tmp_path, "T-10")
    checkpoint = ws.artifacts_dir / "refine_checkpoint.json"
    assert not checkpoint.exists()
    # Must not raise
    clear_refine_checkpoint(ws)
    assert not checkpoint.exists()
