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

        def _fake_clone(remote_url, dest, branch, token):
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

        def _fake_clone(remote_url, dest, branch, token):
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

        def _fake_clone(remote_url, dest, branch, token):
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

        def _fake_clone(remote_url, dest, branch, token):
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

        def _fake_clone(remote_url, dest, branch, token):
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

        def _fake_clone(remote_url, dest, branch, token):
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

        def _fake_clone(remote_url, dest, branch, token):
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
