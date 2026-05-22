"""Tests for ``robotsix_mill.agents.fs_tools`` — the sole I/O gateway
for every agent."""

import os
import sys

import pytest

from robotsix_mill.agents.fs_tools import build_fs_tools, _safe
from robotsix_mill import sandbox


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build(root, settings):
    """Return the 6 tools as a name→callable dict."""
    tools = build_fs_tools(root, settings)
    return {t.__name__: t for t in tools}


def _make_file(root, path, content):
    p = root / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# ===================================================================
# _safe / path sandboxing
# ===================================================================

class TestSafe:
    def test_relative_inside_root(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        (root / "foo.txt").write_text("hi")
        result = _safe(root, "foo.txt")
        assert result == (root / "foo.txt").resolve()

    def test_subdir_inside_root(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        (root / "subdir").mkdir()
        result = _safe(root, "subdir/bar.py")
        assert result == (root / "subdir/bar.py").resolve()

    def test_traversal_dotdot(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        with pytest.raises(ValueError, match="escapes"):
            _safe(root, "../../../etc/passwd")

    def test_absolute_path(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        with pytest.raises(ValueError, match="escapes"):
            _safe(root, "/etc/passwd")

    def test_complex_traversal(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        with pytest.raises(ValueError, match="escapes"):
            _safe(root, "foo/../../etc/shadow")

    def test_symlink_escape(self, tmp_path):
        """If the filesystem supports symlinks, a symlink pointing
        outside root must be rejected."""
        root = tmp_path / "repo"
        root.mkdir()
        try:
            (root / "link").symlink_to("/etc")
        except OSError:
            pytest.skip("symlinks not supported on this platform")
        with pytest.raises(ValueError, match="escapes"):
            _safe(root, "link/passwd")

    def test_root_not_cloned_yet(self, tmp_path):
        root = tmp_path / "nonexistent"
        assert not root.exists()
        with pytest.raises(ValueError, match="not been cloned yet"):
            _safe(root, "any.txt")

    def test_dot_path(self, tmp_path):
        """'.' resolves to root itself — must be allowed."""
        root = tmp_path / "repo"
        root.mkdir()
        result = _safe(root, ".")
        assert result == root.resolve()


# ===================================================================
# Return type / shape
# ===================================================================

def test_build_fs_tools_returns_six_callables(tmp_path, settings):
    root = tmp_path / "repo"
    root.mkdir()
    tools = build_fs_tools(root, settings)
    assert isinstance(tools, list)
    assert len(tools) == 6
    for t in tools:
        assert callable(t)

    names = {t.__name__ for t in tools}
    assert names == {
        "read_file", "write_file", "edit_file", "delete_file",
        "list_dir", "run_command",
    }

    for t in tools:
        assert t.__doc__, f"{t.__name__} has no docstring"


def test_build_fs_tools_does_not_raise_on_valid_root(tmp_path, settings):
    root = tmp_path / "repo"
    root.mkdir()
    build_fs_tools(root, settings)  # must not raise


# ===================================================================
# read_file
# ===================================================================

class TestReadFile:
    def test_read_existing(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "hello.txt", "world\n")
        tools = _build(root, settings)
        assert tools["read_file"]("hello.txt") == "world\n"

    def test_read_nonexistent(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        tools = _build(root, settings)
        result = tools["read_file"]("nope.txt")
        assert isinstance(result, str)
        assert "error" in result.lower()

    def test_read_outside_root(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        tools = _build(root, settings)
        result = tools["read_file"]("../../../etc/passwd")
        assert isinstance(result, str)
        assert "error" in result.lower()

    def test_read_root_not_cloned(self, tmp_path, settings):
        root = tmp_path / "nonexistent"
        tools = _build(root, settings)
        result = tools["read_file"]("any.txt")
        assert isinstance(result, str)
        assert "not been cloned yet" in result.lower()


# ===================================================================
# read_file offset/limit
# ===================================================================

class TestReadFileOffsetLimit:
    def test_default_args_byte_identical(self, tmp_path, settings):
        """read_file(path) returns same content as before the change."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "hello.txt", "line1\nline2\n")
        tools = _build(root, settings)
        result = tools["read_file"]("hello.txt")
        assert result == "line1\nline2\n"

    def test_default_args_empty_file(self, tmp_path, settings):
        """Empty file with default args returns '' (no regression)."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "empty.txt", "")
        tools = _build(root, settings)
        result = tools["read_file"]("empty.txt")
        assert result == ""

    def test_default_args_trailing_newline(self, tmp_path, settings):
        """File ending with newline preserved byte-identically."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "f.txt", "a\n")
        tools = _build(root, settings)
        assert tools["read_file"]("f.txt") == "a\n"

    def test_default_args_no_trailing_newline(self, tmp_path, settings):
        """File without trailing newline preserved."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "f.txt", "a")
        tools = _build(root, settings)
        assert tools["read_file"]("f.txt") == "a"

    def test_basic_offset_limit(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "f.txt", "line1\nline2\nline3\nline4\n")
        tools = _build(root, settings)
        assert tools["read_file"]("f.txt", offset=2, limit=2) == "line2\nline3\n"

    def test_offset_to_eof(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "f.txt", "line1\nline2\nline3\nline4\n")
        tools = _build(root, settings)
        assert tools["read_file"]("f.txt", offset=3) == "line3\nline4\n"

    def test_offset_past_eof_returns_note(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "f.txt", "a\nb\nc\n")
        tools = _build(root, settings)
        result = tools["read_file"]("f.txt", offset=5)
        assert "(file has 3 lines; offset 5 is beyond end)" in result

    def test_offset_past_eof_with_limit_returns_note(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "f.txt", "a\nb\nc\n")
        tools = _build(root, settings)
        result = tools["read_file"]("f.txt", offset=4, limit=1)
        assert "(file has 3 lines; offset 4 is beyond end)" in result

    def test_zero_offset_normalized_to_1(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "f.txt", "line1\nline2\nline3\n")
        tools = _build(root, settings)
        a = tools["read_file"]("f.txt", offset=0, limit=1)
        b = tools["read_file"]("f.txt", offset=1, limit=1)
        assert a == b == "line1\n"

    def test_negative_offset_normalized_to_1(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "f.txt", "line1\nline2\n")
        tools = _build(root, settings)
        result = tools["read_file"]("f.txt", offset=-5, limit=2)
        assert result == "line1\nline2\n"

    def test_limit_exceeds_remaining_clips_silently(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "f.txt", "a\nb\nc\n")
        tools = _build(root, settings)
        result = tools["read_file"]("f.txt", offset=2, limit=10)
        assert result == "b\nc\n"

    def test_limit_none_with_offset_reads_to_eof(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "f.txt", "a\nb\nc\nd\n")
        tools = _build(root, settings)
        result = tools["read_file"]("f.txt", offset=2, limit=None)
        assert result == "b\nc\nd\n"

    def test_error_paths_unchanged(self, tmp_path, settings):
        """Nonexistent, outside root, and not-cloned still return error strings."""
        root = tmp_path / "repo"
        root.mkdir()
        tools = _build(root, settings)
        assert "error" in tools["read_file"]("nope.txt").lower()
        assert "error" in tools["read_file"]("../../../etc/passwd").lower()

        root2 = tmp_path / "nonexistent"
        tools2 = _build(root2, settings)
        assert "not been cloned yet" in tools2["read_file"]("any.txt", offset=2, limit=1).lower()

    def test_docstring_visible(self, tmp_path, settings):
        """Docstring is pydantic-ai-visible on the closure."""
        root = tmp_path / "repo"
        root.mkdir()
        tools = build_fs_tools(root, settings)
        rf = [t for t in tools if t.__name__ == "read_file"][0]
        assert "offset" in rf.__doc__
        assert "limit" in rf.__doc__

    def test_splitlines_keepends_preserves_lf(self, tmp_path, settings):
        """splitlines(keepends=True) preserves \\n endings — no
        regressions from the offset/limit logic."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "f.txt", "line1\nline2\nline3\n")
        tools = _build(root, settings)
        # default: byte-identical
        assert tools["read_file"]("f.txt") == "line1\nline2\nline3\n"
        # offset/limit preserves line endings
        assert tools["read_file"]("f.txt", offset=2, limit=1) == "line2\n"


# ===================================================================
# write_file
# ===================================================================

class TestWriteFile:
    def test_write_new_file(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        tools = _build(root, settings)
        result = tools["write_file"]("new.txt", "hello")
        assert "wrote 5 bytes to new.txt" in result
        assert (root / "new.txt").read_text() == "hello"

    def test_overwrite_existing(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "f.txt", "old")
        tools = _build(root, settings)
        result = tools["write_file"]("f.txt", "new!")
        assert "wrote 4 bytes to f.txt" in result
        assert (root / "f.txt").read_text() == "new!"

    def test_creates_parent_dirs(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        tools = _build(root, settings)
        result = tools["write_file"]("a/b/c/d.txt", "deep")
        assert "wrote 4 bytes to a/b/c/d.txt" in result
        assert (root / "a/b/c/d.txt").read_text() == "deep"

    def test_write_outside_root(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        tools = _build(root, settings)
        result = tools["write_file"]("../../../etc/hosts", "x")
        assert isinstance(result, str)
        assert "error" in result.lower()
        assert not (tmp_path / "etc/hosts").exists()

    def test_write_root_not_cloned(self, tmp_path, settings):
        root = tmp_path / "nonexistent"
        tools = _build(root, settings)
        result = tools["write_file"]("x.txt", "x")
        assert isinstance(result, str)
        assert "not been cloned yet" in result.lower()


# ===================================================================
# edit_file
# ===================================================================

class TestEditFile:
    def test_unique_match_replaces(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "f.py", "alpha beta gamma\n")
        tools = _build(root, settings)
        result = tools["edit_file"]("f.py", "beta", "BETA")
        assert "replaced 1 occurrence in f.py" in result
        assert (root / "f.py").read_text() == "alpha BETA gamma\n"

    def test_not_found(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        content = "hello world\n"
        _make_file(root, "f.txt", content)
        tools = _build(root, settings)
        result = tools["edit_file"]("f.txt", "xyzzy", "nothing")
        assert "old_string not found" in result
        assert (root / "f.txt").read_text() == content  # unchanged

    def test_multiple_occurrences(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        content = "cat dog cat\n"
        _make_file(root, "f.txt", content)
        tools = _build(root, settings)
        result = tools["edit_file"]("f.txt", "cat", "CAT")
        assert "appears 2 times" in result
        assert (root / "f.txt").read_text() == content  # unchanged

    def test_empty_old_string(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "f.txt", "hello")
        tools = _build(root, settings)
        result = tools["edit_file"]("f.txt", "", "X")
        # empty string: str.count('') returns len+1, so "appears 6 times"
        assert "appears 6 times" in result
        assert (root / "f.txt").read_text() == "hello"  # unchanged

    def test_edit_outside_root(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        tools = _build(root, settings)
        result = tools["edit_file"]("../../../etc/passwd", "x", "y")
        assert isinstance(result, str)
        assert "error" in result.lower()

    def test_edit_root_not_cloned(self, tmp_path, settings):
        root = tmp_path / "nonexistent"
        tools = _build(root, settings)
        result = tools["edit_file"]("any.txt", "a", "b")
        assert isinstance(result, str)
        assert "not been cloned yet" in result.lower()


# ===================================================================
# delete_file
# ===================================================================

class TestDeleteFile:
    def test_delete_existing(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "remove.me", "bye")
        tools = _build(root, settings)
        result = tools["delete_file"]("remove.me")
        assert "deleted remove.me" in result
        assert not (root / "remove.me").exists()

    def test_delete_nonexistent(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        tools = _build(root, settings)
        result = tools["delete_file"]("ghost.txt")
        assert isinstance(result, str)
        assert "error" in result.lower()

    def test_delete_outside_root(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        tools = _build(root, settings)
        result = tools["delete_file"]("../../../etc/passwd")
        assert isinstance(result, str)
        assert "error" in result.lower()

    def test_delete_root_not_cloned(self, tmp_path, settings):
        root = tmp_path / "nonexistent"
        tools = _build(root, settings)
        result = tools["delete_file"]("any.txt")
        assert isinstance(result, str)
        assert "not been cloned yet" in result.lower()


# ===================================================================
# list_dir
# ===================================================================

class TestListDir:
    def test_empty_directory(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        tools = _build(root, settings)
        assert tools["list_dir"](".") == ""

    def test_files_and_subdirs(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        (root / "a.txt").write_text("a")
        (root / "b.txt").write_text("b")
        (root / "sub").mkdir()
        tools = _build(root, settings)
        result = tools["list_dir"](".")
        assert result == "a.txt\nb.txt\nsub/"

    def test_default_lists_root(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        (root / "only.txt").write_text("x")
        tools = _build(root, settings)
        # default argument "." — lists root
        assert tools["list_dir"]() == "only.txt"

    def test_path_is_file_not_dir(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "plain.txt", "x")
        tools = _build(root, settings)
        result = tools["list_dir"]("plain.txt")
        assert isinstance(result, str)
        assert "error" in result.lower()

    def test_outside_root(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        tools = _build(root, settings)
        result = tools["list_dir"]("../../../etc")
        assert isinstance(result, str)
        assert "error" in result.lower()

    def test_root_not_cloned(self, tmp_path, settings):
        root = tmp_path / "nonexistent"
        tools = _build(root, settings)
        result = tools["list_dir"]()
        assert isinstance(result, str)
        assert "not been cloned yet" in result.lower()


# ===================================================================
# run_command
# ===================================================================

class TestRunCommand:
    def test_echo_hello(self, tmp_path, settings, fake_sandbox):
        root = tmp_path / "repo"
        root.mkdir()
        tools = _build(root, settings)
        result = tools["run_command"]("echo hello")
        assert result == "exit=0\nhello\n"

    def test_false_nonzero(self, tmp_path, settings, fake_sandbox):
        root = tmp_path / "repo"
        root.mkdir()
        tools = _build(root, settings)
        result = tools["run_command"]("false")
        assert result == "exit=1\nfalse: command failed"

    def test_sandbox_error(self, tmp_path, settings, monkeypatch):
        """When sandbox.run raises SandboxError, the tool catches it
        and returns a string — never propagates the exception."""
        root = tmp_path / "repo"
        root.mkdir()
        tools = _build(root, settings)

        def _raise(*a, **kw):
            raise sandbox.SandboxError("boom")

        monkeypatch.setattr(sandbox, "run", _raise)
        result = tools["run_command"]("anything")
        assert isinstance(result, str)
        assert "sandbox error" in result.lower()
        assert "boom" in result

    def test_root_not_cloned(self, tmp_path, settings, fake_sandbox):
        root = tmp_path / "nonexistent"
        tools = _build(root, settings)
        result = tools["run_command"]("echo hi")
        assert isinstance(result, str)
        assert "not been cloned yet" in result.lower()

    def test_delegates_to_sandbox_correctly(self, tmp_path, settings, monkeypatch):
        """Verify run_command passes the right args to sandbox.run."""
        root = tmp_path / "repo"
        root.mkdir()
        tools = _build(root, settings)
        cap = {}

        def _capture(command, *, repo_dir, settings):
            cap["command"] = command
            cap["repo_dir"] = repo_dir
            cap["settings"] = settings
            return (0, "ok")

        monkeypatch.setattr(sandbox, "run", _capture)
        result = tools["run_command"]("pytest tests/")
        assert result == "exit=0\nok"
        assert cap["command"] == "pytest tests/"
        assert cap["repo_dir"] == root
        assert cap["settings"] is settings


# ===================================================================
# Error-return semantics (the defining invariant)
# ===================================================================

class TestNeverRaises:
    """Every failure path returns a string — no exception ever escapes
    the tool closures."""

    def test_read_file_nonexistent_returns_str(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        tools = _build(root, settings)
        result = tools["read_file"]("no-such-file.txt")
        assert isinstance(result, str)
        assert "error" in result.lower()

    def test_write_file_bad_path_returns_str(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        tools = _build(root, settings)
        result = tools["write_file"]("/absolute/path.txt", "x")
        assert isinstance(result, str)
        assert "error" in result.lower()

    def test_edit_file_bad_path_returns_str(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        tools = _build(root, settings)
        result = tools["edit_file"]("/etc/hosts", "a", "b")
        assert isinstance(result, str)
        assert "error" in result.lower()

    def test_delete_file_bad_path_returns_str(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        tools = _build(root, settings)
        result = tools["delete_file"]("/etc/hosts")
        assert isinstance(result, str)
        assert "error" in result.lower()

    def test_list_dir_bad_path_returns_str(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        tools = _build(root, settings)
        result = tools["list_dir"]("/etc")
        assert isinstance(result, str)
        assert "error" in result.lower()

    def test_run_command_sandbox_error_returns_str(
        self, tmp_path, settings, monkeypatch
    ):
        root = tmp_path / "repo"
        root.mkdir()
        tools = _build(root, settings)

        def _raise(*a, **kw):
            raise sandbox.SandboxError("infra failure")

        monkeypatch.setattr(sandbox, "run", _raise)
        result = tools["run_command"]("ls")
        assert isinstance(result, str)
        assert "error" in result.lower()

    def test_no_tool_ever_raises_on_root_not_cloned(self, tmp_path, settings):
        """Every tool must return an error string, not raise, when the
        root directory does not exist."""
        root = tmp_path / "nonexistent"
        tools = _build(root, settings)

        # All six tools, every failure path returns a string
        for name, tool in tools.items():
            if name == "read_file":
                r = tool("f.txt")
            elif name == "write_file":
                r = tool("f.txt", "x")
            elif name == "edit_file":
                r = tool("f.txt", "a", "b")
            elif name == "delete_file":
                r = tool("f.txt")
            elif name == "list_dir":
                r = tool(".")
            elif name == "run_command":
                r = tool("echo hi")
            else:
                continue
            assert isinstance(r, str), f"{name} returned {type(r)}"
            assert "error" in r.lower(), f"{name}: {r!r}"
