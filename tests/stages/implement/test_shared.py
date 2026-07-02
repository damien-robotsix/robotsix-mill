"""Unit tests for the implement-stage shared helpers.

Covers ``_is_config_only_change`` and ``_should_skip_test_gate``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from robotsix_mill.stages.implement._shared import (
    CONFIG_ONLY_EXTENSIONS,
    _is_config_only_change,
    _is_rename_only_change,
    _is_spec_exact_edits,
    _parse_spec_code_blocks,
    _should_skip_test_gate,
)


# ---------------------------------------------------------------------------
# _is_config_only_change
# ---------------------------------------------------------------------------


class TestIsConfigOnlyChange:
    """Tests for ``_is_config_only_change`` using a real git repo fixture."""

    @pytest.fixture
    def git_repo(self, tmp_path: Path) -> Path:
        """Create a temp git repo with an origin/main ref for diffing."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.name", "Test"],
            check=True,
        )
        # Create an initial commit so we have a baseline.
        (repo / "README.md").write_text("# Test Repo")
        subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "initial"], check=True)
        # Create a branch named "main" so origin/main is resolvable.
        subprocess.run(["git", "-C", str(repo), "branch", "-M", "main"], check=True)
        # Create a bare "remote" at tmp_path/remote.git and push to it
        # so origin/main exists as a remote ref.
        remote = tmp_path / "remote.git"
        remote.mkdir()
        subprocess.run(["git", "-C", str(remote), "init", "--bare"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "remote", "add", "origin", str(remote)],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "push", "-u", "origin", "main"],
            check=True,
        )
        return repo

    def test_all_config_only(self, git_repo: Path) -> None:
        """All changed files are config-only → True."""
        # Add a docs markdown file.
        docs = git_repo / "docs"
        docs.mkdir()
        (docs / "x.md").write_text("# X")
        (git_repo / "config.yaml").write_text("key: val")
        subprocess.run(["git", "-C", str(git_repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(git_repo), "commit", "-m", "config + docs"],
            check=True,
        )
        assert _is_config_only_change(git_repo, "main") is True

    def test_mixed_with_py_file(self, git_repo: Path) -> None:
        """A .py file in the mix → False."""
        (git_repo / "src.py").write_text("x = 1")
        (git_repo / "docs").mkdir(exist_ok=True)
        (git_repo / "docs" / "x.md").write_text("# X")
        subprocess.run(["git", "-C", str(git_repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(git_repo), "commit", "-m", "mixed"],
            check=True,
        )
        assert _is_config_only_change(git_repo, "main") is False

    def test_js_file_is_not_config(self, git_repo: Path) -> None:
        """A .js file → False (not a config-only extension)."""
        (git_repo / "app.js").write_text("console.log(1)")
        subprocess.run(["git", "-C", str(git_repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(git_repo), "commit", "-m", "js"],
            check=True,
        )
        assert _is_config_only_change(git_repo, "main") is False

    def test_no_diff_returns_false(self, git_repo: Path) -> None:
        """No diff vs origin/main → False (run tests as safe default)."""
        assert _is_config_only_change(git_repo, "main") is False

    def test_uppercase_extension(self, git_repo: Path) -> None:
        """Uppercase extensions like .YAML / .MD are still config-only."""
        (git_repo / "SETTINGS.YAML").write_text("k: v")
        subprocess.run(["git", "-C", str(git_repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(git_repo), "commit", "-m", "uppercase"],
            check=True,
        )
        assert _is_config_only_change(git_repo, "main") is True

    def test_git_failure_returns_false(self, tmp_path: Path) -> None:
        """Nonexistent origin/<branch> → git diff fails → False."""
        repo = tmp_path / "noremote"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init"], check=True)
        assert _is_config_only_change(repo, "nonexistent") is False

    def test_css_file_is_not_config(self, git_repo: Path) -> None:
        """A .css file → False."""
        (git_repo / "style.css").write_text("body {}")
        subprocess.run(["git", "-C", str(git_repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(git_repo), "commit", "-m", "css"],
            check=True,
        )
        assert _is_config_only_change(git_repo, "main") is False

    def test_html_file_is_not_config(self, git_repo: Path) -> None:
        """A .html file → False."""
        (git_repo / "page.html").write_text("<html></html>")
        subprocess.run(["git", "-C", str(git_repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(git_repo), "commit", "-m", "html"],
            check=True,
        )
        assert _is_config_only_change(git_repo, "main") is False

    # -- working-tree detection (unstaged edits from a prior retry) --

    def test_working_tree_config_only(self, git_repo: Path) -> None:
        """Unstaged config-only tracked files in the working tree → True."""
        # Create and commit a config-only file, then modify it unstaged.
        (git_repo / "config.yaml").write_text("key: v1")
        subprocess.run(["git", "-C", str(git_repo), "add", "config.yaml"], check=True)
        subprocess.run(
            ["git", "-C", str(git_repo), "commit", "-m", "add config"],
            check=True,
        )
        # Now modify it unstaged — working-tree diff sees the change.
        (git_repo / "config.yaml").write_text("key: v2")
        assert _is_config_only_change(git_repo, "main") is True

    def test_working_tree_mixed_config_and_code(self, git_repo: Path) -> None:
        """Unstaged mix of config-only and .py in working tree → False."""
        # Commit a .py file, then modify both unstaged.
        (git_repo / "src.py").write_text("x = 0")
        subprocess.run(["git", "-C", str(git_repo), "add", "src.py"], check=True)
        subprocess.run(
            ["git", "-C", str(git_repo), "commit", "-m", "add py"],
            check=True,
        )
        (git_repo / "config.yaml").write_text("key: v2")
        (git_repo / "src.py").write_text("x = 1")
        assert _is_config_only_change(git_repo, "main") is False

    def test_working_tree_empty_no_commits(self, git_repo: Path) -> None:
        """No working tree changes and no commits → False (fail-closed)."""
        assert _is_config_only_change(git_repo, "main") is False


# ---------------------------------------------------------------------------
# _is_rename_only_change
# ---------------------------------------------------------------------------


class TestIsRenameOnlyChange:
    """Tests for ``_is_rename_only_change`` using a real git repo fixture."""

    @pytest.fixture
    def git_repo(self, tmp_path: Path) -> Path:
        """Create a temp git repo with an origin/main ref for diffing.

        Seeds the base commit with a README and two Python source files
        so that subsequent ``git mv`` operations are detected as renames
        (not additions) when diffed against ``origin/main``.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.name", "Test"],
            check=True,
        )
        (repo / "README.md").write_text("# Test Repo")
        # Pre-seed source files so renames are detected as R, not A.
        (repo / "src").mkdir()
        (repo / "src" / "mod.py").write_text("x = 1")
        (repo / "src" / "old_a.py").write_text("print('a')")
        (repo / "src" / "old_b.py").write_text("print('b')")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "initial"], check=True)
        subprocess.run(["git", "-C", str(repo), "branch", "-M", "main"], check=True)
        remote = tmp_path / "remote.git"
        remote.mkdir()
        subprocess.run(["git", "-C", str(remote), "init", "--bare"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "remote", "add", "origin", str(remote)],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "push", "-u", "origin", "main"],
            check=True,
        )
        return repo

    def test_pure_renames_only(self, git_repo: Path) -> None:
        """Only git mv operations, no other changes → True."""
        subprocess.run(
            ["git", "-C", str(git_repo), "mv", "src/old_a.py", "src/new_a.py"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(git_repo), "mv", "src/old_b.py", "src/new_b.py"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(git_repo), "commit", "-m", "rename"],
            check=True,
        )
        assert _is_rename_only_change(git_repo, "main") is True

    def test_renames_with_config_stubs(self, git_repo: Path) -> None:
        """Renames + config-only stub files (e.g. docs/modules.yaml) → True."""
        subprocess.run(
            ["git", "-C", str(git_repo), "mv", "src/mod.py", "src/new_mod.py"],
            check=True,
        )
        # Also update a config file.
        (git_repo / "docs").mkdir(exist_ok=True)
        (git_repo / "docs" / "modules.yaml").write_text("- src/new_mod.py")
        subprocess.run(["git", "-C", str(git_repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(git_repo), "commit", "-m", "rename + config"],
            check=True,
        )
        assert _is_rename_only_change(git_repo, "main") is True

    def test_renames_with_py_code_changes(self, git_repo: Path) -> None:
        """Renames + a .py file with real content delta → False."""
        subprocess.run(
            ["git", "-C", str(git_repo), "mv", "src/mod.py", "src/new_mod.py"],
            check=True,
        )
        # Add a .py file with real content.
        (git_repo / "src" / "helper.py").write_text("def foo(): pass")
        subprocess.run(["git", "-C", str(git_repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(git_repo), "commit", "-m", "rename + new code"],
            check=True,
        )
        assert _is_rename_only_change(git_repo, "main") is False

    def test_no_renames_at_all(self, git_repo: Path) -> None:
        """No rename operations in the diff → False."""
        # Just modify, no rename.
        (git_repo / "src" / "mod.py").write_text("x = 2")
        subprocess.run(["git", "-C", str(git_repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(git_repo), "commit", "-m", "modify"],
            check=True,
        )
        assert _is_rename_only_change(git_repo, "main") is False

    def test_rename_with_zero_delta_stub(self, git_repo: Path) -> None:
        """Rename + a genuinely new empty .py stub file (zero delta) → True."""
        subprocess.run(
            ["git", "-C", str(git_repo), "mv", "src/mod.py", "src/new_mod.py"],
            check=True,
        )
        # Create a brand-new empty file (not modifying an existing one).
        (git_repo / "src" / "__init__.py").write_text("")
        subprocess.run(["git", "-C", str(git_repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(git_repo), "commit", "-m", "rename + empty stub"],
            check=True,
        )
        assert _is_rename_only_change(git_repo, "main") is True

    def test_git_failure_returns_false(self, tmp_path: Path) -> None:
        """Nonexistent origin/<branch> → git diff fails → False."""
        repo = tmp_path / "noremote"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init"], check=True)
        assert _is_rename_only_change(repo, "nonexistent") is False

    def test_rename_with_nonzero_delta_py(self, git_repo: Path) -> None:
        """Rename + a .py file with actual content (nonzero delta) → False."""
        subprocess.run(
            ["git", "-C", str(git_repo), "mv", "src/mod.py", "src/new_mod.py"],
            check=True,
        )
        # Add a stub with content (nonzero delta).
        (git_repo / "src" / "stub.py").write_text(
            "# Re-export\nfrom src.new_mod import x"
        )
        subprocess.run(["git", "-C", str(git_repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(git_repo), "commit", "-m", "rename + real stub"],
            check=True,
        )
        assert _is_rename_only_change(git_repo, "main") is False


# ---------------------------------------------------------------------------
# _should_skip_test_gate truth table
# ---------------------------------------------------------------------------


class TestShouldSkipTestGate:
    """Tests for ``_should_skip_test_gate`` via monkeypatching its
    dependencies."""

    def _settings(self):
        """Return a minimal Settings stub."""
        from robotsix_mill.config import Settings

        return Settings(data_dir="/tmp")

    def test_non_config_diff_skips_agent_and_runs_tests(self, monkeypatch) -> None:
        """When _is_config_only_change is False, skip=False and agent is NOT called."""
        monkeypatch.setattr(
            "robotsix_mill.stages.implement._shared._is_config_only_change",
            lambda repo_dir, target_branch: False,
        )
        # Monkeypatch the agent at its real location so we can detect if
        # it's called.
        agent_called = False

        def fake_agent(**kwargs):
            nonlocal agent_called
            agent_called = True
            from robotsix_mill.agents.test_scope import TestScopeVerdict

            return TestScopeVerdict(needs_full_suite=False, rationale="unused")

        monkeypatch.setattr(
            "robotsix_mill.agents.test_scope.run_test_scope_agent",
            fake_agent,
        )

        skip, diag = _should_skip_test_gate(
            Path("/fake"), "main", self._settings(), "some ticket"
        )
        assert skip is False
        assert not agent_called
        assert "non-config" in diag

    def test_config_only_agent_says_skip(self, monkeypatch) -> None:
        """config-only + agent says needs_full_suite=False → skip=True."""
        monkeypatch.setattr(
            "robotsix_mill.stages.implement._shared._is_rename_only_change",
            lambda repo_dir, target_branch: False,
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement._shared._is_config_only_change",
            lambda repo_dir, target_branch: True,
        )
        monkeypatch.setattr(
            "robotsix_mill.agents.test_scope.run_test_scope_agent",
            lambda settings, changed_files, diff_stat, ticket_summary: __import__(
                "robotsix_mill.agents.test_scope", fromlist=["TestScopeVerdict"]
            ).TestScopeVerdict(
                needs_full_suite=False,
                rationale="documentation-only change",
            ),
        )
        # Also stub the subprocess git calls used inside _should_skip_test_gate
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *args, **kwargs: type(
                "R", (), {"returncode": 0, "stdout": "docs/x.md\nconfig.yaml"}
            )(),
        )

        skip, diag = _should_skip_test_gate(
            Path("/fake"), "main", self._settings(), "doc fix"
        )
        assert skip is True
        assert "non-behavioural" in diag

    def test_config_only_agent_says_run(self, monkeypatch) -> None:
        """config-only + agent says needs_full_suite=True → skip=False."""
        monkeypatch.setattr(
            "robotsix_mill.stages.implement._shared._is_rename_only_change",
            lambda repo_dir, target_branch: False,
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement._shared._is_config_only_change",
            lambda repo_dir, target_branch: True,
        )
        monkeypatch.setattr(
            "robotsix_mill.agents.test_scope.run_test_scope_agent",
            lambda settings, changed_files, diff_stat, ticket_summary: __import__(
                "robotsix_mill.agents.test_scope", fromlist=["TestScopeVerdict"]
            ).TestScopeVerdict(
                needs_full_suite=True,
                rationale="config file is read at runtime",
            ),
        )
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *args, **kwargs: type(
                "R", (), {"returncode": 0, "stdout": "config.json"}
            )(),
        )

        skip, diag = _should_skip_test_gate(
            Path("/fake"), "main", self._settings(), "config update"
        )
        assert skip is False
        assert "behaviour-affecting" in diag


# ---------------------------------------------------------------------------
# CONFIG_ONLY_EXTENSIONS constant
# ---------------------------------------------------------------------------


def test_config_only_extensions_includes_expected() -> None:
    """The constant covers the expected config-only extensions."""
    assert ".yaml" in CONFIG_ONLY_EXTENSIONS
    assert ".yml" in CONFIG_ONLY_EXTENSIONS
    assert ".toml" in CONFIG_ONLY_EXTENSIONS
    assert ".md" in CONFIG_ONLY_EXTENSIONS
    assert ".cfg" in CONFIG_ONLY_EXTENSIONS
    assert ".ini" in CONFIG_ONLY_EXTENSIONS
    assert ".json" in CONFIG_ONLY_EXTENSIONS
    assert ".conf" in CONFIG_ONLY_EXTENSIONS
    # Verify it's a tuple (usable with str.endswith).
    assert isinstance(CONFIG_ONLY_EXTENSIONS, tuple)


# ---------------------------------------------------------------------------
# _parse_spec_code_blocks
# ---------------------------------------------------------------------------


class TestParseSpecCodeBlocks:
    """Tests for ``_parse_spec_code_blocks`` — extracting file→code mappings."""

    def test_single_block_with_backtick_path(self):
        spec = """## Changes

Add import in `src/foo/bar.py`:

```python
import new_module
```
"""
        blocks = _parse_spec_code_blocks(spec)
        assert len(blocks) == 1
        assert blocks[0][0] == "src/foo/bar.py"
        assert blocks[0][1] == "python"
        assert "import new_module" in blocks[0][2]

    def test_file_comment_annotation(self):
        spec = """# File: src/module.py
```python
x = 1
```
"""
        blocks = _parse_spec_code_blocks(spec)
        assert len(blocks) == 1
        assert blocks[0][0] == "src/module.py"

    def test_heading_with_backtick_path(self):
        spec = """### `tests/test_foo.py`

Add test:

```python
def test_x():
    pass
```
"""
        blocks = _parse_spec_code_blocks(spec)
        assert len(blocks) == 1
        assert blocks[0][0] == "tests/test_foo.py"

    def test_plain_path_in_preceding_line(self):
        spec = """src/robotsix_mill/cli/__init__.py

```python
runners.register(subparsers)
```
"""
        blocks = _parse_spec_code_blocks(spec)
        assert len(blocks) == 1
        assert blocks[0][0] == "src/robotsix_mill/cli/__init__.py"

    def test_multiple_blocks_different_files(self):
        spec = """### `src/a.py`

```python
# a
```

### `src/b.py`

```python
# b
```
"""
        blocks = _parse_spec_code_blocks(spec)
        assert len(blocks) == 2
        paths = {b[0] for b in blocks}
        assert paths == {"src/a.py", "src/b.py"}

    def test_no_code_blocks_returns_empty(self):
        spec = "Just some text, no code blocks."
        blocks = _parse_spec_code_blocks(spec)
        assert blocks == []

    def test_block_without_file_path_returns_empty(self):
        spec = """```python
print("no file reference")
```
"""
        blocks = _parse_spec_code_blocks(spec)
        assert blocks == []

    def test_non_source_extension_ignored(self):
        spec = """`data/output.csv`

```csv
a,b,c
```
"""
        blocks = _parse_spec_code_blocks(spec)
        assert blocks == []

    def test_file_path_in_info_string_ignored(self):
        """Info string (e.g. ```python filename=...) is not used for path detection."""
        spec = """```python filename=src/x.py
y = 1
```
"""
        blocks = _parse_spec_code_blocks(spec)
        assert blocks == []  # No path in preceding context


# ---------------------------------------------------------------------------
# _is_spec_exact_edits
# ---------------------------------------------------------------------------


class TestIsSpecExactEdits:
    """Tests for ``_is_spec_exact_edits`` — gate before bypass routing."""

    def test_all_files_exist(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "a.py").write_text("# a")
        (repo / "src" / "b.py").write_text("# b")

        spec = """### `src/a.py`

```python
# a new
```

### `src/b.py`

```python
# b new
```
"""
        assert _is_spec_exact_edits(spec, repo) is True

    def test_missing_file_returns_false(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "a.py").write_text("# a")
        # src/b.py does NOT exist.

        spec = """### `src/a.py`

```python
# a
```

### `src/b.py`

```python
# b
```
"""
        assert _is_spec_exact_edits(spec, repo) is False

    def test_no_blocks_returns_false(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        assert _is_spec_exact_edits("No code here.", repo) is False

    def test_empty_spec_returns_false(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        assert _is_spec_exact_edits("", repo) is False

    def test_parse_error_returns_false(self, tmp_path: Path) -> None:
        """Fail-closed: any exception during parsing returns False."""
        repo = tmp_path / "repo"
        repo.mkdir()
        # Passing a non-string should not crash.
        assert _is_spec_exact_edits(None, repo) is False  # type: ignore[arg-type]
