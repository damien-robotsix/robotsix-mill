"""Tests for the board-cleanup agent and its bespoke runner."""

import pytest

from robotsix_mill.agents import board_cleanup as bc_agent
from robotsix_mill.agents import periodic_loader as pl
from robotsix_mill.core.models import SourceKind
from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.runners.periodic_runner import (
    BoardCleanupPassResult,
    run_board_cleanup_pass,
)
from robotsix_mill.runners.pass_runner import _GAP_ID_RE, ProposedActionItem


# --- Agent tests ---


def test_board_cleanup_system_prompt_covers_action_classes():
    """The board-cleanup prompt must cover all four proposed-action types
    and stay read-only with respect to the board."""
    p = bc_agent.SYSTEM_PROMPT.lower()
    # The four ProposedAction action types.
    assert "close" in p
    assert "transition" in p
    assert "comment" in p
    assert "relabel" in p
    # It emits proposals, not direct mutations.
    assert "proposed_actions" in p or "proposed action" in p
    # Must be read-only with respect to the board.
    assert "read-only" in p or "read only" in p
    # Must respect prior human decisions (de-duplication).
    assert "de-duplication" in p or "deduplication" in p or "decided" in p


def test_board_cleanup_result_model():
    """BoardCleanupResult has the expected fields and defaults."""
    result = bc_agent.BoardCleanupResult(
        updated_memory="memory",
        draft_titles=["title1"],
        draft_bodies=["body1"],
        gap_ids=["gap1"],
    )
    assert result.updated_memory == "memory"
    assert len(result.draft_titles) == 1
    assert len(result.draft_bodies) == 1
    assert len(result.gap_ids) == 1
    assert result.proposed_actions == []

    # Defaults
    default_result = bc_agent.BoardCleanupResult()
    assert default_result.updated_memory == ""
    assert default_result.draft_titles == []
    assert default_result.draft_bodies == []
    assert default_result.gap_ids == []
    assert default_result.proposed_actions == []


def test_board_cleanup_result_field_types():
    """BoardCleanupResult fields have correct types, including a
    ProposedActionItem in proposed_actions."""
    from robotsix_mill.runners.pass_runner import ProposedActionItem

    result = bc_agent.BoardCleanupResult(
        updated_memory="# Board Cleanup Memory\n",
        draft_titles=["Some draft"],
        draft_bodies=["body"],
        gap_ids=["anchor"],
        proposed_actions=[
            ProposedActionItem(
                target_ticket_id="abc1234",
                action_type="close",
                rationale="superseded — work merged elsewhere",
            )
        ],
    )
    assert isinstance(result.updated_memory, str)
    assert isinstance(result.draft_titles, list)
    assert isinstance(result.proposed_actions, list)
    assert result.proposed_actions[0].action_type == "close"


def test_max_drafts_is_reasonable():
    """MAX_DRAFTS should be a positive integer."""
    assert isinstance(bc_agent.MAX_DRAFTS, int)
    assert bc_agent.MAX_DRAFTS > 0


# --- Wiring tests ---


def test_source_kind_board_cleanup_exists():
    """SourceKind.BOARD_CLEANUP is defined with the expected value."""
    assert SourceKind.BOARD_CLEANUP == "board_cleanup"


def test_board_cleanup_kind_is_llm_agent():
    """The periodic loader classifies board_cleanup as an llm_agent."""
    assert pl.kind_for("board_cleanup") == "llm_agent"


def test_gap_id_re_matches_board_cleanup():
    """_GAP_ID_RE must match board_cleanup markers so de-duplication works."""
    marker = "<!-- board_cleanup-gap-id: stale_abc1234 -->"
    matches = _GAP_ID_RE.findall(marker)
    assert matches == [("board_cleanup", "stale_abc1234")]


def test_board_cleanup_definition_yaml_loads():
    """The built-in agent definition loads and is wired to the agent."""
    from pathlib import Path

    from robotsix_mill.agents.yaml_loader import load_agent_definition

    path = (
        Path(bc_agent.__file__).parent.parent.parent.parent
        / "agent_definitions"
        / "periodic"
        / "board_cleanup.yaml"
    )
    definition = load_agent_definition(path)
    assert definition.name == "board_cleanup"
    assert definition.output_type == "BoardCleanupResult"
    assert definition.module == "board_cleanup"
    assert definition.read_ticket is True


# --- Runner tests ---


def _test_repo_config():
    from robotsix_mill.config import RepoConfig

    return RepoConfig(
        repo_id="test-repo",
        board_id="test-board",
        langfuse_project_name="test-project",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
    )


