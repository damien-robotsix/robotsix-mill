"""survey companion drafts land on the board of the repo their evidence names."""

from types import SimpleNamespace

from robotsix_mill.agents.periodic_base import PeriodicAgentResult
from robotsix_mill.agents.runners.pass_runner import run_agent_pass
from robotsix_mill.agents.runners.periodic_runner import (
    PERIODIC_PASS_CONFIGS,
    _with_result_routing,
)
from robotsix_mill.agents.runners.survey_runner import (
    STANDARDS_REPO_ID,
    evidence_repo_id,
    route_survey_drafts,
)
from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.models import SourceKind, TicketKind
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.workspace import Workspace

MILL = "robotsix-mill"
AUTO_MAIL = "robotsix-auto-mail"
_GAP = "ci_job_timeout_minutes"
_BOARDS = (MILL, AUTO_MAIL, STANDARDS_REPO_ID)


def _registry(*repo_ids):
    return SimpleNamespace(
        repos={r: SimpleNamespace(repo_id=r, board_id=r) for r in repo_ids}
    )


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "robotsix_mill.config.get_repos_config", lambda: _registry(*_BOARDS)
    )
    settings = Settings(data_dir=str(tmp_path / "data"))
    db.reset_engine()
    for board in _BOARDS:
        db.init_db(settings, board_id=board)
    return settings


def _finding():
    return PeriodicAgentResult(
        updated_memory="mem",
        draft_titles=[
            "Standard: CI jobs declare timeout-minutes",
            "Add job-level timeout-minutes to robotsix-mill CI workflow jobs",
        ],
        draft_bodies=[
            "Every workflow job should set timeout-minutes.",
            "Jobs in `.github/workflows/ci.yml` lack timeout-minutes.",
        ],
        gap_ids=[_GAP, _GAP],
        draft_target_repo_ids=[STANDARDS_REPO_ID, ""],
    )


def _run(settings, scanned, tmp_path, finding=_finding):
    service = TicketService(settings, board_id=scanned)
    agent_fn = _with_result_routing(
        lambda **_kw: finding(),
        route_survey_drafts,
        SimpleNamespace(repo_id=scanned, board_id=scanned),
    )
    return run_agent_pass(
        agent_fn,
        memory_file=tmp_path / f"{scanned}_memory.md",
        source_label=SourceKind.SURVEY,
        service=service,
        settings=settings,
        origin_session="s",
    )


def _tasks(settings, board):
    return [
        t
        for t in TicketService(settings, board_id=board).list()
        if t.kind != TicketKind.EPIC
    ]


def _epics(settings, board):
    return [
        t
        for t in TicketService(settings, board_id=board).list()
        if t.kind == TicketKind.EPIC
    ]


def test_survey_registry_entry_routes_results():
    assert PERIODIC_PASS_CONFIGS["survey"].route_result_fn is route_survey_drafts


def test_companion_with_mill_evidence_filed_on_mill_board(tmp_path, monkeypatch):
    settings = _setup(tmp_path, monkeypatch)
    created: list[tuple[str, str]] = []
    real_create = TicketService.create

    def _spy_create(self, title, *a, **kw):
        created.append((self.board_id, title))
        return real_create(self, title, *a, **kw)

    monkeypatch.setattr(TicketService, "create", _spy_create)

    result = _run(settings, AUTO_MAIL, tmp_path)

    assert len(result.drafts_created) == 2
    assert (
        MILL,
        "Add job-level timeout-minutes to robotsix-mill CI workflow jobs",
    ) in created
    # Nothing lands on the surveyed repo's board — no ticket, no epic.
    assert TicketService(settings, board_id=AUTO_MAIL).list() == []
    (companion,) = _tasks(settings, MILL)
    assert companion.title.startswith("Add job-level timeout-minutes")
    desc = Workspace(settings.workspaces_dir_for(MILL), companion.id).read_description()
    assert f"Observed on: {AUTO_MAIL}" in desc
    # The rollup epic lives on the same board as its child.
    (epic,) = _epics(settings, MILL)
    assert companion.parent_id == epic.id
    (standard,) = _tasks(settings, STANDARDS_REPO_ID)
    assert standard.parent_id == _epics(settings, STANDARDS_REPO_ID)[0].id


def test_companion_dedups_per_gap_and_target_repo(tmp_path, monkeypatch):
    settings = _setup(tmp_path, monkeypatch)
    _run(settings, AUTO_MAIL, tmp_path)
    # A second pass from another repo finding the same gap files nothing new.
    again = _run(settings, AUTO_MAIL, tmp_path)
    assert again.drafts_created == []
    assert len(_tasks(settings, MILL)) == 1
    assert len(_epics(settings, MILL)) == 1


def test_duplicate_gap_and_target_in_one_pass_collapsed():
    res = PeriodicAgentResult(
        updated_memory="",
        draft_titles=["a in robotsix-mill", "b in robotsix-mill", "c"],
        draft_bodies=["x", "y", "z"],
        gap_ids=["g", "g", "g"],
        draft_target_repo_ids=["", "", STANDARDS_REPO_ID],
    )
    routed = route_survey_drafts(
        res, SimpleNamespace(repo_id=AUTO_MAIL, board_id=AUTO_MAIL)
    )
    # robotsix-mill isn't registered here → both stay local, one survives.
    assert routed.draft_titles == ["a in robotsix-mill", "c"]
    assert routed.draft_target_repo_ids == ["", STANDARDS_REPO_ID]
    assert all(f"Observed on: {AUTO_MAIL}" in b for b in routed.draft_bodies)


def test_evidence_repo_id_rules():
    reg = [MILL, AUTO_MAIL, STANDARDS_REPO_ID]
    assert evidence_repo_id("Fix robotsix-mill CI", "", AUTO_MAIL, reg) == MILL
    assert (
        evidence_repo_id("Fix CI", "see src/robotsix_mill/x.py", AUTO_MAIL, reg) == MILL
    )
    # Naming the surveyed repo keeps the draft local.
    assert (
        evidence_repo_id("Fix robotsix-auto-mail CI", "robotsix-mill", AUTO_MAIL, reg)
        is None
    )
    assert evidence_repo_id("Fix CI", "nothing", AUTO_MAIL, reg) is None
    # The standards repo is never an evidence target.
    assert evidence_repo_id("robotsix-standards rule", "", AUTO_MAIL, reg) is None


def test_failed_child_create_leaves_no_empty_rollup(tmp_path, monkeypatch):
    settings = _setup(tmp_path, monkeypatch)
    real_create = TicketService.create

    def _create(self, title, *a, **kw):
        if kw.get("kind") != TicketKind.EPIC:
            raise RuntimeError("boom")
        return real_create(self, title, *a, **kw)

    monkeypatch.setattr(TicketService, "create", _create)
    result = _run(settings, AUTO_MAIL, tmp_path)
    assert result.drafts_created == []
    for board in _BOARDS:
        assert _epics(settings, board) == []
