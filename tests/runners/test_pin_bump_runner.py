"""Tests for pin_bump_runner — monkeypatch-only, no real I/O."""

from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import MagicMock, patch

from robotsix_mill.deps.internal_graph import (
    INTERNAL_GIT_HOST,
    CyclicDependencyError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _repos_yaml_str(*repo_ids: str) -> str:
    """Minimal ``repos.yaml`` body for the given repo ids."""
    lines = ["repos:"]
    for rid in repo_ids:
        lines.append(f"  {rid}:")
        lines.append(f'    board_id: "{rid}"')
        lines.append(f'    forge_remote_url: "https://{INTERNAL_GIT_HOST}{rid}"')
    return "\n".join(lines) + "\n"


def _internal_pin(pkg: str, rev: str = "abc123") -> str:
    return '{ git = "https://' + INTERNAL_GIT_HOST + pkg + '", rev = "' + rev + '" }'


def _pyproject(sources: dict[str, str] | None = None) -> str:
    if not sources:
        return '[project]\nname = "test"\n'
    lines = ["[project]", 'name = "test"', "", "[tool.uv.sources]"]
    for pkg, entry in sources.items():
        lines.append(f"{pkg} = {entry}")
    return "\n".join(lines) + "\n"


def _make_repo_config(forge_remote_url):
    """Minimal RepoConfig-compatible mock."""
    from robotsix_mill.config.repos import RepoConfig

    return RepoConfig(
        repo_id="test-repo",
        board_id="test-repo",
        forge_remote_url=forge_remote_url,
        langfuse_project_name="",
        langfuse_public_key="",
        langfuse_secret_key="",
    )


# ---------------------------------------------------------------------------
# run_pin_bump_pass
# ---------------------------------------------------------------------------


class TestRunPinBumpPass:
    def test_no_repo_config(self):
        """repo_config=None → returns immediately."""
        from robotsix_mill.runners.pin_bump_runner import run_pin_bump_pass

        with patch("robotsix_mill.runners.pin_bump_runner.Settings") as mock_settings:
            result = run_pin_bump_pass(session_id="s1", repo_config=None)
            assert result is None
            mock_settings.assert_not_called()

    def test_no_reachable_repos_logs_and_returns(self):
        """All repos lack forge_remote_url → log + early return."""
        from robotsix_mill.runners.pin_bump_runner import run_pin_bump_pass

        registry = MagicMock()
        rc_no_url = _make_repo_config("")
        rc_no_url.forge_remote_url = None
        registry.repos = {"a": rc_no_url}

        with (
            patch(
                "robotsix_mill.runners.pin_bump_runner.Settings",
            ),
            patch(
                "robotsix_mill.runners.pin_bump_runner.get_repos_config",
                return_value=registry,
            ),
            patch("robotsix_mill.runners.pin_bump_runner.github_token") as mock_token,
            patch(
                "robotsix_mill.runners.pin_bump_runner.build_internal_dep_graph"
            ) as mock_build,
        ):
            result = run_pin_bump_pass(
                session_id="s1",
                repo_config=_make_repo_config("https://example.com/repo"),
            )

        assert result is None
        mock_token.assert_not_called()
        mock_build.assert_not_called()

    def test_token_error_skips_repo(self):
        """github_token raises → repo is skipped with warning."""
        from robotsix_mill.runners.pin_bump_runner import run_pin_bump_pass

        rc = _make_repo_config("https://example.com/repo")
        registry = MagicMock()
        registry.repos = {"a": rc}

        import robotsix_mill.runners.pin_bump_runner as runner_mod

        with (
            patch.object(runner_mod, "Settings"),
            patch.object(runner_mod, "get_repos_config", return_value=registry),
            patch.object(
                runner_mod, "github_token", side_effect=RuntimeError("no token")
            ),
            patch.object(runner_mod.git_ops, "clone") as mock_clone,
            patch.object(runner_mod, "build_internal_dep_graph") as mock_build,
        ):
            result = run_pin_bump_pass(
                session_id="s1",
                repo_config=_make_repo_config("https://example.com/repo"),
            )

        assert result is None
        mock_clone.assert_not_called()
        mock_build.assert_not_called()

    def test_detection_and_log_path(self, tmp_path, caplog):
        """With a small fake registry + pyproject_map, the runner
        computes and logs the topological order, current pins, and
        bump plan."""
        import logging

        from robotsix_mill.config.repos import load_repos_config
        from robotsix_mill.runners.pin_bump_runner import run_pin_bump_pass

        repos_yaml_path = tmp_path / "repos.yaml"
        repos_yaml_path.write_text(_repos_yaml_str("a", "b"))

        registry = load_repos_config(str(repos_yaml_path))

        def _fake_clone(remote_url, dest, branch, token=None):
            dest_path = Path(str(dest))
            dest_path.mkdir(parents=True, exist_ok=True)
            url_str = str(remote_url)
            if url_str.rstrip("/").endswith("/a"):
                content = _pyproject({"b": _internal_pin("b", "sha_b")})
            elif url_str.rstrip("/").endswith("/b"):
                content = _pyproject()
            else:
                content = _pyproject()
            (dest_path / "pyproject.toml").write_text(content, encoding="utf-8")

        import robotsix_mill.runners.pin_bump_runner as runner_mod

        with (
            patch.object(runner_mod, "Settings"),
            patch.object(runner_mod, "get_repos_config", return_value=registry),
            patch.object(runner_mod, "github_token", return_value="fake-token"),
            patch.object(runner_mod, "target_branch_for", return_value="main"),
            patch.object(runner_mod.git_ops, "clone", side_effect=_fake_clone),
            # Stub latest-SHA resolution: "b" has advanced to "new-sha".
            patch.object(
                runner_mod,
                "resolve_latest_shas",
                return_value={"b": "new-sha"},
            ),
            caplog.at_level(
                logging.INFO, logger="robotsix_mill.runners.pin_bump_runner"
            ),
        ):
            run_pin_bump_pass(
                session_id="s1",
                repo_config=registry.repos["a"],
            )

        log_text = caplog.text
        assert "topological order:" in log_text, (
            f"Expected topo order log, got: {log_text}"
        )
        assert "sha_b" in log_text, f"Expected pin SHA 'sha_b' in logs, got: {log_text}"
        # The plan should show a bump from sha_b to new-sha.
        assert "sha_b -> new-sha" in log_text, (
            f"Expected bump plan 'sha_b -> new-sha' in logs, got: {log_text}"
        )

    def test_all_pins_current_log(self, tmp_path, caplog):
        """When every pin matches latest SHAs the runner logs
        'all pins current'."""
        import logging

        from robotsix_mill.config.repos import load_repos_config
        from robotsix_mill.runners.pin_bump_runner import run_pin_bump_pass

        repos_yaml_path = tmp_path / "repos.yaml"
        repos_yaml_path.write_text(_repos_yaml_str("a", "b"))

        registry = load_repos_config(str(repos_yaml_path))

        def _fake_clone(remote_url, dest, branch, token=None):
            dest_path = Path(str(dest))
            dest_path.mkdir(parents=True, exist_ok=True)
            url_str = str(remote_url)
            if url_str.rstrip("/").endswith("/a"):
                content = _pyproject({"b": _internal_pin("b", "sha_b")})
            elif url_str.rstrip("/").endswith("/b"):
                content = _pyproject()
            else:
                content = _pyproject()
            (dest_path / "pyproject.toml").write_text(content, encoding="utf-8")

        import robotsix_mill.runners.pin_bump_runner as runner_mod

        with (
            patch.object(runner_mod, "Settings"),
            patch.object(runner_mod, "get_repos_config", return_value=registry),
            patch.object(runner_mod, "github_token", return_value="fake-token"),
            patch.object(runner_mod, "target_branch_for", return_value="main"),
            patch.object(runner_mod.git_ops, "clone", side_effect=_fake_clone),
            # Latest SHA matches current pin → all pins current.
            patch.object(
                runner_mod,
                "resolve_latest_shas",
                return_value={"b": "sha_b"},
            ),
            caplog.at_level(
                logging.INFO, logger="robotsix_mill.runners.pin_bump_runner"
            ),
        ):
            run_pin_bump_pass(
                session_id="s1",
                repo_config=registry.repos["a"],
            )

        log_text = caplog.text
        assert "all pins current" in log_text, (
            f"Expected 'all pins current' in logs, got: {log_text}"
        )

    def test_pr_actuator_is_noop_when_no_pins(self, tmp_path, caplog):
        """When repos have no internal pins, the runner still computes
        a bump plan — it is a no-op (no forge calls, no PRs created)."""
        import logging

        from robotsix_mill.config.repos import load_repos_config
        from robotsix_mill.runners.pin_bump_runner import run_pin_bump_pass

        repos_yaml_path = tmp_path / "repos.yaml"
        repos_yaml_path.write_text(_repos_yaml_str("a", "b"))

        registry = load_repos_config(str(repos_yaml_path))

        def _fake_clone(remote_url, dest, branch, token=None):
            dest_path = Path(str(dest))
            dest_path.mkdir(parents=True, exist_ok=True)
            content = _pyproject()
            (dest_path / "pyproject.toml").write_text(content, encoding="utf-8")

        import robotsix_mill.runners.pin_bump_runner as runner_mod

        with (
            patch.object(runner_mod, "Settings"),
            patch.object(runner_mod, "get_repos_config", return_value=registry),
            patch.object(runner_mod, "github_token", return_value="fake-token"),
            patch.object(runner_mod, "target_branch_for", return_value="main"),
            patch.object(runner_mod.git_ops, "clone", side_effect=_fake_clone),
            caplog.at_level(
                logging.INFO, logger="robotsix_mill.runners.pin_bump_runner"
            ),
        ):
            run_pin_bump_pass(
                session_id="s1",
                repo_config=registry.repos["a"],
            )

        # No pins → never resolves latest SHAs, no forge calls.
        assert "topological order" in caplog.text

    def test_cyclic_dependency_warning(self, tmp_path, caplog):
        """CyclicDependencyError → logged as warning, not raised."""
        import logging

        from robotsix_mill.config.repos import load_repos_config
        from robotsix_mill.runners.pin_bump_runner import run_pin_bump_pass

        repos_yaml_path = tmp_path / "repos.yaml"
        repos_yaml_path.write_text(_repos_yaml_str("a", "b"))

        registry = load_repos_config(str(repos_yaml_path))

        def _fake_clone(remote_url, dest, branch, token=None):
            dest_path = Path(str(dest))
            dest_path.mkdir(parents=True, exist_ok=True)
            content = _pyproject()
            (dest_path / "pyproject.toml").write_text(content, encoding="utf-8")

        import robotsix_mill.runners.pin_bump_runner as runner_mod

        with (
            patch.object(runner_mod, "Settings"),
            patch.object(runner_mod, "get_repos_config", return_value=registry),
            patch.object(runner_mod, "github_token", return_value="fake-token"),
            patch.object(runner_mod, "target_branch_for", return_value="main"),
            patch.object(runner_mod.git_ops, "clone", side_effect=_fake_clone),
            patch.object(
                runner_mod,
                "build_internal_dep_graph",
                side_effect=CyclicDependencyError("cycle detected"),
            ),
            caplog.at_level(
                logging.WARNING, logger="robotsix_mill.runners.pin_bump_runner"
            ),
        ):
            # Must not raise.
            run_pin_bump_pass(
                session_id="s1",
                repo_config=registry.repos["a"],
            )

        log_text = caplog.text
        assert "cyclic" in log_text.lower(), f"Expected cyclic warning, got: {log_text}"

    def test_clone_failure_skips_repo(self, tmp_path, caplog):
        """clone raises → repo skipped with warning, other repos still
        processed."""
        import logging

        from robotsix_mill.config.repos import load_repos_config
        from robotsix_mill.runners.pin_bump_runner import run_pin_bump_pass

        repos_yaml_path = tmp_path / "repos.yaml"
        repos_yaml_path.write_text(_repos_yaml_str("a", "b", "c"))

        registry = load_repos_config(str(repos_yaml_path))

        call_count = {"count": 0}

        def _fake_clone(remote_url, dest, branch, token=None):
            call_count["count"] += 1
            # Fail for repo "b" only (second call).
            if call_count["count"] == 2:
                raise OSError("network unreachable")
            dest_path = Path(str(dest))
            dest_path.mkdir(parents=True, exist_ok=True)
            content = _pyproject()
            (dest_path / "pyproject.toml").write_text(content, encoding="utf-8")

        import robotsix_mill.runners.pin_bump_runner as runner_mod

        with (
            patch.object(runner_mod, "Settings"),
            patch.object(runner_mod, "get_repos_config", return_value=registry),
            patch.object(runner_mod, "github_token", return_value="fake-token"),
            patch.object(runner_mod, "target_branch_for", return_value="main"),
            patch.object(runner_mod.git_ops, "clone", side_effect=_fake_clone),
            caplog.at_level(
                logging.INFO, logger="robotsix_mill.runners.pin_bump_runner"
            ),
        ):
            run_pin_bump_pass(
                session_id="s1",
                repo_config=registry.repos["a"],
            )

        log_text = caplog.text
        assert "topological order" in log_text, (
            f"Expected topo order despite clone failure on one repo, got: {log_text}"
        )


class TestDispatchWiring:
    def test_schedule_only_runners_contains_pin_bump(self):
        """_SCHEDULE_ONLY_RUNNERS has a 'pin_bump' key."""
        from robotsix_mill.runtime.worker.poll_loops import PollLoopsMixin

        runners = PollLoopsMixin._SCHEDULE_ONLY_RUNNERS
        assert "pin_bump" in runners

    def test_pin_bump_dotted_path_imports_callable(self):
        """The dotted path for 'pin_bump' resolves to
        run_pin_bump_pass (a callable)."""
        from robotsix_mill.runtime.worker.poll_loops import PollLoopsMixin

        path = PollLoopsMixin._SCHEDULE_ONLY_RUNNERS["pin_bump"]
        mod_path, attr = path.rsplit(":", 1)
        mod = importlib.import_module(mod_path)
        fn = getattr(mod, attr)
        assert callable(fn), f"{path} did not resolve to a callable"

    def test_build_periodic_workflow_runner_returns_callable(self):
        """_build_periodic_workflow_runner no longer returns None for
        a 'pin_bump' workflow."""
        from robotsix_mill.agents.periodic_loader import ResolvedPeriodicWorkflow
        from robotsix_mill.runtime.worker.poll_loops import PollLoopsMixin

        mixin = PollLoopsMixin()
        wf = ResolvedPeriodicWorkflow(
            name="pin_bump",
            kind="schedule_only",
            definition=None,
            interval_seconds=None,
            enabled=True,
        )
        runner = mixin._build_periodic_workflow_runner(wf)
        assert runner is not None, (
            "_build_periodic_workflow_runner returned None for pin_bump"
        )
        assert callable(runner)
