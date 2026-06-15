"""Tests for ``robotsix_mill.agents.fs_tools`` — the sole I/O gateway
for every agent."""

import types

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
)

from robotsix_mill.agents.fs_tools import (
    _PRUNED_PLACEHOLDER,
    _safe,
    build_fs_tools,
    build_preseed_history,
)
from robotsix_mill import sandbox


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build(root, settings):
    """Return the 6 tools as a name→callable dict."""
    tools = build_fs_tools(root, settings)
    return {t.__name__: t for t in tools}


def _build_extra(root, settings, extra_roots):
    """Like ``_build`` but threads ``extra_roots`` so the cross-repo
    (meta multi-repo) sandbox branch is exercised."""
    tools = build_fs_tools(root, settings, extra_roots=extra_roots)
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

    # -- extra_roots: cross-repo (meta multi-repo) sandbox --------------

    def test_extra_root_single_allows_sibling(self, tmp_path):
        """A '../extra/...' path resolving into the single extra root is
        returned (not rejected)."""
        root = tmp_path / "repos" / "primary"
        extra = tmp_path / "repos" / "extra"
        root.mkdir(parents=True)
        extra.mkdir(parents=True)
        result = _safe(root, "../extra/file.txt", extra_roots=[extra])
        assert result == (extra / "file.txt").resolve()

    def test_extra_root_multiple_allows_second(self, tmp_path):
        """With several extra roots, a path under the 2nd entry is
        allowed."""
        root = tmp_path / "repos" / "primary"
        extra1 = tmp_path / "repos" / "extra1"
        extra2 = tmp_path / "repos" / "extra2"
        for d in (root, extra1, extra2):
            d.mkdir(parents=True)
        result = _safe(root, "../extra2/file.txt", extra_roots=[extra1, extra2])
        assert result == (extra2 / "file.txt").resolve()

    def test_extra_root_still_rejects_outside_all(self, tmp_path):
        """extra_roots widens the sandbox but does not disable it: a path
        outside root AND every extra root still escapes."""
        root = tmp_path / "repos" / "primary"
        extra = tmp_path / "repos" / "extra"
        root.mkdir(parents=True)
        extra.mkdir(parents=True)
        with pytest.raises(ValueError, match="escapes"):
            _safe(root, "../../../etc/passwd", extra_roots=[extra])

    def test_extra_root_inside_primary_still_allowed(self, tmp_path):
        """A path inside the primary root is allowed even when
        extra_roots is also set (no false negative)."""
        root = tmp_path / "repos" / "primary"
        extra = tmp_path / "repos" / "extra"
        root.mkdir(parents=True)
        extra.mkdir(parents=True)
        result = _safe(root, "foo.txt", extra_roots=[extra])
        assert result == (root / "foo.txt").resolve()


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
        "read_file",
        "write_file",
        "edit_file",
        "delete_file",
        "list_dir",
        "run_command",
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
        assert tools["read_file"](path="hello.txt") == "world\n"

    def test_read_nonexistent(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        tools = _build(root, settings)
        result = tools["read_file"](path="nope.txt")
        assert isinstance(result, str)
        assert "does not exist" in result
        assert "nope.txt" in result
        assert "list_dir" in result

    def test_read_directory(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        (root / "subdir").mkdir()
        tools = _build(root, settings)
        result = tools["read_file"](path="subdir")
        assert isinstance(result, str)
        assert "is a directory" in result
        assert "subdir" in result

    def test_read_outside_root(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        tools = _build(root, settings)
        result = tools["read_file"](path="../../../etc/passwd")
        assert isinstance(result, str)
        assert "error" in result.lower()

    def test_read_root_not_cloned(self, tmp_path, settings):
        root = tmp_path / "nonexistent"
        tools = _build(root, settings)
        result = tools["read_file"](path="any.txt")
        assert isinstance(result, str)
        assert "not been cloned yet" in result.lower()

    def test_read_across_extra_root(self, tmp_path, settings):
        """A file in an extra root is readable via a '../extra/...'
        relative path when extra_roots is set."""
        root = tmp_path / "repos" / "primary"
        extra = tmp_path / "repos" / "extra"
        root.mkdir(parents=True)
        extra.mkdir(parents=True)
        _make_file(extra, "sibling.txt", "from sibling\n")
        tools = _build_extra(root, settings, [extra])
        assert tools["read_file"](path="../extra/sibling.txt") == "from sibling\n"

    def test_read_outside_all_roots_with_extra_set(self, tmp_path, settings):
        """A path outside root AND all extra roots is still refused (error
        string) even when extra_roots is set."""
        root = tmp_path / "repos" / "primary"
        extra = tmp_path / "repos" / "extra"
        root.mkdir(parents=True)
        extra.mkdir(parents=True)
        tools = _build_extra(root, settings, [extra])
        result = tools["read_file"](path="../../../etc/passwd")
        assert isinstance(result, str)
        assert "error" in result.lower()


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
        result = tools["read_file"](path="hello.txt")
        assert result == "line1\nline2\n"

    def test_default_args_empty_file(self, tmp_path, settings):
        """Empty file with default args returns '' (no regression)."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "empty.txt", "")
        tools = _build(root, settings)
        result = tools["read_file"](path="empty.txt")
        assert result == ""

    def test_default_args_trailing_newline(self, tmp_path, settings):
        """File ending with newline preserved byte-identically."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "f.txt", "a\n")
        tools = _build(root, settings)
        assert tools["read_file"](path="f.txt") == "a\n"

    def test_default_args_no_trailing_newline(self, tmp_path, settings):
        """File without trailing newline preserved."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "f.txt", "a")
        tools = _build(root, settings)
        assert tools["read_file"](path="f.txt") == "a"

    def test_basic_offset_limit(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "f.txt", "line1\nline2\nline3\nline4\n")
        tools = _build(root, settings)
        assert tools["read_file"](path="f.txt", offset=2, limit=2) == "line2\nline3\n"

    def test_offset_to_eof(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "f.txt", "line1\nline2\nline3\nline4\n")
        tools = _build(root, settings)
        assert tools["read_file"](path="f.txt", offset=3) == "line3\nline4\n"

    def test_offset_past_eof_returns_note(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "f.txt", "a\nb\nc\n")
        tools = _build(root, settings)
        result = tools["read_file"](path="f.txt", offset=5)
        assert "(file has 3 lines; offset 5 is beyond end)" in result

    def test_offset_past_eof_with_limit_returns_note(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "f.txt", "a\nb\nc\n")
        tools = _build(root, settings)
        result = tools["read_file"](path="f.txt", offset=4, limit=1)
        assert "(file has 3 lines; offset 4 is beyond end)" in result

    def test_zero_offset_normalized_to_1(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "f.txt", "line1\nline2\nline3\n")
        tools = _build(root, settings)
        a = tools["read_file"](path="f.txt", offset=0, limit=1)
        b = tools["read_file"](path="f.txt", offset=1, limit=1)
        assert a == b == "line1\n"

    def test_negative_offset_normalized_to_1(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "f.txt", "line1\nline2\n")
        tools = _build(root, settings)
        result = tools["read_file"](path="f.txt", offset=-5, limit=2)
        assert result == "line1\nline2\n"

    def test_limit_exceeds_remaining_clips_silently(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "f.txt", "a\nb\nc\n")
        tools = _build(root, settings)
        result = tools["read_file"](path="f.txt", offset=2, limit=10)
        assert result == "b\nc\n"

    def test_limit_none_with_offset_reads_to_eof(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "f.txt", "a\nb\nc\nd\n")
        tools = _build(root, settings)
        result = tools["read_file"](path="f.txt", offset=2, limit=None)
        assert result == "b\nc\nd\n"

    def test_error_paths_unchanged(self, tmp_path, settings):
        """Nonexistent, outside root, and not-cloned still return error strings."""
        root = tmp_path / "repo"
        root.mkdir()
        tools = _build(root, settings)
        assert "error" in tools["read_file"](path="nope.txt").lower()
        assert "error" in tools["read_file"](path="../../../etc/passwd").lower()

        root2 = tmp_path / "nonexistent"
        tools2 = _build(root2, settings)
        assert (
            "not been cloned yet"
            in tools2["read_file"](path="any.txt", offset=2, limit=1).lower()
        )

    def test_docstring_visible(self, tmp_path, settings):
        """Docstring is pydantic-ai-visible on the closure."""
        root = tmp_path / "repo"
        root.mkdir()
        tools = build_fs_tools(root, settings)
        rf = [t for t in tools if t.__name__ == "read_file"][0]
        assert "offset" in rf.__doc__
        assert "limit" in rf.__doc__

    def test_splitlines_keepends_preserves_lf(self, tmp_path, settings):
        """splitlines(keepends=True) preserves \n endings — no
        regressions from the offset/limit logic."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "f.txt", "line1\nline2\nline3\n")
        tools = _build(root, settings)
        # default: byte-identical
        assert tools["read_file"](path="f.txt") == "line1\nline2\nline3\n"
        # offset/limit preserves line endings
        assert tools["read_file"](path="f.txt", offset=2, limit=1) == "line2\n"


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

    def test_write_across_extra_root(self, tmp_path, settings):
        """A write via '../extra/...' lands in the extra root when
        extra_roots is set."""
        root = tmp_path / "repos" / "primary"
        extra = tmp_path / "repos" / "extra"
        root.mkdir(parents=True)
        extra.mkdir(parents=True)
        tools = _build_extra(root, settings, [extra])
        result = tools["write_file"]("../extra/new.txt", "hello")
        assert "wrote 5 bytes to ../extra/new.txt" in result
        assert (extra / "new.txt").read_text() == "hello"

    def test_write_outside_all_roots_with_extra_set(self, tmp_path, settings):
        """A write outside root AND all extra roots is refused with no
        filesystem side effect, even when extra_roots is set."""
        root = tmp_path / "repos" / "primary"
        extra = tmp_path / "repos" / "extra"
        root.mkdir(parents=True)
        extra.mkdir(parents=True)
        tools = _build_extra(root, settings, [extra])
        result = tools["write_file"]("../../../etc/hosts", "x")
        assert isinstance(result, str)
        assert "error" in result.lower()
        assert not (tmp_path / "etc/hosts").exists()

    def test_write_file_python_syntax_error_refused(self, tmp_path, settings):
        """write_file must refuse a .py with a SyntaxError so the agent
        retries the edit instead of wasting a test cycle on broken code."""
        root = tmp_path / "repo"
        root.mkdir()
        tools = _build(root, settings)
        bad = "def f(:\n    pass\n"  # missing parameter list
        result = tools["write_file"]("mod.py", bad)
        assert "syntax error" in result.lower()
        assert "mod.py" in result
        assert not (root / "mod.py").exists()

    def test_write_file_python_syntax_ok_writes(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        tools = _build(root, settings)
        good = "def f():\n    return 1\n"
        result = tools["write_file"]("mod.py", good)
        assert "wrote" in result
        assert (root / "mod.py").read_text() == good

    def test_write_file_non_python_skips_syntax_check(self, tmp_path, settings):
        """A `.md` or `.yaml` file with text that LOOKS like broken Python
        must still be written — the guard is .py-only."""
        root = tmp_path / "repo"
        root.mkdir()
        tools = _build(root, settings)
        body = "def broken(:\n  no\n"
        result = tools["write_file"]("notes.md", body)
        assert "wrote" in result
        assert (root / "notes.md").read_text() == body

    def test_write_file_lint_on_edit_off_allows_syntax_error(
        self,
        tmp_path,
    ):
        """When lint_on_edit is False the guard is fully bypassed."""
        from robotsix_mill.config import Settings

        s = Settings(data_dir=str(tmp_path / "data"), lint_on_edit=False)
        root = tmp_path / "repo"
        root.mkdir()
        tools = _build(root, s)
        bad = "def f(:\n"
        result = tools["write_file"]("mod.py", bad)
        assert "wrote" in result
        assert (root / "mod.py").read_text() == bad


# ===================================================================
# edit_file
# ===================================================================


class TestEditFile:
    def test_unique_match_replaces(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        # .py with VALID Python: doubles as syntax-check pass-through
        # coverage — edit must succeed and the result must still parse.
        _make_file(root, "f.py", "x = 1\ny = 2\n")
        tools = _build(root, settings)
        result = tools["edit_file"]("f.py", "y = 2", "y = 3")
        assert "replaced 1 occurrence(s) in f.py" in result
        assert (root / "f.py").read_text() == "x = 1\ny = 3\n"

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
        # default count=1 on a 2-occurrence string: must reject to
        # preserve the "unique string" contract; file left unchanged.
        result = tools["edit_file"]("f.txt", "cat", "CAT")
        assert "old_string appears 2 times" in result
        assert (root / "f.txt").read_text() == content  # unchanged

    def test_multiple_occurrences_count_2(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        content = "cat dog cat\n"
        _make_file(root, "f.txt", content)
        tools = _build(root, settings)
        result = tools["edit_file"]("f.txt", "cat", "CAT", count=2)
        assert "replaced 2 occurrence(s)" in result
        assert (root / "f.txt").read_text() == "CAT dog CAT\n"

    def test_multiple_occurrences_count_too_high(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        content = "cat dog cat\n"
        _make_file(root, "f.txt", content)
        tools = _build(root, settings)
        result = tools["edit_file"]("f.txt", "cat", "CAT", count=5)
        assert "but 5 replacement(s) were requested" in result
        assert (root / "f.txt").read_text() == content  # unchanged

    def test_empty_old_string(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "f.txt", "hello")
        tools = _build(root, settings)
        result = tools["edit_file"]("f.txt", "", "X")
        # empty string: str.count('') returns len+1=6, but count=1 still
        # proceeds (replacing the first empty occurrence at position 0).
        assert "replaced 1 occurrence(s)" in result
        assert (root / "f.txt").read_text() == "Xhello"

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

    def test_edit_across_extra_root(self, tmp_path, settings):
        """A file in an extra root is editable via '../extra/...' when
        extra_roots is set."""
        root = tmp_path / "repos" / "primary"
        extra = tmp_path / "repos" / "extra"
        root.mkdir(parents=True)
        extra.mkdir(parents=True)
        _make_file(extra, "f.py", "x = 1\ny = 2\n")
        tools = _build_extra(root, settings, [extra])
        result = tools["edit_file"]("../extra/f.py", "y = 2", "y = 3")
        assert "replaced 1 occurrence(s) in ../extra/f.py" in result
        assert (extra / "f.py").read_text() == "x = 1\ny = 3\n"

    def test_edit_file_python_syntax_error_refused(self, tmp_path, settings):
        """An edit that would leave the .py file with a SyntaxError must
        be refused without writing — agent gets the diagnostic and can
        retry without burning a test cycle."""
        root = tmp_path / "repo"
        root.mkdir()
        original = "def f():\n    return 1\n"
        _make_file(root, "mod.py", original)
        tools = _build(root, settings)
        # Replace the body with malformed Python.
        result = tools["edit_file"](
            "mod.py",
            "    return 1",
            "    return (",
        )
        assert "syntax error" in result.lower()
        # File on disk unchanged.
        assert (root / "mod.py").read_text() == original


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

        def _capture(
            command, *, repo_dir, settings, epic_workspace_path=None, **kwargs
        ):
            cap["command"] = command
            cap["repo_dir"] = repo_dir
            cap["settings"] = settings
            cap["epic_workspace_path"] = epic_workspace_path
            cap["sandbox_image"] = kwargs.get("sandbox_image")
            return (0, "ok")

        monkeypatch.setattr(sandbox, "run", _capture)
        result = tools["run_command"]("pytest tests/")
        assert result == "exit=0\nok"
        assert cap["command"] == "pytest tests/"
        assert cap["repo_dir"] == root
        assert cap["settings"] is settings
        # No sandbox_image passed to build_fs_tools → forwarded as None.
        assert cap["sandbox_image"] is None

    def test_run_command_forwards_sandbox_image(self, tmp_path, settings, monkeypatch):
        """build_fs_tools(sandbox_image=...) threads the image into the
        run_command tool's sandbox.run call."""
        root = tmp_path / "repo"
        root.mkdir()
        tools = {
            t.__name__: t
            for t in build_fs_tools(
                root, settings, sandbox_image="ros:rolling-ros-base"
            )
        }
        cap = {}

        def _capture(command, *, repo_dir, settings, **kwargs):
            cap["sandbox_image"] = kwargs.get("sandbox_image")
            return (0, "ok")

        monkeypatch.setattr(sandbox, "run", _capture)
        tools["run_command"]("pytest tests/")
        assert cap["sandbox_image"] == "ros:rolling-ros-base"

    def test_empty_output_success(self, tmp_path, settings, fake_sandbox):
        """Successful command with no output returns a friendly message."""
        root = tmp_path / "repo"
        root.mkdir()
        tools = _build(root, settings)
        result = tools["run_command"]("true")
        assert result == "Your command ran successfully and did not produce any output."

    def test_empty_output_failure(self, tmp_path, settings, monkeypatch):
        """Failed command with no output returns an informative message."""
        root = tmp_path / "repo"
        root.mkdir()
        tools = _build(root, settings)

        def _empty_fail(*a, **kw):
            return (2, "")

        monkeypatch.setattr(sandbox, "run", _empty_fail)
        result = tools["run_command"]("failing-command")
        assert result == "The command failed with exit code 2 and produced no output."


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
        result = tools["read_file"](path="no-such-file.txt")
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
                r = tool(path="f.txt")
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


# ===================================================================
# File-read cache
# ===================================================================


class TestFileReadCache:
    """Tests for the in-memory file-content cache in ``build_fs_tools``."""

    def test_repeated_read_hits_cache(self, tmp_path, settings):
        """First full-file read returns content; second also returns the
        same content (served from the in-memory cache, not re-read from disk)."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "f.txt", "original\n")
        tools = _build(root, settings)

        first = tools["read_file"](path="f.txt")
        assert first == "original\n"

        second = tools["read_file"](path="f.txt")
        assert second == "original\n"

    def test_offset_limit_still_hits_cache(self, tmp_path, settings):
        """A full read populates the cache; a subsequent offset/limit
        read hits the cache and slices correctly."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "f.txt", "line1\nline2\nline3\n")
        tools = _build(root, settings)

        # Populate cache with full read.
        tools["read_file"](path="f.txt")

        # Mutate on disk.
        (root / "f.txt").write_text("x\ny\nz\n", encoding="utf-8")

        # Offset/limit read should still return cached original.
        result = tools["read_file"](path="f.txt", offset=2, limit=1)
        assert result == "line2\n"

    def test_write_file_invalidates(self, tmp_path, settings):
        """After write_file, a subsequent read_file sees the new content."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "f.txt", "old\n")
        tools = _build(root, settings)

        # Populate cache.
        assert tools["read_file"](path="f.txt") == "old\n"

        # Write new content.
        tools["write_file"]("f.txt", "new\n")
        # Read must see the new content.
        assert tools["read_file"](path="f.txt") == "new\n"

    def test_edit_file_invalidates(self, tmp_path, settings):
        """After edit_file, a subsequent read_file sees the edited content."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "f.txt", "hello world\n")
        tools = _build(root, settings)

        # Populate cache.
        assert tools["read_file"](path="f.txt") == "hello world\n"

        # Edit.
        tools["edit_file"]("f.txt", "hello", "HELLO")
        # Read must see edited content.
        assert tools["read_file"](path="f.txt") == "HELLO world\n"

    def test_delete_file_invalidates(self, tmp_path, settings):
        """After delete_file, the cache entry is removed.  A subsequent
        read_file of a freshly created file with the same name sees the
        new content."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "f.txt", "first\n")
        tools = _build(root, settings)

        # Populate cache.
        assert tools["read_file"](path="f.txt") == "first\n"

        # Delete.
        tools["delete_file"]("f.txt")

        # Create a fresh file at the same path.
        _make_file(root, "f.txt", "second\n")
        assert tools["read_file"](path="f.txt") == "second\n"

    def test_equivalent_paths_same_cache_entry(self, tmp_path, settings):
        """``read_file("foo/bar.py")`` and ``read_file("./foo/bar.py")``
        hit the same cache entry — the second full-file read returns the
        same cached content."""
        root = tmp_path / "repo"
        root.mkdir()
        (root / "sub").mkdir()
        _make_file(root, "sub/file.txt", "cached\n")
        tools = _build(root, settings)

        # First read via a path with a dot prefix.
        first = tools["read_file"](path="./sub/file.txt")
        assert first == "cached\n"

        # Second read via the normal path — must hit cache.
        second = tools["read_file"](path="sub/file.txt")
        assert second == "cached\n"

    def test_parent_dotdot_paths_same_cache_entry(self, tmp_path, settings):
        """``read_file("sub/../sub/file.txt")`` resolves to the same
        canonical path as ``read_file("sub/file.txt")`` — the second
        full-file read returns the same cached content."""
        root = tmp_path / "repo"
        root.mkdir()
        (root / "sub").mkdir()
        _make_file(root, "sub/file.txt", "cached\n")
        tools = _build(root, settings)

        first = tools["read_file"](path="sub/file.txt")
        assert first == "cached\n"

        second = tools["read_file"](path="sub/../sub/file.txt")
        assert second == "cached\n"

    def test_error_returns_not_cached(self, tmp_path, settings):
        """read_file of a nonexistent file returns an error string and
        does not populate the cache.  A later creation + read must see
        the new content."""
        root = tmp_path / "repo"
        root.mkdir()
        tools = _build(root, settings)

        # First call on a nonexistent file → error, not cached.
        result = tools["read_file"](path="nonexistent.txt")
        assert "error" in result.lower()

        # Create the file and read — must read from disk.
        _make_file(root, "nonexistent.txt", "now exists\n")
        assert tools["read_file"](path="nonexistent.txt") == "now exists\n"

    def test_escape_error_not_cached(self, tmp_path, settings):
        """read_file of a path outside root returns an error string and
        does not populate the cache."""
        root = tmp_path / "repo"
        root.mkdir()
        tools = _build(root, settings)

        result = tools["read_file"](path="../../../etc/passwd")
        assert "error" in result.lower()

        # The error must be actionable: it still names the escape, says the
        # path is unreachable by all fs tools, and tells the model not to
        # fall back to run_command.
        assert "escapes the repository" in result
        assert "run_command" in result
        assert "Do NOT retry" in result

        # A subsequent read of a valid file with name "passwd" in root
        # must not be poisoned by the prior error.
        _make_file(root, "passwd", "local\n")
        assert tools["read_file"](path="passwd") == "local\n"

    def test_escape_error_actionable_absolute(self, tmp_path, settings):
        """read_file of an out-of-repo absolute path (e.g. a site-packages
        file) returns the enriched, actionable escape error."""
        root = tmp_path / "repo"
        root.mkdir()
        tools = _build(root, settings)

        result = tools["read_file"](
            path="/usr/local/lib/python3.14/site-packages/foo.py"
        )
        assert "escapes the repository" in result
        assert "run_command" in result
        assert "Do NOT retry" in result

    def test_repeat_full_read_returns_content(self, tmp_path, settings):
        """First full-file read returns content; second returns the same
        cached content (not a stub)."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "f.txt", "hello\n")
        tools = _build(root, settings)

        first = tools["read_file"](path="f.txt")
        assert first == "hello\n"

        second = tools["read_file"](path="f.txt")
        assert second == "hello\n"

    def test_offset_limit_read_not_stubbed(self, tmp_path, settings):
        """Full read populates cache; offset/limit read still returns
        the actual slice, not a stub."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "f.txt", "line1\nline2\nline3\n")
        tools = _build(root, settings)

        # Populate cache with full read.
        tools["read_file"](path="f.txt")

        # Offset/limit read returns slice from cache.
        result = tools["read_file"](path="f.txt", offset=2, limit=1)
        assert result == "line2\n"

    def test_after_edit_subsequent_read_returns_content(self, tmp_path, settings):
        """After edit_file invalidates cache, first read returns fresh
        content; second read returns the same (now cached) content."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "f.txt", "hello world\n")
        tools = _build(root, settings)

        # Read → content.
        assert tools["read_file"](path="f.txt") == "hello world\n"

        # Edit → invalidates.
        tools["edit_file"]("f.txt", "hello", "HELLO")

        # First read after edit → fresh content from disk.
        assert tools["read_file"](path="f.txt") == "HELLO world\n"

        # Second read → same cached content.
        assert tools["read_file"](path="f.txt") == "HELLO world\n"

    def test_after_write_subsequent_read_returns_content(self, tmp_path, settings):
        """After write_file invalidates cache, first read returns new
        content; second read returns the same (now cached) content."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "f.txt", "old\n")
        tools = _build(root, settings)

        # Read → content.
        assert tools["read_file"](path="f.txt") == "old\n"

        # Write → invalidates.
        tools["write_file"]("f.txt", "new\n")

        # First read after write → fresh content from disk.
        assert tools["read_file"](path="f.txt") == "new\n"

        # Second read → same cached content.
        assert tools["read_file"](path="f.txt") == "new\n"


# ===================================================================
# read_file message-history pruning
# ===================================================================


def _read_call(path, tool_call_id):
    """A ModelResponse carrying a single read_file ToolCallPart."""
    return ModelResponse(
        parts=[
            ToolCallPart(
                tool_name="read_file",
                args={"path": path},
                tool_call_id=tool_call_id,
            )
        ]
    )


def _read_return(content, tool_call_id):
    """A read_file ToolReturnPart (returned bare so a test can assert
    on its ``.content`` after pruning)."""
    return ToolReturnPart(
        tool_name="read_file",
        content=content,
        tool_call_id=tool_call_id,
    )


class TestFileReadPruning:
    """``read_file`` prunes now-stale full-file content from the live
    pydantic-ai message history on a fresh full read."""

    def test_prune_after_edit(self, tmp_path, settings):
        """Read A, edit A, read A again → the old full-content message
        for A is replaced with the pruned placeholder."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "A.txt", "original A\n")
        tools = _build(root, settings)

        a_return = _read_return("original A\n", "call-A")
        ctx = types.SimpleNamespace(
            messages=[
                _read_call("A.txt", "call-A"),
                ModelRequest(parts=[a_return]),
            ]
        )

        # Edit A → invalidates the cache.
        tools["edit_file"]("A.txt", "original", "edited")

        # Fresh full read → fresh content returned, stale copy pruned.
        result = tools["read_file"](ctx, path="A.txt")
        assert result == "edited A\n"
        assert a_return.content == _PRUNED_PLACEHOLDER

    def test_prune_after_write(self, tmp_path, settings):
        """write_file also makes the prior read stale and prunable."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "A.txt", "v1\n")
        tools = _build(root, settings)

        a_return = _read_return("v1\n", "call-A")
        ctx = types.SimpleNamespace(
            messages=[
                _read_call("A.txt", "call-A"),
                ModelRequest(parts=[a_return]),
            ]
        )

        tools["write_file"]("A.txt", "v2\n")

        result = tools["read_file"](ctx, path="A.txt")
        assert result == "v2\n"
        assert a_return.content == _PRUNED_PLACEHOLDER

    def test_unrelated_file_untouched(self, tmp_path, settings):
        """Read A, read B, edit A, read A → A is pruned; B's ToolCallPart
        and ToolReturnPart are both left intact."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "A.txt", "A original\n")
        _make_file(root, "B.txt", "B original\n")
        tools = _build(root, settings)

        a_return = _read_return("A original\n", "call-A")
        b_call = ToolCallPart(
            tool_name="read_file",
            args={"path": "B.txt"},
            tool_call_id="call-B",
        )
        b_return = _read_return("B original\n", "call-B")
        ctx = types.SimpleNamespace(
            messages=[
                _read_call("A.txt", "call-A"),
                ModelRequest(parts=[a_return]),
                ModelResponse(parts=[b_call]),
                ModelRequest(parts=[b_return]),
            ]
        )

        tools["edit_file"]("A.txt", "original", "edited")
        tools["read_file"](ctx, path="A.txt")

        assert a_return.content == _PRUNED_PLACEHOLDER
        assert b_return.content == "B original\n"
        assert b_call.args_as_dict() == {"path": "B.txt"}

    def test_non_read_file_results_untouched(self, tmp_path, settings):
        """Only read_file ToolReturnParts are pruned — run_command and
        other tool results are never touched."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "A.txt", "v1\n")
        tools = _build(root, settings)

        cmd_return = ToolReturnPart(
            tool_name="run_command",
            content="exit=0\nbig output",
            tool_call_id="cmd-1",
        )
        a_return = _read_return("v1\n", "call-A")
        ctx = types.SimpleNamespace(
            messages=[
                ModelResponse(
                    parts=[
                        ToolCallPart(
                            tool_name="run_command",
                            args={"command": "ls"},
                            tool_call_id="cmd-1",
                        )
                    ]
                ),
                ModelRequest(parts=[cmd_return]),
                _read_call("A.txt", "call-A"),
                ModelRequest(parts=[a_return]),
            ]
        )

        tools["write_file"]("A.txt", "v2\n")
        tools["read_file"](ctx, path="A.txt")

        assert a_return.content == _PRUNED_PLACEHOLDER
        assert cmd_return.content == "exit=0\nbig output"

    def test_no_prune_on_partial_read(self, tmp_path, settings):
        """An offset/limit read never triggers pruning of earlier
        partial reads. (Prior history is a partial read so the
        already-loaded refusal doesn't kick in; this test just
        verifies the pruning invariant.)"""
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "A.txt", "l1\nl2\nl3\n")
        tools = _build(root, settings)

        a_call = ToolCallPart(
            tool_name="read_file",
            args={"path": "A.txt", "offset": 1, "limit": 1},
            tool_call_id="call-A",
        )
        a_return = _read_return("l1\n", "call-A")
        ctx = types.SimpleNamespace(
            messages=[
                ModelResponse(parts=[a_call]),
                ModelRequest(parts=[a_return]),
            ]
        )

        result = tools["read_file"](ctx, path="A.txt", offset=2, limit=1)
        assert result == "l2\n"
        assert a_return.content == "l1\n"

    def test_no_prune_on_error(self, tmp_path, settings):
        """An error read (missing file) never triggers pruning."""
        root = tmp_path / "repo"
        root.mkdir()
        tools = _build(root, settings)

        a_return = _read_return("stale", "call-A")
        ctx = types.SimpleNamespace(
            messages=[
                _read_call("missing.txt", "call-A"),
                ModelRequest(parts=[a_return]),
            ]
        )

        result = tools["read_file"](ctx, path="missing.txt")
        assert "error" in result.lower()
        assert a_return.content == "stale"

    def test_no_prune_when_ctx_none(self, tmp_path, settings):
        """Called without a RunContext (the unit-test default), read_file
        returns content and skips pruning entirely — no crash."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "A.txt", "content\n")
        tools = _build(root, settings)

        assert tools["read_file"](path="A.txt") == "content\n"

    def test_prune_path_canonicalization(self, tmp_path, settings):
        """A stale read recorded as './sub/file.txt' is matched when the
        fresh read uses 'sub/file.txt' — both resolve to one path."""
        root = tmp_path / "repo"
        root.mkdir()
        (root / "sub").mkdir()
        _make_file(root, "sub/file.txt", "v1\n")
        tools = _build(root, settings)

        a_return = _read_return("v1\n", "call-A")
        ctx = types.SimpleNamespace(
            messages=[
                _read_call("./sub/file.txt", "call-A"),
                ModelRequest(parts=[a_return]),
            ]
        )

        tools["edit_file"]("sub/file.txt", "v1", "v2")
        result = tools["read_file"](ctx, path="sub/file.txt")
        assert result == "v2\n"
        assert a_return.content == _PRUNED_PLACEHOLDER

    def test_history_structure_intact_after_prune(self, tmp_path, settings):
        """Pruning replaces only the stale ToolReturnPart content — the
        ToolCallPart and the message-list shape are untouched, and a
        follow-up read still works."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "A.txt", "v1\n")
        tools = _build(root, settings)

        a_call_msg = _read_call("A.txt", "call-A")
        a_return = _read_return("v1\n", "call-A")
        ctx = types.SimpleNamespace(
            messages=[
                a_call_msg,
                ModelRequest(parts=[a_return]),
            ]
        )

        tools["write_file"]("A.txt", "v2\n")
        tools["read_file"](ctx, path="A.txt")

        # Same number of messages; ToolCallPart untouched.
        assert len(ctx.messages) == 2
        assert ctx.messages[0] is a_call_msg
        assert a_call_msg.parts[0].args_as_dict() == {"path": "A.txt"}
        assert a_return.content == _PRUNED_PLACEHOLDER

        # A follow-up read still returns the cached content.
        assert tools["read_file"](ctx, path="A.txt") == "v2\n"


class TestPartialReadRefusalWhenAlreadyLoaded:
    """``read_file`` refuses partial-slice calls when the same file's
    content is already present in the conversation history as a
    non-pruned full read — either a preload or a prior runtime full
    read. Encourages reuse of the in-context copy rather than layering
    a slice on top of the still-present full content."""

    def _preload_msgs(self, path, content):
        """Build (call, return) shaped like build_preseed_history."""
        call = ToolCallPart(
            tool_name="read_file",
            args={"path": path, "offset": 1, "limit": None},
            tool_call_id=f"preload_{path}",
        )
        ret = ToolReturnPart(
            tool_name="read_file",
            content=content,
            tool_call_id=f"preload_{path}",
        )
        return ModelResponse(parts=[call]), ModelRequest(parts=[ret])

    def test_partial_refused_when_file_preloaded(self, tmp_path, settings):
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "A.txt", "l1\nl2\nl3\nl4\nl5\n")
        tools = _build(root, settings)

        resp, req = self._preload_msgs("A.txt", "l1\nl2\nl3\nl4\nl5\n")
        ctx = types.SimpleNamespace(messages=[resp, req])

        result = tools["read_file"](ctx, path="A.txt", offset=2, limit=2)
        assert result.startswith("refused:")
        assert "A.txt" in result

    def test_partial_refused_after_prior_full_runtime_read(
        self,
        tmp_path,
        settings,
    ):
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "A.txt", "l1\nl2\nl3\n")
        tools = _build(root, settings)

        a_call = ToolCallPart(
            tool_name="read_file",
            args={"path": "A.txt", "offset": 1, "limit": None},
            tool_call_id="call-A",
        )
        a_return = _read_return("l1\nl2\nl3\n", "call-A")
        ctx = types.SimpleNamespace(
            messages=[
                ModelResponse(parts=[a_call]),
                ModelRequest(parts=[a_return]),
            ]
        )

        result = tools["read_file"](ctx, path="A.txt", offset=2, limit=1)
        assert result.startswith("refused:")

    def test_partial_allowed_when_only_partial_prior(self, tmp_path, settings):
        """A prior PARTIAL read doesn't block a later partial — the
        slice may cover a different range. Only full prior reads
        block."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "A.txt", "l1\nl2\nl3\nl4\nl5\n")
        tools = _build(root, settings)

        a_call = ToolCallPart(
            tool_name="read_file",
            args={"path": "A.txt", "offset": 1, "limit": 2},
            tool_call_id="call-A",
        )
        a_return = _read_return("l1\nl2\n", "call-A")
        ctx = types.SimpleNamespace(
            messages=[
                ModelResponse(parts=[a_call]),
                ModelRequest(parts=[a_return]),
            ]
        )

        result = tools["read_file"](ctx, path="A.txt", offset=3, limit=2)
        assert result == "l3\nl4\n"

    def test_partial_allowed_when_preload_was_pruned(
        self,
        tmp_path,
        settings,
    ):
        """A pruned prior full read no longer counts — its content is
        gone from context, so a partial read is allowed again."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "A.txt", "l1\nl2\nl3\n")
        tools = _build(root, settings)

        resp, req = self._preload_msgs("A.txt", "l1\nl2\nl3\n")
        # Pretend the preload was already pruned by an earlier full re-read.
        req.parts[0].content = _PRUNED_PLACEHOLDER
        ctx = types.SimpleNamespace(messages=[resp, req])

        result = tools["read_file"](ctx, path="A.txt", offset=2, limit=1)
        assert result == "l2\n"

    def test_full_read_of_preloaded_file_still_allowed(
        self,
        tmp_path,
        settings,
    ):
        """Full reads always go through — they trigger pruning of the
        earlier copy. Refusal only targets partial slices."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "A.txt", "l1\nl2\nl3\n")
        tools = _build(root, settings)

        resp, req = self._preload_msgs("A.txt", "l1\nl2\nl3\n")
        ctx = types.SimpleNamespace(messages=[resp, req])

        result = tools["read_file"](ctx, path="A.txt")
        assert result == "l1\nl2\nl3\n"
        # The preload's return content was pruned to make room.
        assert req.parts[0].content == _PRUNED_PLACEHOLDER

    def test_partial_of_unrelated_file_still_allowed(
        self,
        tmp_path,
        settings,
    ):
        """A preload of A.txt doesn't block partial reads of B.txt."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "A.txt", "A\n")
        _make_file(root, "B.txt", "b1\nb2\nb3\n")
        tools = _build(root, settings)

        resp, req = self._preload_msgs("A.txt", "A\n")
        ctx = types.SimpleNamespace(messages=[resp, req])

        result = tools["read_file"](ctx, path="B.txt", offset=2, limit=1)
        assert result == "b2\n"

    def test_no_ctx_skips_refusal(self, tmp_path, settings):
        """Bare invocations (no RunContext) never refuse — only the
        agent's live calls go through the history check."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "A.txt", "l1\nl2\nl3\n")
        tools = _build(root, settings)

        result = tools["read_file"](path="A.txt", offset=2, limit=1)
        assert result == "l2\n"


# ===================================================================
# read_file — PDF support
# ===================================================================


def _make_text_pdf(path: str, text: str) -> None:
    """Create a minimal single-page PDF with extractable *text*.

    Uses ``pypdf`` to build a page with a content stream that draws
    *text* via standard PDF text-showing operators.
    """
    from pypdf import PdfWriter
    from pypdf.generic import (
        NameObject,
        DictionaryObject,
        ContentStream,
        StreamObject,
    )

    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)

    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream_data = f"BT /F1 12 Tf 100 700 Td ({escaped}) Tj ET"

    # Wrap the raw bytes in a StreamObject so ContentStream can read them.
    stm = StreamObject()
    stm.set_data(stream_data.encode("latin-1"))
    content_stream = ContentStream(stm, writer)

    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)

    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref}),
        }
    )
    page[NameObject("/Contents")] = writer._add_object(content_stream)

    with open(path, "wb") as f:
        writer.write(f)


def _make_multipage_text_pdf(path: str, texts: list[str]) -> None:
    """Create a multi-page PDF where each page draws one text string."""
    from pypdf import PdfWriter
    from pypdf.generic import (
        NameObject,
        DictionaryObject,
        ContentStream,
        StreamObject,
    )

    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)

    for text in texts:
        page = writer.add_blank_page(width=612, height=792)
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream_data = f"BT /F1 12 Tf 100 700 Td ({escaped}) Tj ET"
        stm = StreamObject()
        stm.set_data(stream_data.encode("latin-1"))
        content_stream = ContentStream(stm, writer)
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref}),
            }
        )
        page[NameObject("/Contents")] = writer._add_object(content_stream)

    with open(path, "wb") as f:
        writer.write(f)


