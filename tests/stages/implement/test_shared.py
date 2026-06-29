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
