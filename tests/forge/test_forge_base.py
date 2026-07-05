"""Tests for ``_detect_forge_kind`` and ``get_forge`` auto-detection."""

import pytest

from pydantic import ValidationError

from robotsix_mill.config import Settings
from robotsix_mill.forge import _detect_forge_kind, get_forge
from robotsix_mill.forge.github import GitHubForge
from robotsix_mill.forge.gitlab import GitLabForge


# ---------------------------------------------------------------------------
# _detect_forge_kind
# ---------------------------------------------------------------------------


class TestDetectForgeKind:
    def test_https_github(self):
        assert _detect_forge_kind("https://github.com/owner/repo.git") == "github"

    def test_git_github(self):
        assert _detect_forge_kind("git@github.com:owner/repo.git") == "github"

    def test_https_gitlab(self):
        assert _detect_forge_kind("https://gitlab.com/ns/project.git") == "gitlab"

    def test_git_gitlab(self):
        assert _detect_forge_kind("git@gitlab.com:ns/project.git") == "gitlab"

    def test_https_custom_domain_raises(self):
        url = "https://gitlab.mycompany.com/ns/project.git"
        with pytest.raises(RuntimeError, match="cannot auto-detect forge kind"):
            _detect_forge_kind(url)

    def test_git_custom_domain_raises(self):
        url = "git@gitlab.mycompany.com:ns/project.git"
        with pytest.raises(RuntimeError, match="cannot auto-detect forge kind"):
            _detect_forge_kind(url)

    def test_github_with_trailing_slash(self):
        assert _detect_forge_kind("https://github.com/owner/repo.git/") == "github"

    def test_github_without_git_suffix(self):
        assert _detect_forge_kind("https://github.com/owner/repo") == "github"


# ---------------------------------------------------------------------------
# get_forge with forge_kind="auto"
# ---------------------------------------------------------------------------


class TestGetForgeAuto:
    def test_auto_github_com_returns_github_forge(self):
        """forge_kind=auto with a github.com URL returns GitHubForge."""
        s = Settings(
            FORGE_KIND="auto",
            FORGE_REMOTE_URL="https://github.com/owner/repo.git",
        )
        forge = get_forge(s)
        assert isinstance(forge, GitHubForge)

    def test_auto_gitlab_com_returns_gitlab_forge(self):
        """forge_kind=auto with a gitlab.com URL returns GitLabForge."""
        s = Settings(
            FORGE_KIND="auto",
            FORGE_REMOTE_URL="https://gitlab.com/ns/project.git",
        )
        forge = get_forge(s)
        assert isinstance(forge, GitLabForge)

    def test_auto_custom_domain_raises(self):
        """forge_kind=auto with a custom domain raises RuntimeError."""
        s = Settings(
            FORGE_KIND="auto",
            FORGE_REMOTE_URL="https://gitlab.mycompany.com/ns/project.git",
        )
        with pytest.raises(RuntimeError, match="cannot auto-detect forge kind"):
            get_forge(s)

    def test_auto_no_remote_url_raises(self):
        """forge_kind=auto without forge_remote_url raises ValidationError
        from the cross-field validator (not from get_forge)."""
        with pytest.raises(ValidationError):
            Settings(FORGE_KIND="auto")

    def test_explicit_github_still_works(self):
        """forge_kind=github still returns GitHubForge (unchanged)."""
        s = Settings(
            FORGE_KIND="github",
            FORGE_REMOTE_URL="https://github.com/owner/repo.git",
        )
        forge = get_forge(s)
        assert isinstance(forge, GitHubForge)

    def test_explicit_gitlab_still_works(self):
        """forge_kind=gitlab still returns GitLabForge (unchanged)."""
        s = Settings(
            FORGE_KIND="gitlab",
            FORGE_REMOTE_URL="https://gitlab.com/ns/project.git",
        )
        forge = get_forge(s)
        assert isinstance(forge, GitLabForge)

    def test_none_raises(self):
        """forge_kind=none raises RuntimeError (unchanged)."""
        s = Settings(FORGE_KIND="none")
        with pytest.raises(RuntimeError, match="no forge configured"):
            get_forge(s)

    def test_auto_with_per_repo_remote(self):
        """forge_kind=auto uses per-repo forge_remote_url when provided."""
        s = Settings(
            FORGE_KIND="auto",
            FORGE_REMOTE_URL="https://github.com/global/repo.git",
        )
        from robotsix_mill.config import RepoConfig

        rc = RepoConfig(
            repo_id="test",
            
            langfuse_project_name="test",
            langfuse_public_key="pk",
            langfuse_secret_key="sk",
            forge_remote_url="https://gitlab.com/ns/project.git",
        )
        forge = get_forge(s, repo_config=rc)
        # Per-repo gitlab.com URL should win over the global github.com URL.
        assert isinstance(forge, GitLabForge)


# ---------------------------------------------------------------------------
# get_forge per-repo routing with an *explicit* global forge_kind
# (the multi-repo, mixed-forge production scenario: global FORGE_KIND=github
# but one repo is hosted on gitlab.com)
# ---------------------------------------------------------------------------


def _repo_config(url):
    from robotsix_mill.config import RepoConfig

    return RepoConfig(
        repo_id="test",
        
        langfuse_project_name="test",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
        forge_remote_url=url,
    )


class TestGetForgePerRepoRouting:
    def test_github_repo_config_no_regression(self):
        """A github.com repo_config + global github → GitHubForge,
        identical to today, with repo_config threaded through."""
        s = Settings(
            FORGE_KIND="github",
            FORGE_REMOTE_URL="https://github.com/owner/repo.git",
        )
        rc = _repo_config("https://github.com/owner/repo.git")
        forge = get_forge(s, repo_config=rc)
        assert isinstance(forge, GitHubForge)
        # The repo_config must reach the adapter for token minting.
        assert forge._repo_config is rc

    def test_gitlab_repo_config_overrides_global_github(self):
        """The fix: a gitlab.com repo_config under global FORGE_KIND=github
        routes to GitLabForge instead of crashing as GitHub."""
        s = Settings(
            FORGE_KIND="github",
            FORGE_REMOTE_URL="https://github.com/owner/repo.git",
        )
        rc = _repo_config("https://gitlab.com/damien_six_tii/robotsix-mill-gitlab")
        forge = get_forge(s, repo_config=rc)
        assert isinstance(forge, GitLabForge)

    def test_no_repo_config_github(self):
        """No repo_config + global github → GitHubForge (unchanged)."""
        s = Settings(
            FORGE_KIND="github",
            FORGE_REMOTE_URL="https://github.com/owner/repo.git",
        )
        forge = get_forge(s)
        assert isinstance(forge, GitHubForge)

    def test_per_repo_custom_domain_falls_back_to_global(self):
        """A repo_config URL on an ambiguous/custom domain falls back to
        the global forge_kind — get_forge must NOT raise RuntimeError."""
        s = Settings(
            FORGE_KIND="github",
            FORGE_REMOTE_URL="https://github.com/owner/repo.git",
        )
        rc = _repo_config("https://gitlab.mycompany.com/ns/project.git")
        forge = get_forge(s, repo_config=rc)
        assert isinstance(forge, GitHubForge)
