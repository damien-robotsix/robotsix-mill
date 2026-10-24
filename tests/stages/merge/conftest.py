"""Default forge mocks for merge stage tests.

Any test that needs different behavior can override these with its own
``monkeypatch.setattr`` calls — the test's patches take precedence because
pytest fixtures execute before the test body.
"""

import pytest

from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.forge import github
from robotsix_mill.stages import StageContext


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


# ---------------------------------------------------------------------------
# Shared ticket/context builders for the merge-stage core tests
# ---------------------------------------------------------------------------


def _ctx(tmp_path, **env):
    db.reset_engine()
    env.setdefault("data_dir", str(tmp_path / "data"))
    repo_auto_merge_enabled = env.pop("repo_auto_merge_enabled", False)
    # Mirror forge_token into Secrets so get_secrets() works
    ft = env.pop("FORGE_TOKEN", None)
    if ft is not None:
        import robotsix_mill.config as _cfg
        from robotsix_mill.config import Secrets, _reset_secrets

        _reset_secrets()
        _cfg._secrets = Secrets(forge_token=ft)
    s = Settings(**env)
    db.init_db(s, board_id="test-board")
    from robotsix_mill.config import RepoConfig

    return StageContext(
        settings=s,
        service=TicketService(s, board_id="test-board"),
        repo_config=RepoConfig(
            repo_id="test-repo",
            board_id="test-board",
            langfuse_project_name="test",
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
            auto_merge_enabled=repo_auto_merge_enabled,
        ),
    )


def _human_mr_approval(ctx):
    t = ctx.service.create("x", "y")
    for st in (
        State.READY,
        State.DELIVERABLE,
        State.IMPLEMENT_COMPLETE,
        State.HUMAN_MR_APPROVAL,
    ):
        ctx.service.transition(t.id, st)
    ctx.service.set_branch(t.id, f"mill/{t.id}")
    return ctx.service.get(t.id)


def _implement_complete(ctx):
    """Create a ticket in IMPLEMENT_COMPLETE state (PR open, gates not verified)."""
    t = ctx.service.create("x", "y")
    for st in (State.READY, State.DELIVERABLE, State.IMPLEMENT_COMPLETE):
        ctx.service.transition(t.id, st)
    ctx.service.set_branch(t.id, f"mill/{t.id}")
    return ctx.service.get(t.id)


def _in_rebasing(ctx):
    """Create a ticket already in REBASING state."""
    t = _implement_complete(ctx)
    ctx.service.transition(t.id, State.REBASING, note="PR conflicting")
    return ctx.service.get(t.id)


def _gh(tmp_path, **extra):
    # When auto_merge_enabled is set to "true", also opt in the repo.
    if (
        extra.get("auto_merge_enabled") == "true"
        and "repo_auto_merge_enabled" not in extra
    ):
        extra["repo_auto_merge_enabled"] = True
    return _ctx(
        tmp_path,
        forge_kind="github",
        FORGE_TOKEN="t",
        forge_remote_url="https://github.com/o/r.git",
        **extra,
    )


def _write_review_artifact(
    ctx, ticket, *, verdict="APPROVE", eligible=True, comment="", head_sha=None
):
    """Helper: write a review.md artifact for auto-merge tests."""
    art_dir = ctx.service.workspace(ticket).artifacts_dir
    art_dir.mkdir(parents=True, exist_ok=True)
    text = f"verdict: {verdict}\nauto_merge_eligible: {str(eligible).lower()}\n"
    if head_sha is not None:
        text += f"head_sha: {head_sha}\n"
    if comment:
        text += f"comment: {comment}\n"
    (art_dir / "review.md").write_text(text, encoding="utf-8")
