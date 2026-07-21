"""Tests for ``clone_all_repos`` in ``robotsix_mill.vcs``."""

from __future__ import annotations

import subprocess

from robotsix_mill.config import RepoConfig, ReposRegistry, Settings
from robotsix_mill.vcs import clone_all_repos


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _repo(
    repo_id: str,
    *,
    forge_remote_url: str | None = None,
    board_id: str | None = None,
) -> RepoConfig:
    return RepoConfig(
        repo_id=repo_id,
        board_id=board_id or repo_id,
        langfuse_project_name=f"proj-{repo_id}",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
        forge_remote_url=forge_remote_url,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCloneAllRepos:
    def test_basic_clone(self, settings: Settings, monkeypatch):
        """A repo with a forge_remote_url gets cloned to the expected path."""
        reg = ReposRegistry(
            repos={
                "alpha": _repo("alpha", forge_remote_url="https://gh.com/a.git"),
            }
        )
        monkeypatch.setattr("robotsix_mill.vcs.get_repos_config", lambda: reg)
        monkeypatch.setattr(
            "robotsix_mill.vcs.github_token",
            lambda _settings, repo_config: "tok",
        )

        call_args: list = []

        def _fake_clone(remote_url, dest, branch, token, **kwargs):
            call_args.append((remote_url, dest, branch, token))
            dest.mkdir(parents=True, exist_ok=True)
            (dest / ".git").mkdir()

        monkeypatch.setattr("robotsix_mill.vcs.git_ops.clone", _fake_clone)

        result = clone_all_repos(settings)

        assert result == {
            "alpha": settings.data_dir / "meta" / "workspace" / "alpha" / "repo"
        }
        assert len(call_args) == 1
        url, dest, branch, token = call_args[0]
        assert url == "https://gh.com/a.git"
        assert dest == result["alpha"]
        assert branch == "main"
        assert token == "tok"

    def test_skips_repo_without_forge_remote_url(self, settings: Settings, monkeypatch):
        """Repos with forge_remote_url=None are excluded from the result."""
        reg = ReposRegistry(
            repos={
                "alpha": _repo("alpha", forge_remote_url="https://gh.com/a.git"),
                "no_remote": _repo("no_remote", forge_remote_url=None),
            }
        )
        monkeypatch.setattr("robotsix_mill.vcs.get_repos_config", lambda: reg)
        monkeypatch.setattr(
            "robotsix_mill.vcs.github_token",
            lambda _settings, repo_config: "tok",
        )

        call_args: list = []

        def _fake_clone(remote_url, dest, branch, token, **kwargs):
            call_args.append((remote_url, dest, branch, token))
            dest.mkdir(parents=True, exist_ok=True)
            (dest / ".git").mkdir()

        monkeypatch.setattr("robotsix_mill.vcs.git_ops.clone", _fake_clone)

        result = clone_all_repos(settings)

        assert set(result.keys()) == {"alpha"}
        assert "no_remote" not in result
        assert len(call_args) == 1

    def test_each_call_wipes_and_reclones_fresh(self, settings: Settings, monkeypatch):
        """Each call wipes any existing workspace and clones FRESH, so periodic
        agents never analyse a stale reused tree."""
        reg = ReposRegistry(
            repos={
                "alpha": _repo("alpha", forge_remote_url="https://gh.com/a.git"),
            }
        )
        monkeypatch.setattr("robotsix_mill.vcs.get_repos_config", lambda: reg)
        monkeypatch.setattr(
            "robotsix_mill.vcs.github_token",
            lambda _settings, repo_config: "tok",
        )

        call_args: list = []

        def _fake_clone(remote_url, dest, branch, token, **kwargs):
            call_args.append((remote_url, dest, branch, token))
            dest.mkdir(parents=True, exist_ok=True)
            (dest / ".git").mkdir()
            (dest / "stale.txt").write_text("from a previous run")

        monkeypatch.setattr("robotsix_mill.vcs.git_ops.clone", _fake_clone)

        result1 = clone_all_repos(settings)
        assert len(call_args) == 1
        dest = result1["alpha"]
        assert (dest / "stale.txt").exists()

        # Second call must wipe the prior workspace and clone again.
        result2 = clone_all_repos(settings)
        assert len(call_args) == 2  # fresh clone every run
        assert result1 == result2
        # The wipe happened before the re-clone (the fake clone re-creates it).
        assert (dest / ".git").exists()

    def test_error_resilience_one_fails_others_succeed(
        self, settings: Settings, monkeypatch
    ):
        """A CalledProcessError on one repo doesn't prevent the others."""
        reg = ReposRegistry(
            repos={
                "alpha": _repo("alpha", forge_remote_url="https://gh.com/a.git"),
                "bad": _repo("bad", forge_remote_url="https://gh.com/bad.git"),
                "gamma": _repo("gamma", forge_remote_url="https://gh.com/g.git"),
            }
        )
        monkeypatch.setattr("robotsix_mill.vcs.get_repos_config", lambda: reg)
        monkeypatch.setattr(
            "robotsix_mill.vcs.github_token",
            lambda _settings, repo_config: "tok",
        )

        call_args: list = []

        def _fake_clone(remote_url, dest, branch, token, **kwargs):
            call_args.append((remote_url, dest, branch, token))
            if "bad" in remote_url:
                raise subprocess.CalledProcessError(
                    128, ["git", "clone"], stderr="remote not found"
                )
            dest.mkdir(parents=True, exist_ok=True)
            (dest / ".git").mkdir()

        monkeypatch.setattr("robotsix_mill.vcs.git_ops.clone", _fake_clone)

        result = clone_all_repos(settings)

        assert set(result.keys()) == {"alpha", "gamma"}
        assert "bad" not in result
        assert len(call_args) == 3  # all three attempted

    def test_token_none_still_attempts_clone(self, settings: Settings, monkeypatch):
        """When github_token raises RuntimeError, token is None but clone is
        still attempted (and may fail, which is caught)."""
        reg = ReposRegistry(
            repos={
                "alpha": _repo("alpha", forge_remote_url="https://gh.com/a.git"),
            }
        )
        monkeypatch.setattr("robotsix_mill.vcs.get_repos_config", lambda: reg)
        monkeypatch.setattr(
            "robotsix_mill.vcs.github_token",
            lambda _settings, repo_config: (_ for _ in ()).throw(
                RuntimeError("no creds")
            ),
        )

        call_args: list = []

        def _fake_clone(remote_url, dest, branch, token, **kwargs):
            call_args.append((remote_url, dest, branch, token))
            # clone fails because no token (public repo would work, but
            # we simulate a private-repo failure)
            raise subprocess.CalledProcessError(
                128, ["git", "clone"], stderr="authentication failed"
            )

        monkeypatch.setattr("robotsix_mill.vcs.git_ops.clone", _fake_clone)

        result = clone_all_repos(settings)

        assert result == {}
        assert len(call_args) == 1
        assert call_args[0][3] is None  # token passed as None

    def test_multiple_repos_all_cloned(self, settings: Settings, monkeypatch):
        """All repos with forge_remote_url are cloned."""
        reg = ReposRegistry(
            repos={
                "alpha": _repo("alpha", forge_remote_url="https://gh.com/a.git"),
                "beta": _repo("beta", forge_remote_url="https://gh.com/b.git"),
                "gamma": _repo("gamma", forge_remote_url="https://gh.com/g.git"),
            }
        )
        monkeypatch.setattr("robotsix_mill.vcs.get_repos_config", lambda: reg)
        monkeypatch.setattr(
            "robotsix_mill.vcs.github_token",
            lambda _settings, repo_config: "tok",
        )

        call_args: list = []

        def _fake_clone(remote_url, dest, branch, token, **kwargs):
            call_args.append((remote_url, dest, branch, token))
            dest.mkdir(parents=True, exist_ok=True)
            (dest / ".git").mkdir()

        monkeypatch.setattr("robotsix_mill.vcs.git_ops.clone", _fake_clone)

        result = clone_all_repos(settings)

        assert set(result.keys()) == {"alpha", "beta", "gamma"}
        for repo_id in ("alpha", "beta", "gamma"):
            expected = settings.data_dir / "meta" / "workspace" / repo_id / "repo"
            assert result[repo_id] == expected
            assert (expected / ".git").is_dir()
        assert len(call_args) == 3

    def test_empty_repo_bootstrapped(self, settings: Settings, monkeypatch):
        """clone() handles empty repos internally — clone_all_repos just
        sees a successful clone and includes the repo in the result."""
        reg = ReposRegistry(
            repos={
                "empty": _repo("empty", forge_remote_url="https://gh.com/e.git"),
            }
        )
        monkeypatch.setattr("robotsix_mill.vcs.get_repos_config", lambda: reg)
        monkeypatch.setattr(
            "robotsix_mill.vcs.github_token",
            lambda _settings, repo_config: "tok",
        )

        clone_call_count = 0

        def _fake_clone(remote_url, dest, branch, token, **kwargs):
            nonlocal clone_call_count
            clone_call_count += 1
            # Simulate a successful clone (internally bootstrapped).
            dest.parent.mkdir(parents=True, exist_ok=True)
            (dest / ".git").mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr("robotsix_mill.vcs.git_ops.clone", _fake_clone)

        result = clone_all_repos(settings)

        assert clone_call_count == 1
        assert set(result.keys()) == {"empty"}

    def test_bootstrap_failure_logs_error(
        self, settings: Settings, caplog, monkeypatch
    ):
        """When clone() raises (bootstrap also failed), a WARNING is logged
        (clone_all_repos treats it as a standard clone failure — the bootstrap
        failure details are in the stderr)."""
        import logging

        caplog.set_level(logging.WARNING)

        reg = ReposRegistry(
            repos={
                "empty": _repo("empty", forge_remote_url="https://gh.com/e.git"),
            }
        )
        monkeypatch.setattr("robotsix_mill.vcs.get_repos_config", lambda: reg)
        monkeypatch.setattr(
            "robotsix_mill.vcs.github_token",
            lambda _settings, repo_config: "tok",
        )

        def _fake_clone(remote_url, dest, branch, token, **kwargs):
            raise subprocess.CalledProcessError(
                128,
                ["git", "clone"],
                stderr="fatal: Remote branch main not found in upstream origin\n"
                "(empty-repo bootstrap also failed: Permission denied)",
            )

        monkeypatch.setattr("robotsix_mill.vcs.git_ops.clone", _fake_clone)

        result = clone_all_repos(settings)

        assert result == {}
        warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) >= 1
        assert any(
            "remote branch not found" in msg or "clone failed" in msg
            for msg in warnings
        )

    def test_non_empty_clone_failure_still_logs_warning(
        self, settings: Settings, caplog, monkeypatch
    ):
        """A clone failure NOT caused by empty repo still logs WARNING."""
        import logging

        caplog.set_level(logging.WARNING)

        reg = ReposRegistry(
            repos={
                "bad": _repo("bad", forge_remote_url="https://gh.com/bad.git"),
            }
        )
        monkeypatch.setattr("robotsix_mill.vcs.get_repos_config", lambda: reg)
        monkeypatch.setattr(
            "robotsix_mill.vcs.github_token",
            lambda _settings, repo_config: "tok",
        )

        def _fake_clone(remote_url, dest, branch, token, **kwargs):
            raise subprocess.CalledProcessError(
                128,
                ["git", "clone"],
                stderr="fatal: repository 'https://gh.com/bad.git' not found",
            )

        monkeypatch.setattr("robotsix_mill.vcs.git_ops.clone", _fake_clone)

        result = clone_all_repos(settings)

        assert result == {}
        warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("clone failed" in msg for msg in warnings)
        # No bootstrap was attempted
        errors = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
        assert not any("bootstrap" in msg.lower() for msg in errors)

    def test_existing_per_repo_workspaces_untouched(
        self, settings: Settings, monkeypatch
    ):
        """Existing audit/health workspace dirs are not affected."""
        reg = ReposRegistry(
            repos={
                "alpha": _repo("alpha", forge_remote_url="https://gh.com/a.git"),
            }
        )
        monkeypatch.setattr("robotsix_mill.vcs.get_repos_config", lambda: reg)
        monkeypatch.setattr(
            "robotsix_mill.vcs.github_token",
            lambda _settings, repo_config: "tok",
        )

        def _fake_clone(remote_url, dest, branch, token, **kwargs):
            dest.mkdir(parents=True, exist_ok=True)
            (dest / ".git").mkdir()

        monkeypatch.setattr("robotsix_mill.vcs.git_ops.clone", _fake_clone)

        # Create existing per-repo workspace dirs
        audit_ws = settings.data_dir / "audit_workspace" / "repo"
        audit_ws.mkdir(parents=True)
        (audit_ws / "some_file.txt").write_text("audit data")

        health_ws = settings.data_dir / "health_workspace" / "repo"
        health_ws.mkdir(parents=True)
        (health_ws / "health_file.txt").write_text("health data")

        result = clone_all_repos(settings)

        # Audit/health workspaces are untouched
        assert audit_ws.exists()
        assert (audit_ws / "some_file.txt").read_text() == "audit data"
        assert health_ws.exists()
        assert (health_ws / "health_file.txt").read_text() == "health data"

        # Meta workspace was created
        assert (
            result["alpha"]
            == settings.data_dir / "meta" / "workspace" / "alpha" / "repo"
        )
