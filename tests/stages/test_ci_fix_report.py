"""ci_fix CI-failure reports: dedup by (repo, workflow, run id), route by cause."""

import pytest

from robotsix_mill.agents.report_issue import make_report_issue_tool
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.stages import ci_fix_report
from robotsix_mill.stages.ci_fix_report import (
    ROUTE_GITHUB_WORKFLOWS,
    ROUTE_INFRA,
    ROUTE_REPO,
    parse_ci_report,
    route_ci_report,
    scrub_pipeline_vocabulary,
)

_RUN_URL = "https://github.com/o/invest/actions/runs/18012345678"


@pytest.fixture(autouse=True)
def _gw_board(monkeypatch):
    monkeypatch.setattr(ci_fix_report, "_github_workflows_board_id", lambda: "gw-board")


def _tool(settings):
    return make_report_issue_tool(settings, agent_name="ci_fix", board_id="invest")


def test_six_reports_same_run_file_one_ticket_and_five_comments(settings):
    tool = _tool(settings)
    outs = [
        tool(
            f"CI + Lint Workflows startup_failure, pre-existing (take {i})",
            f"The ci_fix agent saw run {_RUN_URL} conclude startup_failure "
            f"for .github/workflows/ci.yml; not caused by the pin bump. #{i}",
            "error",
        )
        for i in range(6)
    ]
    assert outs[0].startswith("report_issue: filed draft ")
    assert all("appended to existing report" in o for o in outs[1:])

    svc = TicketService(settings, board_id="invest")
    tickets = svc.list()
    assert len(tickets) == 1
    assert len(svc.list_comments(tickets[0].id)) == 5


def test_reusable_workflow_startup_failure_lands_on_github_workflows_board(
    settings,
):
    out = _tool(settings)(
        "Lint workflow startup_failure",
        "Run 18099999999 failed to start: `uses: damien-robotsix/"
        "robotsix-github-workflows/.github/workflows/python-ci.yml@abc` — "
        "the implement agent did not cause it.",
        "error",
    )
    assert "on board gw-board" in out
    assert TicketService(settings, board_id="invest").list() == []
    gw = TicketService(settings, board_id="gw-board")
    [t] = gw.list()
    desc = gw.workspace(t).read_description()
    assert "python-ci.yml" in desc
    assert "ci_fix" not in desc.lower() and "implement agent" not in desc
    assert "ci_fix" not in t.title.lower()


def test_github_side_failure_is_not_filed(settings):
    out = _tool(settings)(
        "CI startup_failure",
        f"Run {_RUN_URL}: no check-run registered; billing issue.",
        "error",
    )
    assert out.startswith("report_issue: not filed")
    assert TicketService(settings, board_id="invest").list() == []


def test_non_ci_report_falls_through_to_generic_path(settings):
    out = _tool(settings)("sandbox lacks node", "details", "missing-tool")
    assert out.startswith("report_issue: filed draft ")
    assert "on board" not in out


def test_different_run_ids_file_separate_tickets(settings):
    tool = _tool(settings)
    tool("Docs failed", "run 18000000001 failed in docs build", "error")
    tool("Docs failed again", "run 18000000002 failed in docs build", "error")
    assert len(TicketService(settings, board_id="invest").list()) == 2


def test_parse_and_route():
    r = parse_ci_report("invest", f"see {_RUN_URL} startup_failure")
    assert r is not None
    assert r.run_ids == {"18012345678"} and r.startup_failure
    assert route_ci_report(r, "") == ROUTE_REPO
    assert route_ci_report(r, "spending limit needs to be increased") == ROUTE_INFRA
    gw = parse_ci_report(
        "invest",
        "startup_failure in damien-robotsix/robotsix-github-workflows/"
        ".github/workflows/lint.yml@v1",
    )
    assert gw is not None and gw.reusable_workflow == "lint.yml"
    assert route_ci_report(gw, "no check-run") == ROUTE_GITHUB_WORKFLOWS
    assert parse_ci_report("invest", "nothing about CI runs") is None


def test_scrub_pipeline_vocabulary():
    out = scrub_pipeline_vocabulary("The ci_fix stage and implement agent")
    assert "ci_fix" not in out and "implement agent" not in out


def test_status_note_appends_history_note_and_files_no_ticket(settings, monkeypatch):
    """robotsix-modules 2026-09-04: 'Agent report: … pushed' and 'DONE: …'
    progress notes must land on the ticket being fixed, not as tickets."""
    svc = TicketService(settings, board_id="invest")
    parent = svc.create("CI red on main", "body")
    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.current_session",
        lambda: f"invest · {parent.id}",
    )
    tool = _tool(settings)
    a = tool("Agent report: gitleaks CI fix pushed", "commit 5019a93")
    b = tool("DONE: gitleaks CI fix pushed", "commit 5019a93")
    assert "status note appended to" in a
    assert "status note appended to" in b
    assert [t.id for t in svc.list()] == [parent.id]
    notes = [e.note or "" for e in svc.history(parent.id)]
    assert any("gitleaks CI fix pushed" in n and "5019a93" in n for n in notes)


def test_status_note_without_parent_files_nothing(settings):
    out = _tool(settings)("DONE: lint fixed")
    assert "not filed" in out
    assert TicketService(settings, board_id="invest").list() == []


def test_identical_followups_within_window_file_one_ticket(settings, monkeypatch):
    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.current_session", lambda: "sess-1"
    )
    tool = _tool(settings)
    a = tool("Agent report: gitleaks config needs an allowlist entry", "x")
    svc = TicketService(settings, board_id="invest")
    # Even once the first is closed, a repeat within the window is suppressed.
    svc.transition(svc.list()[0].id, State.CLOSED)
    b = tool("gitleaks config needs an allowlist entry", "x")
    assert a.startswith("report_issue: filed draft ")
    assert "already filed as" in b
    assert len(svc.list()) == 1


@pytest.mark.parametrize(
    "title",
    [
        "lint still not fixed after pin bump",
        "CI red: pushed commit breaks docs build",
        "startup_failure not resolved by retry",
    ],
)
def test_issue_titles_mentioning_status_words_are_not_status_notes(title):
    assert not ci_fix_report.is_status_note(title)


def test_issue_title_with_status_word_is_still_filed(settings):
    out = _tool(settings)("lint still not fixed after pin bump", "details")
    assert out.startswith("report_issue: filed draft ")
    assert len(TicketService(settings, board_id="invest").list()) == 1


def test_followup_dedup_skipped_without_origin_session(settings):
    svc = TicketService(settings, board_id="invest")
    svc.create("gitleaks config needs an allowlist entry", "human-filed")
    assert (
        ci_fix_report.find_recent_followup(
            svc, "gitleaks config needs an allowlist entry", None
        )
        is None
    )


def test_normalise_report_title():
    assert ci_fix_report.normalise_report_title(
        "Agent report: Gitleaks CI fix pushed"
    ) == ci_fix_report.normalise_report_title("DONE:  gitleaks CI fix pushed.")
