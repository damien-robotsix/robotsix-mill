"""ci_fix CI-failure reports: dedup by (repo, workflow, run id), route by cause."""

import pytest

from robotsix_mill.agents.report_issue import make_report_issue_tool
from robotsix_mill.core.service import TicketService
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
