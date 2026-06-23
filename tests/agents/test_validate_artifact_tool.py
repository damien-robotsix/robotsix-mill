from __future__ import annotations

import contextlib

from robotsix_mill.agents import validate_artifact_tool
from robotsix_mill.agents.tool_registry import ToolRegistry


# ── validate_artifact_path ─────────────────────────────────────────────


def test_validate_artifact_path_existing_file(tmp_path):
    """An existing file resolves to an EXISTS (file) string."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text("x = 1\n")
    result = validate_artifact_tool.validate_artifact_path(tmp_path, "src/mod.py")
    assert result == "EXISTS: src/mod.py (file)"


def test_validate_artifact_path_existing_directory(tmp_path):
    """An existing directory resolves to an EXISTS (directory) string."""
    (tmp_path / "docs").mkdir()
    result = validate_artifact_tool.validate_artifact_path(tmp_path, "docs")
    assert result == "EXISTS: docs (directory)"


def test_validate_artifact_path_missing(tmp_path):
    """An absent path resolves to a MISSING string and does not raise."""
    result = validate_artifact_tool.validate_artifact_path(tmp_path, "src/nope.py")
    assert result == "MISSING: src/nope.py does not exist in the repository"


def test_validate_artifact_path_escape_is_missing(tmp_path):
    """A path escaping repo_dir is reported as MISSING via the confinement
    guard — even when the escaped path resolves to a file that genuinely
    exists outside the clone."""
    repo = tmp_path / "repo"
    repo.mkdir()
    # Place a real file at the location ``../outside.txt`` resolves to
    # (``tmp_path/outside.txt``). Without the confinement guard the
    # filesystem check would report EXISTS; the guard must override that
    # to MISSING so the tool never reports artifacts outside the clone.
    (repo.parent / "outside.txt").write_text("secret")
    assert (repo / "../outside.txt").resolve().is_file()  # escaped target exists
    result = validate_artifact_tool.validate_artifact_path(repo, "../outside.txt")
    assert result == "MISSING: ../outside.txt does not exist in the repository"


# ── make_validate_artifact_tool ────────────────────────────────────────


def test_make_validate_artifact_tool_returns_callable(tmp_path):
    """The factory returns a callable that delegates to the existence check."""
    (tmp_path / "f.py").write_text("y = 2\n")
    tool = validate_artifact_tool.make_validate_artifact_tool(tmp_path)
    assert callable(tool)
    assert tool("f.py") == "EXISTS: f.py (file)"
    assert tool("missing.py") == "MISSING: missing.py does not exist in the repository"


def test_make_validate_artifact_tool_registers_once(tmp_path):
    """make_validate_artifact_tool registers 'validate_artifact' exactly once."""
    ToolRegistry._tools.pop("validate_artifact", None)

    validate_artifact_tool.make_validate_artifact_tool(tmp_path)
    validate_artifact_tool.make_validate_artifact_tool(tmp_path)

    matches = [t for t in ToolRegistry.list_tools() if t.name == "validate_artifact"]
    assert len(matches) == 1
    info = matches[0]
    assert info.category == "exploration"
    assert info.parameters == {"path": "str"}


# ── trace_stage child-span test ────────────────────────────────────────


def test_trace_stage_validate_artifact_emits_span(tmp_path, monkeypatch):
    """validate_artifact opens a child span named 'validate_artifact' via trace_stage."""
    spans: list[str] = []

    @contextlib.contextmanager
    def fake_trace_stage(name):
        spans.append(name)
        yield

    monkeypatch.setattr(validate_artifact_tool, "trace_stage", fake_trace_stage)
    (tmp_path / "f.py").write_text("x = 1")
    tool = validate_artifact_tool.make_validate_artifact_tool(tmp_path)
    result = tool("f.py")
    assert result == "EXISTS: f.py (file)"
    assert spans == ["validate_artifact"]