def _make_settings(tmp_path, **overrides):
    overrides.setdefault("data_dir", str(tmp_path / "data"))
    s = Settings(**overrides)
    db.reset_engine()
    db.init_db(s, board_id="test-board")
    return s


def test_run_board_cleanup_pass_requires_repo_config(tmp_path):
    """A missing repo_config raises ValueError (no board-less fallback)."""
    settings = _make_settings(tmp_path)
    with pytest.raises(ValueError):
        run_board_cleanup_pass("sid", None, settings=settings)  # type: ignore[arg-type]


def test_run_board_cleanup_pass_injects_board_and_returns_result(tmp_path, monkeypatch):
    """The bespoke runner injects the live board snapshot into the agent,
    persists its proposed actions, and returns a BoardCleanupPassResult."""
    settings = _make_settings(tmp_path)
    service = TicketService(settings, board_id="test-board")
    ticket = service.create(
        "An obviously stale ticket",
        "body",
        source=SourceKind.USER,
    )

    captured = {}

    def fake_agent(*, settings, memory, recent_proposals, verified_proposals, **kw):
        captured["board_snapshot"] = kw.get("board_snapshot", "")
        return bc_agent.BoardCleanupResult(
            updated_memory="updated",
            proposed_actions=[
                ProposedActionItem(
                    target_ticket_id=ticket.id,
                    action_type="close",
                    rationale="superseded",
                )
            ],
        )

    monkeypatch.setattr(
        "robotsix_mill.agents.board_cleanup.run_board_cleanup_agent", fake_agent
    )

    result = run_board_cleanup_pass("sid", _test_repo_config(), settings=settings)

    assert isinstance(result, BoardCleanupPassResult)
    assert result.session_id == "sid"
    assert result.updated_memory == "updated"
    # The board snapshot threaded into the agent includes the live ticket.
    assert "An obviously stale ticket" in captured["board_snapshot"]
    # The proposed close action was persisted.
    assert len(result.proposed_actions) == 1
    assert result.proposed_actions[0]["action_type"] == "close"


def test_run_board_cleanup_pass_skips_agent_on_empty_board(tmp_path, monkeypatch):
    """An empty board short-circuits: the agent is never invoked and a no-op
    BoardCleanupPassResult is returned."""
    settings = _make_settings(tmp_path)

    def fake_agent(*args, **kwargs):
        raise AssertionError("run_board_cleanup_agent must not be called")

    monkeypatch.setattr(
        "robotsix_mill.agents.board_cleanup.run_board_cleanup_agent", fake_agent
    )

    result = run_board_cleanup_pass("sid", _test_repo_config(), settings=settings)

    assert isinstance(result, BoardCleanupPassResult)
    assert result.session_id == "sid"
    assert result.drafts_created == []
    assert result.proposed_actions == []


def test_run_board_cleanup_pass_skips_agent_on_fetch_failure(tmp_path, monkeypatch):
    """When recent_tickets() raises, the board falls back to [] and the agent
    is still not invoked — a no-op result is returned."""
    settings = _make_settings(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("DB not initialised")

    monkeypatch.setattr(TicketService, "recent_tickets", boom)

    def fake_agent(*args, **kwargs):
        raise AssertionError("run_board_cleanup_agent must not be called")

    monkeypatch.setattr(
        "robotsix_mill.agents.board_cleanup.run_board_cleanup_agent", fake_agent
    )

    result = run_board_cleanup_pass("sid", _test_repo_config(), settings=settings)

    assert isinstance(result, BoardCleanupPassResult)
    assert result.drafts_created == []
    assert result.proposed_actions == []


def test_run_board_cleanup_pass_honors_memory_path_override(tmp_path, monkeypatch):
    """When board_cleanup_memory_path is set, the runner persists the
    agent's memory to that pinned path instead of the per-repo default."""
    override = tmp_path / "pinned" / "board_cleanup_memory.md"
    settings = _make_settings(tmp_path, board_cleanup_memory_path=str(override))
    # A non-empty board so the agent (and thus memory persistence) actually runs.
    TicketService(settings, board_id="test-board").create(
        "A ticket", "body", source=SourceKind.USER
    )

    def fake_agent(*, settings, memory, recent_proposals, verified_proposals, **kw):
        return bc_agent.BoardCleanupResult(updated_memory="pinned-memory")

    monkeypatch.setattr(
        "robotsix_mill.agents.board_cleanup.run_board_cleanup_agent", fake_agent
    )

    run_board_cleanup_pass("sid", _test_repo_config(), settings=settings)

    # The override path was used (and its parent created); the per-repo
    # default path was NOT written.
    assert override.read_text() == "pinned-memory"
    default_path = settings.data_dir / "test-repo" / "board_cleanup_memory.md"
    assert not default_path.exists()
