import pytest
from pydantic import ValidationError

from robotsix_mill.agents.review_revision import (
    ReviewRevisionResult,
    run_review_revision_agent,
)


class FakeRunSyncResult:
    """Simulates pydantic-ai's agent.run_sync return type."""

    def __init__(self, output: ReviewRevisionResult):
        self.output = output


class FakeAgent:
    """Simulates a pydantic-ai agent — just returns the canned result."""

    def __init__(self, result: ReviewRevisionResult):
        self._result = result

    def run_sync(self, msg, message_history=None, board_id="", usage_limits=None):
        return FakeRunSyncResult(self._result)


def _patch_agent(monkeypatch, status, summary, updated_memory=""):
    """Shorthand to patch build_agent_from_definition to return a FakeAgent."""
    import robotsix_mill.agents.base as base_mod

    monkeypatch.setattr(
        base_mod,
        "build_agent_from_definition",
        lambda *args, **kwargs: FakeAgent(
            ReviewRevisionResult(
                status=status, summary=summary, updated_memory=updated_memory
            )
        ),
    )


# ---------------------------------------------------------------------------
# Happy path — DONE
# ---------------------------------------------------------------------------


def test_happy_path_done(settings, tmp_path, monkeypatch, secrets_set):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    secrets_set(openrouter_api_key="sk-test")
    _patch_agent(monkeypatch, "DONE", "fixed typo in utils.py")

    result = run_review_revision_agent(
        settings=settings,
        repo_dir=repo_dir,
        branch="feature/x",
        review_comments="please fix the typo",
        pr_files=["src/utils.py"],
    )

    assert result.status == "DONE"
    assert result.summary == "fixed typo in utils.py"


# ---------------------------------------------------------------------------
# FAILED path
# ---------------------------------------------------------------------------


def test_failed_path(settings, tmp_path, monkeypatch, secrets_set):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    secrets_set(openrouter_api_key="sk-test")
    _patch_agent(monkeypatch, "FAILED", "cannot implement — requires redesign")

    result = run_review_revision_agent(
        settings=settings,
        repo_dir=repo_dir,
        branch="feature/x",
        review_comments="rewrite the auth module",
        pr_files=["src/auth.py"],
    )

    assert result.status == "FAILED"
    assert result.summary == "cannot implement — requires redesign"


# ---------------------------------------------------------------------------
# Memory passthrough
# ---------------------------------------------------------------------------


def test_memory_passthrough(settings, tmp_path, monkeypatch, secrets_set):
    """Verify memory flows into the user prompt and updated_memory flows back."""
    import robotsix_mill.agents.base as base_mod

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    secrets_set(openrouter_api_key="sk-test")

    captured_user_prompt: list[str] = []
    # Pre-seed the list with a slot so capturing_run_sync can assign to [0].
    captured_user_prompt.append("")

    def fake_build_agent_from_definition(
        settings, definition, tools=None, system_prompt=None, **kwargs
    ):
        return FakeAgent(
            ReviewRevisionResult(
                status="DONE",
                summary="done",
                updated_memory="Learned: the project uses ruff for linting.",
            )
        )

    monkeypatch.setattr(
        base_mod, "build_agent_from_definition", fake_build_agent_from_definition
    )

    # Capture the user prompt by wrapping agent.run_sync
    def capturing_run_sync(
        self, msg, message_history=None, board_id="", usage_limits=None
    ):
        captured_user_prompt[0] = msg
        return FakeRunSyncResult(self._result)

    monkeypatch.setattr(FakeAgent, "run_sync", capturing_run_sync)

    result = run_review_revision_agent(
        settings=settings,
        repo_dir=repo_dir,
        branch="feature/x",
        review_comments="use ruff format",
        pr_files=["src/main.py"],
        memory="Previous run: project uses black.",
    )

    assert result.status == "DONE"
    assert result.updated_memory == "Learned: the project uses ruff for linting."
    assert "Previous run: project uses black." in captured_user_prompt[0]
    assert "````memory" in captured_user_prompt[0]


# ---------------------------------------------------------------------------
# API key check
# ---------------------------------------------------------------------------


def test_missing_api_key_raises_runtime_error(settings, tmp_path):
    """The autouse _reset_secrets_each_test fixture clears the cached
    Secrets singleton, so get_secrets().openrouter_api_key is None by
    default.  Without a secrets_set() call, run_review_revision_agent
    must raise RuntimeError."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY is not set"):
        run_review_revision_agent(
            settings=settings,
            repo_dir=repo_dir,
            branch="feature/x",
            review_comments="fix this",
            pr_files=["src/x.py"],
        )


# ---------------------------------------------------------------------------
# ReviewRevisionResult model validation
# ---------------------------------------------------------------------------


def test_review_revision_result_validation():
    result = ReviewRevisionResult(status="DONE", summary="all changes applied")
    assert result.status == "DONE"
    assert result.summary == "all changes applied"
    assert result.updated_memory == ""

    result2 = ReviewRevisionResult(
        status="FAILED", summary="cannot implement", updated_memory="remember: ruff"
    )
    assert result2.status == "FAILED"
    assert result2.summary == "cannot implement"
    assert result2.updated_memory == "remember: ruff"


def test_review_revision_result_invalid_status_rejected():
    with pytest.raises(ValidationError):
        ReviewRevisionResult(status="INVALID", summary="nope")


def test_review_revision_result_missing_required_fields_rejected():
    with pytest.raises(ValidationError):
        ReviewRevisionResult(status="DONE")  # type: ignore[call-arg]
