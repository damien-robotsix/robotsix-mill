"""Default forge mocks for merge stage tests.

Any test that needs different behavior can override these with its own
``monkeypatch.setattr`` calls — the test's patches take precedence because
pytest fixtures execute before the test body.
"""

import pytest
from robotsix_mill.forge import github


@pytest.fixture(autouse=True)
def _default_forge_mocks(monkeypatch):
    """Provide harmless defaults for ``pr_files`` and
    ``get_authenticated_user_login`` so tests that exercise auto-merge
    eligibility (which calls both) don't fail on unmocked forge
    methods.

    Tests that need different behavior (e.g. sensitive files, different
    bot login) override these with their own ``monkeypatch.setattr``
    inside the test body — those take precedence.
    """
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_files",
        lambda self, *, source_branch: [],
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "get_authenticated_user_login",
        lambda self: "mill-bot",
    )
