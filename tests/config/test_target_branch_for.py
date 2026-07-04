"""Unit tests for ``target_branch_for``.

The effective target branch is ``repo_config.working_branch`` when set,
else ``settings.forge_target_branch`` — zero change for existing boards.
"""

from robotsix_mill.config import Settings, RepoConfig, target_branch_for


def _repo_config(working_branch):
    return RepoConfig(
        repo_id="test-repo",
        langfuse_project_name="test-project",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
        working_branch=working_branch,
    )


def test_working_branch_set_returns_it():
    repo_config = _repo_config("lyrical")
    settings = Settings()
    assert target_branch_for(settings, repo_config) == "lyrical"


def test_working_branch_none_falls_back():
    repo_config = _repo_config(None)
    settings = Settings()
    assert target_branch_for(settings, repo_config) == "main"


def test_working_branch_empty_falls_back():
    repo_config = _repo_config("")
    settings = Settings()
    assert target_branch_for(settings, repo_config) == "main"


def test_repo_config_none_falls_back():
    assert target_branch_for(Settings(), None) == "main"


def test_custom_forge_target_branch():
    settings = Settings(FORGE_TARGET_BRANCH="develop")
    repo_config = _repo_config(None)
    assert target_branch_for(settings, repo_config) == "develop"
