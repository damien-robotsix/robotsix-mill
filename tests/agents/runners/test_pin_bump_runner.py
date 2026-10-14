"""Tests for pin_bump_runner — monkeypatch-only, no real I/O."""

from __future__ import annotations

import contextlib
import importlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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


@pytest.fixture(autouse=True)
def _fresh_pass_guard():
    """Each test starts as if no cross-repo pass had run in this process."""
    import robotsix_mill.agents.runners.pin_bump_runner as runner_mod

    runner_mod._last_pass_started_monotonic = None
    runner_mod._last_latest_map = None
    yield
    runner_mod._last_pass_started_monotonic = None
    runner_mod._last_latest_map = None


class TestPassGuard:
    """One fleet-wide pass per interval, however many per-repo loops fire it."""

    def _settings(self):
        return MagicMock(pin_bump_interval_seconds=86400)

    def test_second_firing_within_interval_is_skipped(self, caplog):
        """Two per-repo firings in one interval → the registry is read once."""
        import logging

        import robotsix_mill.agents.runners.pin_bump_runner as runner_mod

        registry = MagicMock()
        registry.repos = {}
        with (
            patch.object(runner_mod, "Settings", return_value=self._settings()),
            patch.object(
                runner_mod, "get_repos_config", return_value=registry
            ) as mock_registry,
            caplog.at_level(logging.INFO),
        ):
            runner_mod.run_pin_bump_pass(
                session_id="s1", repo_config=_make_repo_config("https://x/a")
            )
            runner_mod.run_pin_bump_pass(
                session_id="s2", repo_config=_make_repo_config("https://x/b")
            )

        assert mock_registry.call_count == 1
        assert "cross-repo pass already ran" in caplog.text
        assert "skipping the firing for repo test-repo" in caplog.text

    def test_firing_after_interval_runs_again(self):
        """Once the guard window has elapsed the next firing does the work."""
        import robotsix_mill.agents.runners.pin_bump_runner as runner_mod

        registry = MagicMock()
        registry.repos = {}
        clock = iter([1000.0, 1000.0 + 86400 * 0.95])
        with (
            patch.object(runner_mod, "Settings", return_value=self._settings()),
            patch.object(
                runner_mod, "get_repos_config", return_value=registry
            ) as mock_registry,
            patch.object(runner_mod.time, "monotonic", side_effect=lambda: next(clock)),
        ):
            runner_mod.run_pin_bump_pass(
                session_id="s1", repo_config=_make_repo_config("https://x/a")
            )
            runner_mod.run_pin_bump_pass(
                session_id="s2", repo_config=_make_repo_config("https://x/b")
            )

        assert mock_registry.call_count == 2

    def test_guard_window_is_a_fraction_of_the_interval(self):
        import robotsix_mill.agents.runners.pin_bump_runner as runner_mod

        runner_mod._last_pass_started_monotonic = 0.0
        assert runner_mod._pass_recently_ran(100.0, 50.0)
        assert runner_mod._pass_recently_ran(100.0, 89.0)
        assert not runner_mod._pass_recently_ran(100.0, 90.0)
        runner_mod._last_pass_started_monotonic = None
        assert not runner_mod._pass_recently_ran(100.0, 1.0)

    def test_disabled_interval_does_not_arm_the_guard(self):
        """interval<=0 returns before the guard so re-enabling runs at once."""
        import robotsix_mill.agents.runners.pin_bump_runner as runner_mod

        with patch.object(
            runner_mod, "Settings", return_value=MagicMock(pin_bump_interval_seconds=0)
        ):
            runner_mod.run_pin_bump_pass(
                session_id="s1", repo_config=_make_repo_config("https://x/a")
            )
        assert runner_mod._last_pass_started_monotonic is None