def _make_empty_pdf(path: str) -> None:
    """Create a minimal PDF with no text layer (blank page only)."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with open(path, "wb") as f:
        writer.write(f)


def _make_encrypted_pdf(path: str, password: str = "secret123") -> None:
    """Create a password-protected PDF with extractable text."""
    _make_text_pdf(path, "secret content")
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(path)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(password)
    with open(path, "wb") as f:
        writer.write(f)


def _make_corrupted_pdf(path: str) -> None:
    """Write arbitrary non-PDF bytes to a ``.pdf`` file."""
    from pathlib import Path

    Path(path).write_bytes(b"this is not a PDF\x00\xff\xfe\xfd")


class TestReadFilePDF:
    """``read_file`` behaviour on ``.pdf`` files: text extraction via
    ``pypdf``, encrypted detection, error handling, and integration
    with offset/limit / caching."""

    def test_extracts_text_from_valid_pdf(self, tmp_path, settings):
        """``read_file`` on a valid .pdf returns the extracted text
        from the PDF's text layer."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_text_pdf(str(root / "doc.pdf"), "Hello PDF world")
        tools = _build(root, settings)
        result = tools["read_file"](path="doc.pdf")
        assert "Hello PDF world" in result

    def test_empty_pdf_no_text_layer(self, tmp_path, settings):
        """A PDF with no text layer (blank page) returns an empty string."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_empty_pdf(str(root / "blank.pdf"))
        tools = _build(root, settings)
        result = tools["read_file"](path="blank.pdf")
        assert result == ""

    def test_encrypted_pdf_returns_error(self, tmp_path, settings):
        """An encrypted PDF returns an error string starting with the
        prescribed prefix."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_encrypted_pdf(str(root / "locked.pdf"), "secret123")
        tools = _build(root, settings)
        result = tools["read_file"](path="locked.pdf")
        assert isinstance(result, str)
        assert result.startswith("error: PDF is encrypted")

    def test_corrupted_pdf_returns_error(self, tmp_path, settings):
        """A file with ``.pdf`` extension that is not a valid PDF returns
        an error string."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_corrupted_pdf(str(root / "bad.pdf"))
        tools = _build(root, settings)
        result = tools["read_file"](path="bad.pdf")
        assert isinstance(result, str)
        assert result.startswith("error reading PDF")

    def test_non_pdf_files_unchanged(self, tmp_path, settings):
        """Non-.pdf files are read via ``read_text`` exactly as before —
        no regression."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "hello.txt", "world\n")
        tools = _build(root, settings)
        result = tools["read_file"](path="hello.txt")
        assert result == "world\n"

    def test_offset_limit_on_pdf(self, tmp_path, settings):
        """``offset`` and ``limit`` slice the extracted PDF text by
        lines, just like a text file."""
        root = tmp_path / "repo"
        root.mkdir()
        # Create a multi-page PDF — each page draws one distinct line.
        _make_multipage_text_pdf(
            str(root / "multi.pdf"),
            ["First page text", "Second page text", "Third page text"],
        )
        tools = _build(root, settings)

        # Full read should contain all three texts (each on its own line).
        full = tools["read_file"](path="multi.pdf")
        assert "First page text" in full
        assert "Second page text" in full
        assert "Third page text" in full

        # offset=2, limit=1 → only the second line.
        sliced = tools["read_file"](path="multi.pdf", offset=2, limit=1)
        lines = sliced.strip().split("\n")
        assert len(lines) == 1
        assert "Second page text" in sliced
        assert "First page text" not in sliced
        assert "Third page text" not in sliced

    def test_pdf_caching(self, tmp_path, settings):
        """Two consecutive ``read_file`` calls on the same .pdf return
        the same extracted text (cache hit)."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_text_pdf(str(root / "doc.pdf"), "Cache me")
        tools = _build(root, settings)

        first = tools["read_file"](path="doc.pdf")
        second = tools["read_file"](path="doc.pdf")
        assert first == second
        assert "Cache me" in first

    def test_pdf_caching_equivalent_paths(self, tmp_path, settings):
        """Different path strings that resolve to the same PDF hit the
        same cache entry."""
        root = tmp_path / "repo"
        root.mkdir()
        (root / "sub").mkdir()
        _make_text_pdf(str(root / "sub" / "doc.pdf"), "Cached PDF")
        tools = _build(root, settings)

        first = tools["read_file"](path="./sub/doc.pdf")
        second = tools["read_file"](path="sub/doc.pdf")
        # Same cache entry → same extracted text (not re-extracted).
        assert first == second
        assert "Cached PDF" in first

    def test_pdf_cache_invalidated_by_write(self, tmp_path, settings):
        """After ``write_file`` overwrites a ``.pdf``, ``read_file``
        re-reads from disk: the cache is invalidated, and the fresh
        read reflects whatever is on disk now (even if it's no longer
        a valid PDF)."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_text_pdf(str(root / "doc.pdf"), "Old PDF")
        tools = _build(root, settings)

        assert "Old PDF" in tools["read_file"](path="doc.pdf")

        # Overwrite via write_file — this invalidates the cache.
        tools["write_file"]("doc.pdf", "plain text, not a PDF")

        # Re-read: the cache was cleared, so _read_cached hits the disk,
        # sees a .pdf extension, and _extract_pdf_text fails to parse the
        # plain-text content as a PDF.
        result = tools["read_file"](path="doc.pdf")
        assert "error reading PDF" in result
        assert "not a PDF" not in result  # it's a parse error, not the raw text

    def test_pdf_cache_invalidated_by_edit(self, tmp_path, settings):
        """After ``edit_file`` on a ``.pdf``, the cache is cleared and a
        subsequent ``read_file`` returns the fresh file contents."""
        root = tmp_path / "repo"
        root.mkdir()
        # write_file can create a .pdf but we need a valid PDF to read
        # it.  Use a plain .txt file for the edit→cache test — the
        # cache-invalidation path is shared across all file types.
        _make_file(root, "f.txt", "hello world\n")
        tools = _build(root, settings)

        assert tools["read_file"](path="f.txt") == "hello world\n"
        tools["edit_file"]("f.txt", "hello", "HELLO")
        assert tools["read_file"](path="f.txt") == "HELLO world\n"

    def test_pdf_lazy_import(self, tmp_path, settings):
        """``pypdf`` is not imported until a ``.pdf`` path is actually
        read — importing ``fs_tools`` and calling ``build_fs_tools``
        leaves ``pypdf`` out of ``sys.modules``."""
        import sys

        # Ensure pypdf is not already loaded from prior tests.
        sys.modules.pop("pypdf", None)

        root = tmp_path / "repo"
        root.mkdir()
        tools = _build(root, settings)

        assert "pypdf" not in sys.modules, (
            "pypdf should not be imported until a .pdf is read"
        )

        # Reading a .pdf triggers the lazy import.
        _make_text_pdf(str(root / "doc.pdf"), "Trigger import")
        tools["read_file"](path="doc.pdf")
        assert "pypdf" in sys.modules, "pypdf should be imported after reading a .pdf"

    def test_pdf_extension_case_insensitive(self, tmp_path, settings):
        """``.PDF`` (upper-case) is handled the same as ``.pdf``."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_text_pdf(str(root / "DOC.PDF"), "Case test")
        tools = _build(root, settings)
        result = tools["read_file"](path="DOC.PDF")
        assert "Case test" in result


class TestBuildPreseedHistoryPDF:
    """``build_preseed_history`` extracts text from ``.pdf`` files via
    ``_extract_pdf_text``, matching the behaviour of ``_read_cached``."""

    def test_valid_pdf_returns_extracted_text(self, tmp_path):
        """``build_preseed_history`` with a valid ``.pdf`` returns
        extracted text, not raw binary mojibake."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_text_pdf(str(root / "spec.pdf"), "Hello PDF world")

        history = build_preseed_history(root, ["spec.pdf"])
        # history is [ModelResponse(calls), ModelRequest(returns)]
        assert len(history) == 2

        calls_msg, returns_msg = history
        assert isinstance(calls_msg, ModelResponse)
        assert isinstance(returns_msg, ModelRequest)

        # The ToolReturnPart content should be the extracted text.
        return_content = returns_msg.parts[0].content
        assert "Hello PDF world" in return_content
        # Must NOT be raw PDF binary mojibake.
        assert "%PDF" not in return_content

    def test_corrupted_pdf_returns_error_string(self, tmp_path):
        """A file with ``.pdf`` extension that is not a valid PDF
        returns the error string from ``_extract_pdf_text``."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_corrupted_pdf(str(root / "bad.pdf"))

        history = build_preseed_history(root, ["bad.pdf"])
        assert len(history) == 2
        _, returns_msg = history
        return_content = returns_msg.parts[0].content
        assert isinstance(return_content, str)
        assert return_content.startswith("error reading PDF")

    def test_non_pdf_files_unchanged(self, tmp_path):
        """Non-``.pdf`` files are read via ``read_text`` exactly as
        before — no regression."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_file(root, "hello.txt", "world\n")

        history = build_preseed_history(root, ["hello.txt"])
        assert len(history) == 2
        _, returns_msg = history
        assert returns_msg.parts[0].content == "world\n"

    def test_mixed_pdf_and_text_files(self, tmp_path):
        """A mix of ``.pdf`` and text files: each gets the correct
        reader (``_extract_pdf_text`` vs ``read_text``)."""
        root = tmp_path / "repo"
        root.mkdir()
        _make_text_pdf(str(root / "a.pdf"), "PDF content")
        _make_file(root, "b.txt", "text content\n")

        history = build_preseed_history(root, ["a.pdf", "b.txt"])
        assert len(history) == 2
        _, returns_msg = history

        parts = returns_msg.parts
        assert len(parts) == 2
        contents = {p.content for p in parts}
        assert "PDF content" in contents
        assert "text content\n" in contents
