"""triage_boilerplate findings land on the owning (mill / workflows) board."""

from types import SimpleNamespace

from robotsix_mill.agents.periodic_base import PeriodicAgentResult
from robotsix_mill.agents.runners.pass_runner import run_agent_pass
from robotsix_mill.agents.runners.periodic_runner import (
    PERIODIC_PASS_CONFIGS,
    _with_result_routing,
)
from robotsix_mill.agents.runners.triage_boilerplate_runner import (
    MILL_REPO_ID,
    WORKFLOWS_REPO_ID,
    owner_repo_id,
    route_triage_boilerplate_drafts,
)
from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.models import SourceKind
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.workspace import Workspace

_GAP = "ci-failure-on-main-noise"


def _registry(*repo_ids):
    return SimpleNamespace(
        repos={r: SimpleNamespace(repo_id=r, board_id=r) for r in repo_ids}
    )


def _finding():
    return PeriodicAgentResult(
        updated_memory="mem",
        draft_titles=["ci source: auto-close stale `CI failure: X on main` tickets"],
        draft_bodies=["Close the ticket once the default branch is green again."],
        gap_ids=[_GAP],
    )


def _run(settings, scanned, tmp_path):
    service = TicketService(settings, board_id=scanned)
    agent_fn = _with_result_routing(
        lambda **_kw: _finding(),
        route_triage_boilerplate_drafts,
        SimpleNamespace(repo_id=scanned, board_id=scanned),
    )
    return run_agent_pass(
        agent_fn,
        memory_file=tmp_path / f"{scanned}_memory.md",
        source_label=SourceKind.TRIAGE_BOILERPLATE,
        service=service,
        settings=settings,
        origin_session="s",
    )


def test_triage_boilerplate_registry_entry_routes_results():
    cfg = PERIODIC_PASS_CONFIGS["triage_boilerplate"]
    assert cfg.route_result_fn is route_triage_boilerplate_drafts


def test_ci_failure_finding_filed_on_mill_board_with_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "robotsix_mill.config.get_repos_config",
        lambda: _registry(MILL_REPO_ID, "invest", "cost-monitor"),
    )
    settings = Settings(data_dir=str(tmp_path / "data"))
    db.reset_engine()
    for board in (MILL_REPO_ID, "invest", "cost-monitor"):
        db.init_db(settings, board_id=board)

    first = _run(settings, "invest", tmp_path)
    assert len(first.drafts_created) == 1
    mill = TicketService(settings, board_id=MILL_REPO_ID)
    (ticket,) = mill.list()
    assert ticket.board_id == MILL_REPO_ID
    assert ticket.source == SourceKind.TRIAGE_BOILERPLATE
    desc = Workspace(
        settings.workspaces_dir_for(MILL_REPO_ID), ticket.id
    ).read_description()
    assert "Observed on: invest" in desc
    assert TicketService(settings, board_id="invest").list() == []

    # Same pattern on a second repo: one mill ticket, both repos as evidence.
    second = _run(settings, "cost-monitor", tmp_path)
    assert second.drafts_created == []
    assert len(mill.list()) == 1
    assert TicketService(settings, board_id="cost-monitor").list() == []
    comments = [c.body for c in mill.list_comments(ticket.id)]
    assert comments == ["Observed on: cost-monitor"]

    # Re-scan of an already-recorded repo adds no duplicate evidence.
    _run(settings, "cost-monitor", tmp_path)
    assert len(mill.list_comments(ticket.id)) == 1
    db.reset_engine()


def test_owner_repo_id_classification(monkeypatch):
    monkeypatch.setattr(
        "robotsix_mill.config.get_repos_config",
        lambda: _registry(MILL_REPO_ID, WORKFLOWS_REPO_ID),
    )
    assert owner_repo_id("refine: close stale tickets", "") == MILL_REPO_ID
    assert owner_repo_id("x", "python-ci.yml reusable workflow") == WORKFLOWS_REPO_ID
    # A scanned-repo target chosen by the agent is overridden.
    assert owner_repo_id("ci_fix: x", "", requested="invest") == MILL_REPO_ID
    assert owner_repo_id("x", "", requested=WORKFLOWS_REPO_ID) == WORKFLOWS_REPO_ID


def test_workflows_falls_back_to_mill_when_unregistered(monkeypatch):
    monkeypatch.setattr(
        "robotsix_mill.config.get_repos_config", lambda: _registry(MILL_REPO_ID)
    )
    assert owner_repo_id("x", "reusable workflow pin") == MILL_REPO_ID