class TestRunPinBumpPass:
    def test_no_repo_config(self):
        """repo_config=None → returns immediately."""
        from robotsix_mill.agents.runners.pin_bump_runner import run_pin_bump_pass

        with patch(
            "robotsix_mill.agents.runners.pin_bump_runner.Settings"
        ) as mock_settings:
            result = run_pin_bump_pass(session_id="s1", repo_config=None)
            assert result is None
            mock_settings.assert_not_called()

    def test_no_reachable_repos_logs_and_returns(self):
        """All repos lack forge_remote_url → log + early return."""
        from robotsix_mill.agents.runners.pin_bump_runner import run_pin_bump_pass

        registry = MagicMock()
        rc_no_url = _make_repo_config("")
        rc_no_url.forge_remote_url = None
        registry.repos = {"a": rc_no_url}

        with (
            patch(
                "robotsix_mill.agents.runners.pin_bump_runner.Settings",
                return_value=MagicMock(pin_bump_interval_seconds=86400),
            ),
            patch(
                "robotsix_mill.agents.runners.pin_bump_runner.get_repos_config",
                return_value=registry,
            ),
            patch(
                "robotsix_mill.agents.runners.pin_bump_runner.github_token"
            ) as mock_token,
            patch(
                "robotsix_mill.agents.runners.pin_bump_runner.build_internal_dep_graph"
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
        from robotsix_mill.agents.runners.pin_bump_runner import run_pin_bump_pass

        rc = _make_repo_config("https://example.com/repo")
        registry = MagicMock()
        registry.repos = {"a": rc}

        import robotsix_mill.agents.runners.pin_bump_runner as runner_mod

        with (
            patch.object(
                runner_mod,
                "Settings",
                return_value=MagicMock(pin_bump_interval_seconds=86400),
            ),
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
        computes and logs the topological order and current pins."""
        import logging

        from robotsix_mill.agents.runners.pin_bump_runner import run_pin_bump_pass
        from robotsix_mill.config.repos import load_repos_config

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

        import robotsix_mill.agents.runners.pin_bump_runner as runner_mod

        with (
            patch.object(
                runner_mod,
                "Settings",
                return_value=MagicMock(pin_bump_interval_seconds=86400),
            ),
            patch.object(runner_mod, "get_repos_config", return_value=registry),
            patch.object(runner_mod, "github_token", return_value="fake-token"),
            patch.object(runner_mod, "github_push_token", return_value="fake-token"),
            patch.object(runner_mod, "target_branch_for", return_value="main"),
            patch.object(runner_mod, "get_forge"),
            patch.object(runner_mod.git_ops, "clone", side_effect=_fake_clone),
            patch.object(runner_mod.git_ops, "ls_remote_sha", return_value="sha_b"),
            caplog.at_level(
                logging.INFO, logger="robotsix_mill.agents.runners.pin_bump_runner"
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
        # Actuator was called and checked the pin; it is already at latest
        # (sha_b == sha_b), so the pin was skipped (no PR created).

    def test_pr_actuator_is_noop_when_no_pins(self, tmp_path, caplog):
        """When repos have no internal pins, the actuator runs but
        is a no-op (no forge calls, no PRs created)."""
        import logging

        from robotsix_mill.agents.runners.pin_bump_runner import run_pin_bump_pass
        from robotsix_mill.config.repos import load_repos_config

        repos_yaml_path = tmp_path / "repos.yaml"
        repos_yaml_path.write_text(_repos_yaml_str("a", "b"))

        registry = load_repos_config(str(repos_yaml_path))

        def _fake_clone(remote_url, dest, branch, token=None):
            dest_path = Path(str(dest))
            dest_path.mkdir(parents=True, exist_ok=True)
            content = _pyproject()
            (dest_path / "pyproject.toml").write_text(content, encoding="utf-8")

        import robotsix_mill.agents.runners.pin_bump_runner as runner_mod

        with (
            patch.object(
                runner_mod,
                "Settings",
                return_value=MagicMock(pin_bump_interval_seconds=86400),
            ),
            patch.object(runner_mod, "get_repos_config", return_value=registry),
            patch.object(runner_mod, "github_token", return_value="fake-token"),
            patch.object(runner_mod, "github_push_token", return_value="fake-token"),
            patch.object(runner_mod, "target_branch_for", return_value="main"),
            patch.object(runner_mod, "get_forge") as mock_get_forge,
            patch.object(runner_mod.git_ops, "clone", side_effect=_fake_clone),
            patch.object(runner_mod.git_ops, "ls_remote_sha") as mock_ls_remote,
            caplog.at_level(
                logging.INFO, logger="robotsix_mill.agents.runners.pin_bump_runner"
            ),
        ):
            run_pin_bump_pass(
                session_id="s1",
                repo_config=registry.repos["a"],
            )

        # No pins → actuator never calls forge, never resolves SHAs.
        mock_get_forge.assert_not_called()
        mock_ls_remote.assert_not_called()

    def test_cyclic_dependency_warning(self, tmp_path, caplog):
        """CyclicDependencyError → logged as warning, not raised."""
        import logging

        from robotsix_mill.agents.runners.pin_bump_runner import run_pin_bump_pass
        from robotsix_mill.config.repos import load_repos_config

        repos_yaml_path = tmp_path / "repos.yaml"
        repos_yaml_path.write_text(_repos_yaml_str("a", "b"))

        registry = load_repos_config(str(repos_yaml_path))

        def _fake_clone(remote_url, dest, branch, token=None):
            dest_path = Path(str(dest))
            dest_path.mkdir(parents=True, exist_ok=True)
            content = _pyproject()
            (dest_path / "pyproject.toml").write_text(content, encoding="utf-8")

        import robotsix_mill.agents.runners.pin_bump_runner as runner_mod

        with (
            patch.object(
                runner_mod,
                "Settings",
                return_value=MagicMock(pin_bump_interval_seconds=86400),
            ),
            patch.object(runner_mod, "get_repos_config", return_value=registry),
            patch.object(runner_mod, "github_token", return_value="fake-token"),
            patch.object(runner_mod, "github_push_token", return_value="fake-token"),
            patch.object(runner_mod, "target_branch_for", return_value="main"),
            patch.object(runner_mod.git_ops, "clone", side_effect=_fake_clone),
            patch.object(
                runner_mod,
                "build_internal_dep_graph",
                side_effect=CyclicDependencyError("cycle detected"),
            ),
            caplog.at_level(
                logging.WARNING, logger="robotsix_mill.agents.runners.pin_bump_runner"
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

        from robotsix_mill.agents.runners.pin_bump_runner import run_pin_bump_pass
        from robotsix_mill.config.repos import load_repos_config

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

        import robotsix_mill.agents.runners.pin_bump_runner as runner_mod

        with (
            patch.object(
                runner_mod,
                "Settings",
                return_value=MagicMock(pin_bump_interval_seconds=86400),
            ),
            patch.object(runner_mod, "get_repos_config", return_value=registry),
            patch.object(runner_mod, "github_token", return_value="fake-token"),
            patch.object(runner_mod, "github_push_token", return_value="fake-token"),
            patch.object(runner_mod, "target_branch_for", return_value="main"),
            patch.object(runner_mod.git_ops, "clone", side_effect=_fake_clone),
            caplog.at_level(
                logging.INFO, logger="robotsix_mill.agents.runners.pin_bump_runner"
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

    def test_disabled_by_config_returns_early(self, tmp_path, caplog):
        """When settings.pin_bump_interval_seconds is 0, the pass
        returns immediately without doing any work."""
        import logging

        import robotsix_mill.agents.runners.pin_bump_runner as runner_mod
        from robotsix_mill.agents.runners.pin_bump_runner import run_pin_bump_pass

        mock_settings = MagicMock()
        mock_settings.pin_bump_interval_seconds = 0

        with (
            patch.object(runner_mod, "Settings", return_value=mock_settings),
            patch.object(runner_mod, "get_repos_config") as mock_registry,
            patch.object(runner_mod, "github_token") as mock_token,
            caplog.at_level(
                logging.INFO, logger="robotsix_mill.agents.runners.pin_bump_runner"
            ),
        ):
            run_pin_bump_pass(
                session_id="s1",
                repo_config=_make_repo_config("https://example.com/repo"),
            )

        mock_registry.assert_not_called()
        mock_token.assert_not_called()
        assert "disabled" in caplog.text


# ---------------------------------------------------------------------------
# PR actuator
# ---------------------------------------------------------------------------


class TestActuator:
    def test_update_pin_rev_basic(self):
        """_update_pin_rev replaces the rev value in a standard sources entry."""
        from robotsix_mill.agents.runners.pin_bump_runner import _update_pin_rev

        content = _pyproject({"b": _internal_pin("b", "old_sha")})
        result = _update_pin_rev(content, "b", "new_sha")
        assert 'rev = "old_sha"' not in result
        assert 'rev = "new_sha"' in result

    def test_update_pin_rev_multiple_deps(self):
        """_update_pin_rev updates only the targeted dep, not others."""
        from robotsix_mill.agents.runners.pin_bump_runner import _update_pin_rev

        content = _pyproject(
            {
                "a": _internal_pin("a", "sha_a"),
                "b": _internal_pin("b", "sha_b"),
            }
        )
        result = _update_pin_rev(content, "b", "new_b")
        assert 'rev = "sha_a"' in result  # untouched
        assert 'rev = "new_b"' in result
        assert 'rev = "sha_b"' not in result

    @pytest.mark.parametrize(
        "new_rev",
        [
            # Crash path: "\\1" + "9a…" parsed as backreference 19.
            "9a3f2293faf7b2ea6f6cde02f24ae813a0875e07",
            "1a1f2293faf7b2ea6f6cde02f24ae813a0875e07",
            # Silent-corruption path: "\\1" + "21…" parsed as octal "\\121".
            "211f2293faf7b2ea6f6cde02f24ae813a0875e07",
            "02005ecc9e86aa9c6645252eea72a24083e8b445",
            # Control: a SHA starting with a hex letter always worked.
            "e5509fd1e675516f4fd00a603d7921cdfa2b779b",
        ],
    )
    def test_update_pin_rev_numeric_leading_sha(self, new_rev):
        """A SHA whose leading characters are digits is substituted verbatim.

        Regression: the replacement used to be the template ``rf"\\1{new_rev}\\2"``,
        so ``re`` re-read the SHA's own digits as part of the group reference —
        raising "invalid group reference N" for 96 of the 256 two-hex-char
        prefixes and silently emitting an octal escape (dropping the group-1
        prefix) for another 64. Only the 6 hex-letter leads worked, which is
        why every pre-existing test here passed.
        """
        from robotsix_mill.agents.runners.pin_bump_runner import _update_pin_rev

        content = _pyproject({"b": _internal_pin("b", "old_sha")})
        result = _update_pin_rev(content, "b", new_rev)
        assert f'rev = "{new_rev}"' in result
        assert 'rev = "old_sha"' not in result
        # The group-1 prefix must survive intact — the octal-escape path used
        # to swallow it along with two characters of the SHA.
        assert content.replace('rev = "old_sha"', f'rev = "{new_rev}"') == result

    def test_update_pin_rev_missing_raises(self):
        """_update_pin_rev raises ValueError when the dep is not found."""
        from robotsix_mill.agents.runners.pin_bump_runner import _update_pin_rev

        content = _pyproject()
        with pytest.raises(ValueError, match="Could not find rev"):
            _update_pin_rev(content, "nonexistent", "sha")

    def test_actuator_standalone_noop(self, tmp_path, caplog):
        """When called standalone with no pins, the actuator is a no-op."""
        import logging

        from robotsix_mill.agents.runners.pin_bump_runner import (
            run_pin_bump_pr_actuator,
        )
        from robotsix_mill.config.repos import load_repos_config

        repos_yaml_path = tmp_path / "repos.yaml"
        repos_yaml_path.write_text(_repos_yaml_str("x"))
        registry = load_repos_config(str(repos_yaml_path))

        def _fake_clone(remote_url, dest, branch, token=None):
            dest_path = Path(str(dest))
            dest_path.mkdir(parents=True, exist_ok=True)
            (dest_path / "pyproject.toml").write_text(_pyproject(), encoding="utf-8")

        import robotsix_mill.agents.runners.pin_bump_runner as runner_mod

        with (
            patch.object(
                runner_mod,
                "Settings",
                return_value=MagicMock(pin_bump_interval_seconds=86400),
            ),
            patch.object(runner_mod, "get_repos_config", return_value=registry),
            patch.object(runner_mod, "github_token", return_value="fake-token"),
            patch.object(runner_mod, "github_push_token", return_value="fake-token"),
            patch.object(runner_mod, "target_branch_for", return_value="main"),
            patch.object(runner_mod, "get_forge") as mock_get_forge,
            patch.object(runner_mod.git_ops, "clone", side_effect=_fake_clone),
            patch.object(runner_mod.git_ops, "ls_remote_sha") as mock_ls,
            caplog.at_level(
                logging.INFO, logger="robotsix_mill.agents.runners.pin_bump_runner"
            ),
        ):
            run_pin_bump_pr_actuator(
                session_id="s1",
                repo_config=registry.repos["x"],
            )

        mock_get_forge.assert_not_called()
        mock_ls.assert_not_called()
        assert "no internal pins" in caplog.text

    def test_actuator_stale_pin_creates_pr(self, tmp_path, caplog):
        """A stale pin triggers clone, edit, uv lock, push, and PR."""
        import logging

        from robotsix_mill.agents.runners.pin_bump_runner import (
            run_pin_bump_pr_actuator,
        )
        from robotsix_mill.config.repos import load_repos_config
        from robotsix_mill.deps.internal_graph import (
            GitPin,
            InternalDepGraph,
        )

        repos_yaml_path = tmp_path / "repos.yaml"
        repos_yaml_path.write_text(_repos_yaml_str("a", "b"))
        registry = load_repos_config(str(repos_yaml_path))

        graph = InternalDepGraph(
            pins={
                "a": {
                    "b": GitPin(
                        git_url=f"https://{INTERNAL_GIT_HOST}b",
                        rev="old_sha",
                    )
                },
                "b": {},
            },
            topo_order=["b", "a"],
        )

        mock_forge = MagicMock()
        mock_forge.pr_status.return_value = None
        mock_forge.list_open_pr_branches.return_value = set()
        mock_forge.open_merge_request.return_value = (
            "https://github.com/damien-robotsix/a/pull/1"
        )

        import robotsix_mill.agents.runners.pin_bump_runner as runner_mod

        with (
            patch.object(
                runner_mod,
                "Settings",
                return_value=MagicMock(pin_bump_interval_seconds=86400),
            ),
            patch.object(runner_mod, "get_repos_config", return_value=registry),
            patch.object(runner_mod, "github_token", return_value="fake-token"),
            patch.object(runner_mod, "github_push_token", return_value="fake-token"),
            patch.object(runner_mod, "target_branch_for", return_value="main"),
            patch.object(runner_mod, "get_forge", return_value=mock_forge),
            patch.object(runner_mod.git_ops, "ls_remote_sha", return_value="new_sha"),
            patch.object(runner_mod.git_ops, "clone") as mock_clone,
            patch.object(runner_mod.git_ops, "create_branch"),
            patch.object(runner_mod.git_ops, "commit_all"),
            patch.object(runner_mod.git_ops, "push"),
            patch.object(runner_mod, "run_coherence_check", return_value=[]),
            caplog.at_level(
                logging.INFO, logger="robotsix_mill.agents.runners.pin_bump_runner"
            ),
        ):
            # Simulate the clone creating a real pyproject.toml with the
            # old pin so _update_pin_rev has something to edit.
            def _fake_clone(remote_url, dest, branch, token=None):
                dest_path = Path(str(dest))
                dest_path.mkdir(parents=True, exist_ok=True)
                (dest_path / "pyproject.toml").write_text(
                    _pyproject({"b": _internal_pin("b", "old_sha")}),
                    encoding="utf-8",
                )

            mock_clone.side_effect = _fake_clone

            run_pin_bump_pr_actuator(
                session_id="s1",
                repo_config=registry.repos["a"],
                graph=graph,
            )

        # Verify the flow was executed.
        mock_clone.assert_called_once()
        mock_forge.open_merge_request.assert_called_once()
        call_kwargs = mock_forge.open_merge_request.call_args.kwargs
        assert "new_sha" in call_kwargs["title"]
        assert "new_sha" in call_kwargs["body"]
        assert "b" in call_kwargs["title"]
        assert "PRs created" in caplog.text

    def test_actuator_idempotent_skips(self, tmp_path, caplog):
        """A pin already at latest SHA is skipped — no clone, no PR."""
        import logging

        from robotsix_mill.agents.runners.pin_bump_runner import (
            run_pin_bump_pr_actuator,
        )
        from robotsix_mill.config.repos import load_repos_config
        from robotsix_mill.deps.internal_graph import (
            GitPin,
            InternalDepGraph,
        )

        repos_yaml_path = tmp_path / "repos.yaml"
        repos_yaml_path.write_text(_repos_yaml_str("a", "b"))
        registry = load_repos_config(str(repos_yaml_path))

        graph = InternalDepGraph(
            pins={
                "a": {
                    "b": GitPin(
                        git_url=f"https://{INTERNAL_GIT_HOST}b",
                        rev="same_sha",
                    )
                },
                "b": {},
            },
            topo_order=["b", "a"],
        )

        import robotsix_mill.agents.runners.pin_bump_runner as runner_mod

        with (
            patch.object(
                runner_mod,
                "Settings",
                return_value=MagicMock(pin_bump_interval_seconds=86400),
            ),
            patch.object(runner_mod, "get_repos_config", return_value=registry),
            patch.object(runner_mod, "github_token", return_value="fake-token"),
            patch.object(runner_mod, "github_push_token", return_value="fake-token"),
            patch.object(runner_mod, "target_branch_for", return_value="main"),
            patch.object(runner_mod, "get_forge") as mock_get_forge,
            patch.object(runner_mod.git_ops, "ls_remote_sha", return_value="same_sha"),
            patch.object(runner_mod.git_ops, "clone") as mock_clone,
            caplog.at_level(
                logging.INFO, logger="robotsix_mill.agents.runners.pin_bump_runner"
            ),
        ):
            run_pin_bump_pr_actuator(
                session_id="s1",
                repo_config=registry.repos["a"],
                graph=graph,
            )

        mock_clone.assert_not_called()
        # get_forge is called for repos that have pins (even if all
        # pins are already at latest — the forge is resolved upfront).
        mock_get_forge.assert_called_once()
        assert "already at latest" in caplog.text

    def test_duplicate_pr_skipped(self, tmp_path, caplog):
        """When an open PR already exists for the bump branch, skip."""
        import logging

        from robotsix_mill.agents.runners.pin_bump_runner import (
            run_pin_bump_pr_actuator,
        )
        from robotsix_mill.config.repos import load_repos_config
        from robotsix_mill.deps.internal_graph import (
            GitPin,
            InternalDepGraph,
        )

        repos_yaml_path = tmp_path / "repos.yaml"
        repos_yaml_path.write_text(_repos_yaml_str("a", "b"))
        registry = load_repos_config(str(repos_yaml_path))

        graph = InternalDepGraph(
            pins={
                "a": {
                    "b": GitPin(
                        git_url=f"https://{INTERNAL_GIT_HOST}b",
                        rev="old_sha",
                    )
                },
                "b": {},
            },
            topo_order=["b", "a"],
        )

        mock_forge = MagicMock()
        # Report an already-open PR for the target branch.
        mock_forge.pr_status.return_value = {
            "state": "open",
            "url": "https://github.com/damien-robotsix/a/pull/42",
        }

        import robotsix_mill.agents.runners.pin_bump_runner as runner_mod

        with (
            patch.object(
                runner_mod,
                "Settings",
                return_value=MagicMock(pin_bump_interval_seconds=86400),
            ),
            patch.object(runner_mod, "get_repos_config", return_value=registry),
            patch.object(runner_mod, "github_token", return_value="fake-token"),
            patch.object(runner_mod, "github_push_token", return_value="fake-token"),
            patch.object(runner_mod, "target_branch_for", return_value="main"),
            patch.object(runner_mod, "get_forge", return_value=mock_forge),
            patch.object(runner_mod.git_ops, "ls_remote_sha", return_value="new_sha"),
            patch.object(runner_mod.git_ops, "clone") as mock_clone,
            caplog.at_level(
                logging.INFO, logger="robotsix_mill.agents.runners.pin_bump_runner"
            ),
        ):
            run_pin_bump_pr_actuator(
                session_id="s1",
                repo_config=registry.repos["a"],
                graph=graph,
            )

        # Must not clone or open a PR — the duplicate guard fires first.
        mock_clone.assert_not_called()
        mock_forge.open_merge_request.assert_not_called()
        assert "already open" in caplog.text

    def test_pr_lookup_5xx_skipped(self, tmp_path, caplog):
        """A persistent 5xx on the PR lookup skips the pin (no crash)."""
        import logging

        import httpx

        from robotsix_mill.agents.runners.pin_bump_runner import (
            run_pin_bump_pr_actuator,
        )
        from robotsix_mill.config.repos import load_repos_config
        from robotsix_mill.deps.internal_graph import (
            GitPin,
            InternalDepGraph,
        )

        repos_yaml_path = tmp_path / "repos.yaml"
        repos_yaml_path.write_text(_repos_yaml_str("a", "b"))
        registry = load_repos_config(str(repos_yaml_path))

        graph = InternalDepGraph(
            pins={
                "a": {
                    "b": GitPin(
                        git_url=f"https://{INTERNAL_GIT_HOST}b",
                        rev="old_sha",
                    )
                },
                "b": {},
            },
            topo_order=["b", "a"],
        )

        mock_forge = MagicMock()
        # The PR lookup 500s persistently (e.g. a PR stuck in a bad state).
        request = httpx.Request("GET", "https://api.github.com/repos/x/y/pulls/94")
        response = httpx.Response(500, request=request)
        mock_forge.pr_status.side_effect = httpx.HTTPStatusError(
            "Server error '500 Internal Server Error'",
            request=request,
            response=response,
        )

        import robotsix_mill.agents.runners.pin_bump_runner as runner_mod

        with (
            patch.object(
                runner_mod,
                "Settings",
                return_value=MagicMock(pin_bump_interval_seconds=86400),
            ),
            patch.object(runner_mod, "get_repos_config", return_value=registry),
            patch.object(runner_mod, "github_token", return_value="fake-token"),
            patch.object(runner_mod, "github_push_token", return_value="fake-token"),
            patch.object(runner_mod, "target_branch_for", return_value="main"),
            patch.object(runner_mod, "get_forge", return_value=mock_forge),
            patch.object(runner_mod.git_ops, "ls_remote_sha", return_value="new_sha"),
            patch.object(runner_mod.git_ops, "clone") as mock_clone,
            caplog.at_level(
                logging.INFO, logger="robotsix_mill.agents.runners.pin_bump_runner"
            ),
        ):
            # Must not raise — the pass swallows the 5xx and skips the pin.
            run_pin_bump_pr_actuator(
                session_id="s1",
                repo_config=registry.repos["a"],
                graph=graph,
            )

        mock_clone.assert_not_called()
        mock_forge.open_merge_request.assert_not_called()
        assert "PR lookup failed" in caplog.text

    def test_inflight_cap_skipped(self, tmp_path, caplog):
        """When in-flight pin-bump PRs reach max_inflight_prs, skip."""
        import logging

        from robotsix_mill.agents.runners.pin_bump_runner import (
            run_pin_bump_pr_actuator,
        )
        from robotsix_mill.config.repos import load_repos_config
        from robotsix_mill.deps.internal_graph import (
            GitPin,
            InternalDepGraph,
        )

        repos_yaml_path = tmp_path / "repos.yaml"
        repos_yaml_path.write_text(_repos_yaml_str("a", "b"))
        registry = load_repos_config(str(repos_yaml_path))
        # Set max_inflight_prs=1 so the second open PR is blocked.
        registry.repos["a"].max_inflight_prs = 1

        graph = InternalDepGraph(
            pins={
                "a": {
                    "b": GitPin(
                        git_url=f"https://{INTERNAL_GIT_HOST}b",
                        rev="old_sha",
                    )
                },
                "b": {},
            },
            topo_order=["b", "a"],
        )

        mock_forge = MagicMock()

        # No duplicate PR for the dep branch, but two OTHER current (open +
        # mergeable) pin-bump PRs already hold the single available slot.
        def _pr_status(*, source_branch):
            if source_branch in ("mill/pin-bump/x", "mill/pin-bump/y"):
                return {"state": "open", "mergeable": True, "url": source_branch}
            return None

        mock_forge.pr_status.side_effect = _pr_status
        mock_forge.list_open_pr_branches.return_value = {
            "mill/pin-bump/x",
            "mill/pin-bump/y",
        }

        import robotsix_mill.agents.runners.pin_bump_runner as runner_mod

        with (
            patch.object(
                runner_mod,
                "Settings",
                return_value=MagicMock(pin_bump_interval_seconds=86400),
            ),
            patch.object(runner_mod, "get_repos_config", return_value=registry),
            patch.object(runner_mod, "github_token", return_value="fake-token"),
            patch.object(runner_mod, "github_push_token", return_value="fake-token"),
            patch.object(runner_mod, "target_branch_for", return_value="main"),
            patch.object(runner_mod, "get_forge", return_value=mock_forge),
            patch.object(runner_mod.git_ops, "ls_remote_sha", return_value="new_sha"),
            patch.object(runner_mod.git_ops, "clone") as mock_clone,
            caplog.at_level(
                logging.INFO, logger="robotsix_mill.agents.runners.pin_bump_runner"
            ),
        ):
            run_pin_bump_pr_actuator(
                session_id="s1",
                repo_config=registry.repos["a"],
                graph=graph,
            )

        mock_clone.assert_not_called()
        mock_forge.open_merge_request.assert_not_called()
        assert "in-flight cap" in caplog.text

    def test_coherence_conflict_skips_pr(self, tmp_path, caplog):
        """A repo whose run_coherence_check reports conflicts is
        skipped with a WARNING and no PR."""
        import logging

        from robotsix_mill.agents.runners.pin_bump_runner import (
            run_pin_bump_pr_actuator,
        )
        from robotsix_mill.config.repos import load_repos_config
        from robotsix_mill.deps.internal_graph import (
            GitPin,
            InternalDepGraph,
        )

        repos_yaml_path = tmp_path / "repos.yaml"
        repos_yaml_path.write_text(_repos_yaml_str("a", "b"))
        registry = load_repos_config(str(repos_yaml_path))

        graph = InternalDepGraph(
            pins={
                "a": {
                    "b": GitPin(
                        git_url=f"https://{INTERNAL_GIT_HOST}b",
                        rev="old_sha",
                    )
                },
                "b": {},
            },
            topo_order=["b", "a"],
        )

        mock_forge = MagicMock()
        mock_forge.pr_status.return_value = None
        mock_forge.list_open_pr_branches.return_value = set()

        import robotsix_mill.agents.runners.pin_bump_runner as runner_mod

        with (
            patch.object(
                runner_mod,
                "Settings",
                return_value=MagicMock(pin_bump_interval_seconds=86400),
            ),
            patch.object(runner_mod, "get_repos_config", return_value=registry),
            patch.object(runner_mod, "github_token", return_value="fake-token"),
            patch.object(runner_mod, "github_push_token", return_value="fake-token"),
            patch.object(runner_mod, "target_branch_for", return_value="main"),
            patch.object(runner_mod, "get_forge", return_value=mock_forge),
            patch.object(runner_mod.git_ops, "ls_remote_sha", return_value="new_sha"),
            patch.object(runner_mod.git_ops, "clone") as mock_clone,
            patch.object(runner_mod.git_ops, "create_branch"),
            patch.object(runner_mod.git_ops, "commit_all"),
            patch.object(runner_mod.git_ops, "push"),
            patch.object(
                runner_mod,
                "run_coherence_check",
                return_value=["Requirements contain conflicting URLs for package foo:"],
            ),
            caplog.at_level(
                logging.WARNING, logger="robotsix_mill.agents.runners.pin_bump_runner"
            ),
        ):
            # Simulate clone creating a real pyproject.toml.
            def _fake_clone(remote_url, dest, branch, token=None):
                dest_path = Path(str(dest))
                dest_path.mkdir(parents=True, exist_ok=True)
                (dest_path / "pyproject.toml").write_text(
                    _pyproject({"b": _internal_pin("b", "old_sha")}),
                    encoding="utf-8",
                )

            mock_clone.side_effect = _fake_clone

            run_pin_bump_pr_actuator(
                session_id="s1",
                repo_config=registry.repos["a"],
                graph=graph,
            )

        # Clone happened, but PR was NOT opened because coherence
        # check reported conflicts.
        mock_forge.open_merge_request.assert_not_called()
        assert "coherence check" in caplog.text


_OLD_SHA = "0123456789abcdef0123456789abcdef01234567"
_NEW_SHA = "fedcba9876543210fedcba9876543210fedcba98"


def _graph_a_pins_b(rev=_OLD_SHA):
    from robotsix_mill.deps.internal_graph import GitPin, InternalDepGraph

    return InternalDepGraph(
        pins={
            "a": {"b": GitPin(git_url=f"https://{INTERNAL_GIT_HOST}b", rev=rev)},
            "b": {},
        },
        topo_order=["b", "a"],
    )


def _actuator_patches(runner_mod, registry, mock_forge, latest_sha):
    """Common patch context for an end-to-end actuator run."""
    return (
        patch.object(
            runner_mod,
            "Settings",
            return_value=MagicMock(pin_bump_interval_seconds=86400),
        ),
        patch.object(runner_mod, "get_repos_config", return_value=registry),
        patch.object(runner_mod, "github_token", return_value="fake-token"),
        patch.object(runner_mod, "github_push_token", return_value="fake-token"),
        patch.object(runner_mod, "target_branch_for", return_value="main"),
        patch.object(runner_mod, "get_forge", return_value=mock_forge),
        patch.object(runner_mod.git_ops, "ls_remote_sha", return_value=latest_sha),
    )


class TestSupersede:
    """A stale own PR is closed + re-opened instead of holding a cap slot."""

    def _registry(self, tmp_path):
        from robotsix_mill.config.repos import load_repos_config

        repos_yaml_path = tmp_path / "repos.yaml"
        repos_yaml_path.write_text(_repos_yaml_str("a", "b"))
        return load_repos_config(str(repos_yaml_path))

    def test_conflicting_pr_is_superseded_and_recreated(self, tmp_path, caplog):
        import logging

        from robotsix_mill.agents.runners.pin_bump_runner import (
            run_pin_bump_pr_actuator,
        )

        registry = self._registry(tmp_path)
        graph = _graph_a_pins_b()

        mock_forge = MagicMock()
        # Own PR exists for the dep branch but conflicts on uv.lock.
        mock_forge.pr_status.return_value = {
            "state": "open",
            "mergeable": False,
            "url": "https://github.com/damien-robotsix/a/pull/1",
        }
        mock_forge.list_open_prs.return_value = []
        # After the supersede closes it, no pin-bump PR is open → cap free.
        mock_forge.list_open_pr_branches.return_value = set()
        mock_forge.open_merge_request.return_value = (
            "https://github.com/damien-robotsix/a/pull/2"
        )

        import robotsix_mill.agents.runners.pin_bump_runner as runner_mod

        def _fake_clone(remote_url, dest, branch, token=None):
            dest_path = Path(str(dest))
            dest_path.mkdir(parents=True, exist_ok=True)
            (dest_path / "pyproject.toml").write_text(
                _pyproject({"b": _internal_pin("b", _OLD_SHA)}), encoding="utf-8"
            )

        with contextlib.ExitStack() as es:
            for cm in _actuator_patches(runner_mod, registry, mock_forge, _NEW_SHA):
                es.enter_context(cm)
            es.enter_context(
                patch.object(runner_mod.git_ops, "clone", side_effect=_fake_clone)
            )
            es.enter_context(patch.object(runner_mod.git_ops, "create_branch"))
            es.enter_context(patch.object(runner_mod.git_ops, "commit_all"))
            es.enter_context(patch.object(runner_mod.git_ops, "push"))
            es.enter_context(
                patch.object(runner_mod, "run_coherence_check", return_value=[])
            )
            es.enter_context(
                caplog.at_level(
                    logging.INFO,
                    logger="robotsix_mill.agents.runners.pin_bump_runner",
                )
            )
            run_pin_bump_pr_actuator(
                session_id="s1", repo_config=registry.repos["a"], graph=graph
            )

        mock_forge.close_pr.assert_called_once_with(source_branch="mill/pin-bump/b")
        mock_forge.delete_branch.assert_called_once_with(branch="mill/pin-bump/b")
        mock_forge.post_pr_comment.assert_called_once()
        mock_forge.open_merge_request.assert_called_once()
        assert "superseded stale PRs" in caplog.text

    def test_outdated_target_pr_is_superseded(self, tmp_path, caplog):
        import logging

        from robotsix_mill.agents.runners.pin_bump_runner import (
            run_pin_bump_pr_actuator,
        )

        registry = self._registry(tmp_path)
        graph = _graph_a_pins_b()

        mock_forge = MagicMock()
        # Mergeable but targets an older rev than the latest.
        mock_forge.pr_status.return_value = {
            "state": "open",
            "mergeable": True,
            "url": "https://github.com/damien-robotsix/a/pull/1",
        }
        mock_forge.list_open_prs.return_value = [
            {
                "branch": "mill/pin-bump/b",
                "title": f"chore(deps): bump b pin to {_OLD_SHA[:7]}",
            }
        ]
        mock_forge.list_open_pr_branches.return_value = set()
        mock_forge.open_merge_request.return_value = (
            "https://github.com/damien-robotsix/a/pull/2"
        )

        import robotsix_mill.agents.runners.pin_bump_runner as runner_mod

        def _fake_clone(remote_url, dest, branch, token=None):
            dest_path = Path(str(dest))
            dest_path.mkdir(parents=True, exist_ok=True)
            (dest_path / "pyproject.toml").write_text(
                _pyproject({"b": _internal_pin("b", _OLD_SHA)}), encoding="utf-8"
            )

        with contextlib.ExitStack() as es:
            for cm in _actuator_patches(runner_mod, registry, mock_forge, _NEW_SHA):
                es.enter_context(cm)
            es.enter_context(
                patch.object(runner_mod.git_ops, "clone", side_effect=_fake_clone)
            )
            es.enter_context(patch.object(runner_mod.git_ops, "create_branch"))
            es.enter_context(patch.object(runner_mod.git_ops, "commit_all"))
            es.enter_context(patch.object(runner_mod.git_ops, "push"))
            es.enter_context(
                patch.object(runner_mod, "run_coherence_check", return_value=[])
            )
            es.enter_context(
                caplog.at_level(
                    logging.INFO,
                    logger="robotsix_mill.agents.runners.pin_bump_runner",
                )
            )
            run_pin_bump_pr_actuator(
                session_id="s1", repo_config=registry.repos["a"], graph=graph
            )

        mock_forge.close_pr.assert_called_once()
        mock_forge.open_merge_request.assert_called_once()

    def test_current_pr_is_kept_not_superseded(self, tmp_path, caplog):
        import logging

        from robotsix_mill.agents.runners.pin_bump_runner import (
            run_pin_bump_pr_actuator,
        )

        registry = self._registry(tmp_path)
        graph = _graph_a_pins_b()

        mock_forge = MagicMock()
        # Mergeable AND already targets the latest rev → nothing to do.
        mock_forge.pr_status.return_value = {
            "state": "open",
            "mergeable": True,
            "url": "https://github.com/damien-robotsix/a/pull/1",
        }
        mock_forge.list_open_prs.return_value = [
            {
                "branch": "mill/pin-bump/b",
                "title": f"chore(deps): bump b pin to {_NEW_SHA[:7]}",
            }
        ]

        import robotsix_mill.agents.runners.pin_bump_runner as runner_mod

        with contextlib.ExitStack() as es:
            for cm in _actuator_patches(runner_mod, registry, mock_forge, _NEW_SHA):
                es.enter_context(cm)
            mock_clone = es.enter_context(patch.object(runner_mod.git_ops, "clone"))
            es.enter_context(
                caplog.at_level(
                    logging.INFO,
                    logger="robotsix_mill.agents.runners.pin_bump_runner",
                )
            )
            run_pin_bump_pr_actuator(
                session_id="s1", repo_config=registry.repos["a"], graph=graph
            )

        mock_forge.close_pr.assert_not_called()
        mock_forge.open_merge_request.assert_not_called()
        mock_clone.assert_not_called()
        assert "already open" in caplog.text


class TestInflightAccounting:
    def test_inflight_current_count_excludes_conflicting(self):
        from robotsix_mill.agents.runners.pin_bump_runner import (
            _inflight_current_count,
        )

        forge = MagicMock()
        forge.list_open_pr_branches.return_value = {
            "mill/pin-bump/a",
            "mill/pin-bump/b",
            "some/other-branch",
        }

        def _pr_status(*, source_branch):
            if source_branch == "mill/pin-bump/a":
                return {"state": "open", "mergeable": True}
            if source_branch == "mill/pin-bump/b":
                return {"state": "open", "mergeable": False}  # conflicting
            return None

        forge.pr_status.side_effect = _pr_status
        # Only the mergeable pin-bump PR counts; the conflicting one does not.
        assert _inflight_current_count(forge) == 1

    def test_existing_pr_is_current_classification(self):
        from robotsix_mill.agents.runners.pin_bump_runner import (
            _existing_pr_is_current,
        )

        current_title = f"chore(deps): bump b pin to {_NEW_SHA[:7]}"
        old_title = f"chore(deps): bump b pin to {_OLD_SHA[:7]}"
        # Targets latest + mergeable → current.
        assert _existing_pr_is_current(
            existing={"mergeable": True}, title=current_title, latest_sha=_NEW_SHA
        )
        # Targets an older rev → stale.
        assert not _existing_pr_is_current(
            existing={"mergeable": True}, title=old_title, latest_sha=_NEW_SHA
        )
        # Targets latest but conflicting → stale.
        assert not _existing_pr_is_current(
            existing={"mergeable": False}, title=current_title, latest_sha=_NEW_SHA
        )
        # Unknown title + mergeable → treated as current (conservative).
        assert _existing_pr_is_current(
            existing={"mergeable": None}, title=None, latest_sha=_NEW_SHA
        )


class TestOrderingAndCache:
    def test_bumps_highest_consumer_dep_first(self):
        import robotsix_mill.agents.runners.pin_bump_runner as runner_mod
        from robotsix_mill.deps.internal_graph import GitPin, InternalDepGraph

        url_lib = f"https://{INTERNAL_GIT_HOST}lib"
        url_tool = f"https://{INTERNAL_GIT_HOST}tool"
        graph = InternalDepGraph(
            pins={
                "a": {
                    "lib": GitPin(git_url=url_lib, rev=_OLD_SHA),
                    "tool": GitPin(git_url=url_tool, rev=_OLD_SHA),
                },
                "b": {"lib": GitPin(git_url=url_lib, rev=_OLD_SHA)},
            },
            topo_order=["a", "b"],
        )
        latest_map = {url_lib: _NEW_SHA, url_tool: _NEW_SHA}

        registry = MagicMock()
        rc = _make_repo_config(f"https://{INTERNAL_GIT_HOST}a")
        registry.repos = {
            "a": rc,
            "b": _make_repo_config(f"https://{INTERNAL_GIT_HOST}b"),
        }

        order: list[str] = []

        def _record(*, repo_id, dep_name, **kwargs):
            order.append(dep_name)

        with (
            patch.object(runner_mod, "github_push_token", return_value="t"),
            patch.object(runner_mod, "target_branch_for", return_value="main"),
            patch.object(runner_mod, "get_forge", return_value=MagicMock()),
            patch.object(runner_mod, "_apply_one_bump", side_effect=_record),
        ):
            runner_mod._bump_pins_for_repos(
                settings=MagicMock(),
                registry=registry,
                graph=graph,
                latest_map=latest_map,
            )

        # lib (2 consumers) is attempted before tool (1 consumer) within repo a.
        assert order[0] == "lib"
        assert order.index("lib") < order.index("tool")

    def test_pass_skipped_when_upstream_unchanged(self, tmp_path, caplog):
        import logging

        from robotsix_mill.agents.runners.pin_bump_runner import (
            run_pin_bump_pr_actuator,
        )
        from robotsix_mill.config.repos import load_repos_config

        repos_yaml_path = tmp_path / "repos.yaml"
        repos_yaml_path.write_text(_repos_yaml_str("a", "b"))
        registry = load_repos_config(str(repos_yaml_path))
        graph = _graph_a_pins_b(rev=_NEW_SHA)  # already at latest

        mock_forge = MagicMock()

        import robotsix_mill.agents.runners.pin_bump_runner as runner_mod

        with contextlib.ExitStack() as es:
            for cm in _actuator_patches(runner_mod, registry, mock_forge, _NEW_SHA):
                es.enter_context(cm)
            mock_clone = es.enter_context(patch.object(runner_mod.git_ops, "clone"))
            es.enter_context(
                caplog.at_level(
                    logging.INFO,
                    logger="robotsix_mill.agents.runners.pin_bump_runner",
                )
            )
            # First pass primes the cache (pin already at latest → no PR).
            run_pin_bump_pr_actuator(
                session_id="s1", repo_config=registry.repos["a"], graph=graph
            )
            caplog.clear()
            # Second pass: identical ls-remote SHAs → short-circuit.
            run_pin_bump_pr_actuator(
                session_id="s2", repo_config=registry.repos["a"], graph=graph
            )

        mock_clone.assert_not_called()
        assert "upstream SHAs unchanged" in caplog.text


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
