"""Tests for pin_bump_runner — monkeypatch-only, no real I/O."""

from __future__ import annotations

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
        computes and logs the topological order and current pins."""
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
            patch.object(runner_mod, "get_forge"),
            patch.object(runner_mod.git_ops, "clone", side_effect=_fake_clone),
            patch.object(runner_mod.git_ops, "ls_remote_sha", return_value="sha_b"),
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
        # Actuator was called and checked the pin; it is already at latest
        # (sha_b == sha_b), so the pin was skipped (no PR created).

    def test_pr_actuator_is_noop_when_no_pins(self, tmp_path, caplog):
        """When repos have no internal pins, the actuator runs but
        is a no-op (no forge calls, no PRs created)."""
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
            patch.object(runner_mod, "get_forge") as mock_get_forge,
            patch.object(runner_mod.git_ops, "clone", side_effect=_fake_clone),
            patch.object(runner_mod.git_ops, "ls_remote_sha") as mock_ls_remote,
            caplog.at_level(
                logging.INFO, logger="robotsix_mill.runners.pin_bump_runner"
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

    def test_disabled_by_config_returns_early(self, tmp_path, caplog):
        """When settings.periodic.pin_bump_periodic is False, the pass
        returns immediately without doing any work."""
        import logging

        from robotsix_mill.runners.pin_bump_runner import run_pin_bump_pass

        import robotsix_mill.runners.pin_bump_runner as runner_mod

        mock_settings = MagicMock()
        mock_settings.pin_bump_periodic = False

        with (
            patch.object(runner_mod, "Settings", return_value=mock_settings),
            patch.object(runner_mod, "get_repos_config") as mock_registry,
            patch.object(runner_mod, "github_token") as mock_token,
            caplog.at_level(
                logging.INFO, logger="robotsix_mill.runners.pin_bump_runner"
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
        from robotsix_mill.runners.pin_bump_runner import _update_pin_rev

        content = _pyproject({"b": _internal_pin("b", "old_sha")})
        result = _update_pin_rev(content, "b", "new_sha")
        assert 'rev = "old_sha"' not in result
        assert 'rev = "new_sha"' in result

    def test_update_pin_rev_multiple_deps(self):
        """_update_pin_rev updates only the targeted dep, not others."""
        from robotsix_mill.runners.pin_bump_runner import _update_pin_rev

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

    def test_update_pin_rev_missing_raises(self):
        """_update_pin_rev raises ValueError when the dep is not found."""
        from robotsix_mill.runners.pin_bump_runner import _update_pin_rev

        content = _pyproject()
        with pytest.raises(ValueError, match="Could not find rev"):
            _update_pin_rev(content, "nonexistent", "sha")

    def test_actuator_standalone_noop(self, tmp_path, caplog):
        """When called standalone with no pins, the actuator is a no-op."""
        import logging

        from robotsix_mill.config.repos import load_repos_config
        from robotsix_mill.runners.pin_bump_runner import (
            run_pin_bump_pr_actuator,
        )

        repos_yaml_path = tmp_path / "repos.yaml"
        repos_yaml_path.write_text(_repos_yaml_str("x"))
        registry = load_repos_config(str(repos_yaml_path))

        def _fake_clone(remote_url, dest, branch, token=None):
            dest_path = Path(str(dest))
            dest_path.mkdir(parents=True, exist_ok=True)
            (dest_path / "pyproject.toml").write_text(_pyproject(), encoding="utf-8")

        import robotsix_mill.runners.pin_bump_runner as runner_mod

        with (
            patch.object(runner_mod, "Settings"),
            patch.object(runner_mod, "get_repos_config", return_value=registry),
            patch.object(runner_mod, "github_token", return_value="fake-token"),
            patch.object(runner_mod, "target_branch_for", return_value="main"),
            patch.object(runner_mod, "get_forge") as mock_get_forge,
            patch.object(runner_mod.git_ops, "clone", side_effect=_fake_clone),
            patch.object(runner_mod.git_ops, "ls_remote_sha") as mock_ls,
            caplog.at_level(
                logging.INFO, logger="robotsix_mill.runners.pin_bump_runner"
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

        from robotsix_mill.config.repos import load_repos_config
        from robotsix_mill.deps.internal_graph import (
            GitPin,
            InternalDepGraph,
        )
        from robotsix_mill.runners.pin_bump_runner import (
            run_pin_bump_pr_actuator,
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

        import robotsix_mill.runners.pin_bump_runner as runner_mod

        with (
            patch.object(runner_mod, "Settings"),
            patch.object(runner_mod, "get_repos_config", return_value=registry),
            patch.object(runner_mod, "github_token", return_value="fake-token"),
            patch.object(runner_mod, "target_branch_for", return_value="main"),
            patch.object(runner_mod, "get_forge", return_value=mock_forge),
            patch.object(runner_mod.git_ops, "ls_remote_sha", return_value="new_sha"),
            patch.object(runner_mod.git_ops, "clone") as mock_clone,
            patch.object(runner_mod.git_ops, "create_branch"),
            patch.object(runner_mod.git_ops, "commit_all"),
            patch.object(runner_mod.git_ops, "push"),
            patch.object(runner_mod, "run_coherence_check", return_value=[]),
            caplog.at_level(
                logging.INFO, logger="robotsix_mill.runners.pin_bump_runner"
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

        from robotsix_mill.config.repos import load_repos_config
        from robotsix_mill.deps.internal_graph import (
            GitPin,
            InternalDepGraph,
        )
        from robotsix_mill.runners.pin_bump_runner import (
            run_pin_bump_pr_actuator,
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

        import robotsix_mill.runners.pin_bump_runner as runner_mod

        with (
            patch.object(runner_mod, "Settings"),
            patch.object(runner_mod, "get_repos_config", return_value=registry),
            patch.object(runner_mod, "github_token", return_value="fake-token"),
            patch.object(runner_mod, "target_branch_for", return_value="main"),
            patch.object(runner_mod, "get_forge") as mock_get_forge,
            patch.object(runner_mod.git_ops, "ls_remote_sha", return_value="same_sha"),
            patch.object(runner_mod.git_ops, "clone") as mock_clone,
            caplog.at_level(
                logging.INFO, logger="robotsix_mill.runners.pin_bump_runner"
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

        from robotsix_mill.config.repos import load_repos_config
        from robotsix_mill.deps.internal_graph import (
            GitPin,
            InternalDepGraph,
        )
        from robotsix_mill.runners.pin_bump_runner import (
            run_pin_bump_pr_actuator,
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

        import robotsix_mill.runners.pin_bump_runner as runner_mod

        with (
            patch.object(runner_mod, "Settings"),
            patch.object(runner_mod, "get_repos_config", return_value=registry),
            patch.object(runner_mod, "github_token", return_value="fake-token"),
            patch.object(runner_mod, "target_branch_for", return_value="main"),
            patch.object(runner_mod, "get_forge", return_value=mock_forge),
            patch.object(runner_mod.git_ops, "ls_remote_sha", return_value="new_sha"),
            patch.object(runner_mod.git_ops, "clone") as mock_clone,
            caplog.at_level(
                logging.INFO, logger="robotsix_mill.runners.pin_bump_runner"
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

    def test_inflight_cap_skipped(self, tmp_path, caplog):
        """When in-flight pin-bump PRs reach max_inflight_prs, skip."""
        import logging

        from robotsix_mill.config.repos import load_repos_config
        from robotsix_mill.deps.internal_graph import (
            GitPin,
            InternalDepGraph,
        )
        from robotsix_mill.runners.pin_bump_runner import (
            run_pin_bump_pr_actuator,
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
        # No duplicate PR, but in-flight cap reached.
        mock_forge.pr_status.return_value = None
        mock_forge.list_open_pr_branches.return_value = {
            "mill/pin-bump/x",
            "mill/pin-bump/y",
        }

        import robotsix_mill.runners.pin_bump_runner as runner_mod

        with (
            patch.object(runner_mod, "Settings"),
            patch.object(runner_mod, "get_repos_config", return_value=registry),
            patch.object(runner_mod, "github_token", return_value="fake-token"),
            patch.object(runner_mod, "target_branch_for", return_value="main"),
            patch.object(runner_mod, "get_forge", return_value=mock_forge),
            patch.object(runner_mod.git_ops, "ls_remote_sha", return_value="new_sha"),
            patch.object(runner_mod.git_ops, "clone") as mock_clone,
            caplog.at_level(
                logging.INFO, logger="robotsix_mill.runners.pin_bump_runner"
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

        from robotsix_mill.config.repos import load_repos_config
        from robotsix_mill.deps.internal_graph import (
            GitPin,
            InternalDepGraph,
        )
        from robotsix_mill.runners.pin_bump_runner import (
            run_pin_bump_pr_actuator,
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

        import robotsix_mill.runners.pin_bump_runner as runner_mod

        with (
            patch.object(runner_mod, "Settings"),
            patch.object(runner_mod, "get_repos_config", return_value=registry),
            patch.object(runner_mod, "github_token", return_value="fake-token"),
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
                logging.WARNING, logger="robotsix_mill.runners.pin_bump_runner"
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
