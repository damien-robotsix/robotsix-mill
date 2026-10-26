"""module_size scan: one addressable child per finding + full epic report."""

import subprocess

from robotsix_mill.agents.runners import module_size_findings
from robotsix_mill.agents.runners.pass_runner import run_agent_pass
from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.models import SourceKind, TicketKind
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.workspace import Workspace


class _Res:
    def __init__(self, titles, bodies):
        self.updated_memory = "mem"
        self.draft_titles = titles
        self.draft_bodies = bodies
        self.gap_ids = []
        self.summary = ""


def _repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "src" / "pkg" / "big.py").write_text(
        "import os\n\n\nclass A:\n    pass\n\n\ndef f():\n    pass\n\n\n"
        "async def g():\n    pass\n",
        encoding="utf-8",
    )
    (repo / "src" / "pkg" / "huge.py").write_text("x = 1\n" * 7, encoding="utf-8")
    git = ["git", "-C", str(repo)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run(
        [*git, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"],
        check=True,
    )
    sha = subprocess.run(
        [*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    return repo, sha


def test_module_size_emits_one_child_per_finding_with_measurement(tmp_path):
    repo, sha = _repo(tmp_path)
    settings = Settings(data_dir=str(tmp_path / "data"))
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    titles = [
        "module size: split src/pkg/big.py (1200 lines, 3 top-level symbols)",
        "module size: split src/pkg/huge.py (1100 lines, 0 top-level symbols)",
    ]
    res = _Res(titles, ["Split big.", "Split huge."])

    result = run_agent_pass(
        lambda **_: res,
        memory_file=tmp_path / "memory.md",
        source_label=SourceKind.MODULE_SIZE,
        service=service,
        settings=settings,
        repo_dir=repo,
    )

    # Not collapsed: one child per finding, under one rollup epic.
    assert len(result.drafts_created) == 2
    tickets = service.list()
    epics = [t for t in tickets if t.kind == TicketKind.EPIC]
    children = [t for t in tickets if t.kind == TicketKind.TASK]
    assert len(epics) == 1 and len(children) == 2
    assert all(c.parent_id == epics[0].id for c in children)

    ws_dir = settings.workspaces_dir_for("test-board")
    by_title = {c.title: Workspace(ws_dir, c.id).read_description() for c in children}
    big = by_title[titles[0]]
    assert "- **Path:** `src/pkg/big.py`" in big
    assert "- **Line count:** 13" in big
    assert "- **Top-level symbol count:** 3" in big
    assert "- **Counting method:** " in big
    assert f"- **Commit:** `{sha}`" in big
    huge = by_title[titles[1]]
    assert "- **Path:** `src/pkg/huge.py`" in huge
    assert "- **Line count:** 7" in huge
    assert "- **Top-level symbol count:** 0" in huge

    # Full untruncated report posted on the parent epic.
    comments = service.list_comments(epics[0].id)
    assert len(comments) == 1
    report = comments[0].body
    for c in children:
        assert c.id in report
        assert c.title in report
    assert "Split big." in report and "Split huge." in report
    assert sha in report
    db.reset_engine()


def test_module_size_report_includes_findings_over_draft_cap(tmp_path):
    repo, _ = _repo(tmp_path)
    settings = Settings(data_dir=str(tmp_path / "data"))
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    titles = [
        "module size: split src/pkg/big.py (1200 lines, 3 top-level symbols)",
        "module size: split src/pkg/huge.py (1100 lines, 0 top-level symbols)",
    ]
    res = _Res(titles, ["Split big.", "Split huge."])

    result = run_agent_pass(
        lambda **_: res,
        memory_file=tmp_path / "memory.md",
        source_label=SourceKind.MODULE_SIZE,
        service=service,
        settings=settings,
        max_drafts=1,
        repo_dir=repo,
    )

    # Only one child under the cap, but the report lists both findings.
    assert len(result.drafts_created) == 1
    epic = next(t for t in service.list() if t.kind == TicketKind.EPIC)
    report = service.list_comments(epic.id)[0].body
    assert "2 finding(s)" in report
    assert titles[0] in report and titles[1] in report
    assert "Split huge." in report
    assert "- **Path:** `src/pkg/huge.py`" in report
    assert "- **Line count:** 7" in report
    assert "over the per-run draft cap" in report
    db.reset_engine()


def test_module_size_pass_without_filed_child_leaves_no_rollup_epic(tmp_path):
    repo, _ = _repo(tmp_path)
    settings = Settings(data_dir=str(tmp_path / "data"))
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    # Zero findings: nothing filed, no epic.
    run_agent_pass(
        lambda **_: _Res([], []),
        memory_file=tmp_path / "memory.md",
        source_label=SourceKind.MODULE_SIZE,
        service=service,
        settings=settings,
        repo_dir=repo,
    )
    assert service.list() == []

    # Findings exist but none becomes a child (draft cap 0): the report
    # must not create an empty rollup epic either.
    titles = ["module size: split src/pkg/big.py (1200 lines, 3 top-level symbols)"]
    result = run_agent_pass(
        lambda **_: _Res(titles, ["Split big."]),
        memory_file=tmp_path / "memory.md",
        source_label=SourceKind.MODULE_SIZE,
        service=service,
        settings=settings,
        max_drafts=0,
        repo_dir=repo,
    )
    assert result.drafts_created == []
    assert [t for t in service.list() if t.kind == TicketKind.EPIC] == []
    db.reset_engine()


def test_measure_refuses_paths_outside_repo(tmp_path):
    repo, _ = _repo(tmp_path)
    (tmp_path / "outside.py").write_text("x = 1\n", encoding="utf-8")
    assert module_size_findings.measure(repo, "../outside.py") is None
    assert module_size_findings.measure(repo, "src/pkg/missing.py") is None


def test_measurement_block_without_repo_reports_unknowns():
    block = module_size_findings.measurement_block(
        None, "module size: split src/x.py (1 lines, 1 symbols)", "", ""
    )
    assert "- **Path:** `src/x.py`" in block
    assert "not measured" in block
    assert "- **Commit:** `unknown`" in block
