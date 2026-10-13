"""Tests for ``runners.ci_auto_close_runner``.

Real :class:`TicketService` on a ``tmp_path`` SQLite DB; forge is mocked.
"""

from __future__ import annotations

import robotsix_mill.config as _cfg
from robotsix_mill.agents.runners import ci_auto_close_runner as car
from robotsix_mill.config import (
    RepoConfig,
    ReposRegistry,
    Settings,
    _reset_repos_config,
)
from robotsix_mill.core import db
from robotsix_mill.core.models import SourceKind
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State

_BOARD = "test-board"


class _FakeForge:
    def __init__(self, runs, prs=None, runs_by_id=None, triggers=None, dispatched=None):
        self._runs = runs
        # branch -> ``pr_status`` dict; mirrors the forge answer the API's
        # ``pr_url`` enrichment is built on (the Ticket row stores no url).
        self._prs = prs if prs is not None else {}
        # run_id -> single-run dict, served by ``get_workflow_run`` (the
        # run-URL anchor fallback for refined tickets).
        self._runs_by_id = runs_by_id if runs_by_id is not None else {}
        # ``on:`` trigger event names returned for any workflow file, or None.
        self._triggers = triggers
        # Shared record of (workflow_file, ref) dispatches across instances.
        self._dispatched = dispatched if dispatched is not None else []

    def list_workflow_runs(self, *, branch=None, head_sha=None):
        return self._runs

    def pr_status(self, *, source_branch):
        return self._prs.get(source_branch)

    def get_workflow_run(self, *, run_id):
        return self._runs_by_id.get(run_id)

    def get_workflow_triggers(self, *, path):
        return self._triggers

    def dispatch_workflow(self, *, workflow_file, ref="main"):
        self._dispatched.append((workflow_file, ref))
        return {"dispatched": True}


def _prepare(
    tmp_path,
    monkeypatch,
    runs=None,
    prs=None,
    runs_by_id=None,
    triggers=None,
    dispatched=None,
):
    # One shared ``prs`` dict across every forge instance the lambda builds,
    # so ``_ci_ticket(pr_url=...)`` can register a PR after ``_prepare``.
    prs = {} if prs is None else prs
    runs_by_id = {} if runs_by_id is None else runs_by_id
    # Shared across every forge instance so dispatches recorded by the runner
    # survive the per-call ``get_forge`` lambda (mirrors the ``prs`` pattern).
    dispatched = [] if dispatched is None else dispatched
    db.reset_engine()
    settings = Settings(data_dir=str(tmp_path / "data"), require_approval="false")
    db.init_db(settings, board_id=_BOARD)
    _reset_repos_config()
    rc = RepoConfig(
        repo_id="test-repo",
        board_id=_BOARD,
        langfuse_project_name="t",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
    )
    _cfg._repos_config = ReposRegistry(repos={rc.repo_id: rc})
    monkeypatch.setattr(
        car,
        "get_forge",
        lambda settings, repo_config=None: _FakeForge(
            runs or [], prs, runs_by_id, triggers, dispatched
        ),
    )
    return settings, TicketService(settings, board_id=_BOARD)


def _ci_ticket(
    service,
    settings,
    wf="ci / tests",
    branch="main",
    f_sha="abc123",
    run_id=100,
    state=State.DRAFT,
    has_branch=False,
    pr_url=None,
):
    """Create a ci ticket; *has_branch* writes ``row.branch`` the way implement
    does.  *pr_url* registers an OPEN PR for that branch on the fake forge
    (the runner learns about PRs via ``forge.pr_status``, never a row field).
    """
    body = (
        f"**Workflow:** {wf}\n**Path:** .github/workflows/ci.yml\n"
        f"**Branch:** {branch}\n**Run:** [{run_id}](https://x/run/{run_id})\n"
        f"**Commit:** `{f_sha}`\n**Created:** 2026-09-01T00:00:00Z\n\nlogs..."
    )
    t = service.create(
        title=f"CI failure: {wf} on {branch}",
        description=body,
        source=SourceKind.CI,
    )
    if has_branch:
        from robotsix_mill.core import db as _db
        from robotsix_mill.core.models import Ticket

        with _db.session(settings, _BOARD) as s:
            row = s.get(Ticket, t.id)
            row.branch = "ci-fix-1"
            s.add(row)
            s.commit()
    if pr_url:
        forge = car.get_forge(settings, repo_config=None)
        forge._prs["ci-fix-1"] = {
            "merged": False,
            "state": "open",
            "url": pr_url,
            "mergeable": True,
            "author": "bot",
        }
    if state is State.DRAFT:
        return t
    if state is State.HUMAN_ISSUE_APPROVAL:
        service.transition(t.id, State.HUMAN_ISSUE_APPROVAL)
    elif state is State.BLOCKED:
        service.transition(t.id, State.READY)
        service.transition(t.id, State.BLOCKED, note="ci fix failed")
    return service.get(t.id)


def _run(settings):
    return car.run_ci_auto_close(settings)


def test_red_then_fixed_two_consecutive_greens_closes(tmp_path, monkeypatch):
    settings, service = _prepare(
        tmp_path,
        monkeypatch,
        runs=[
            {
                "name": "ci / tests",
                "head_sha": "cafe01",
                "conclusion": "success",
                "id": 300,
                "html_url": "https://x/run/300",
                "created_at": "2026-09-02T03:00:00Z",
            },
            {
                "name": "ci / tests",
                "head_sha": "dead02",
                "conclusion": "success",
                "id": 200,
                "html_url": "https://x/run/200",
                "created_at": "2026-09-02T02:00:00Z",
            },
            {
                "name": "ci / tests",
                "head_sha": "abc123",
                "conclusion": "failure",
                "id": 100,
                "html_url": "https://x/run/100",
                "created_at": "2026-09-01T00:00:00Z",
            },
        ],
    )
    t = _ci_ticket(service, settings, f_sha="abc123", state=State.DRAFT)

    result = _run(settings)

    assert result["closed"] == 1
    fresh = service.get(t.id)
    assert fresh.state is State.DONE
    events = service.history(t.id)
    done = [e for e in events if e.state is State.DONE]
    assert done and "https://x/run/300" in done[-1].note


def test_single_green_on_new_head_fix_identifiable_closes(tmp_path, monkeypatch):
    settings, service = _prepare(
        tmp_path,
        monkeypatch,
        runs=[
            {
                "name": "ci / tests",
                "head_sha": "dead02",
                "conclusion": "success",
                "id": 200,
                "html_url": "https://x/run/200",
                "created_at": "2026-09-02T02:00:00Z",
            },
            {
                "name": "ci / tests",
                "head_sha": "abc123",
                "conclusion": "failure",
                "id": 100,
                "html_url": "https://x/run/100",
                "created_at": "2026-09-01T00:00:00Z",
            },
        ],
    )
    t = _ci_ticket(service, settings, f_sha="abc123")

    result = _run(settings)

    assert result["closed"] == 1
    assert service.get(t.id).state is State.DONE


def test_still_red_not_closed(tmp_path, monkeypatch):
    settings, service = _prepare(
        tmp_path,
        monkeypatch,
        runs=[
            {
                "name": "ci / tests",
                "head_sha": "dead02",
                "conclusion": "failure",
                "id": 200,
                "html_url": "https://x/run/200",
                "created_at": "2026-09-02T02:00:00Z",
            },
            {
                "name": "ci / tests",
                "head_sha": "abc123",
                "conclusion": "failure",
                "id": 100,
                "html_url": "https://x/run/100",
                "created_at": "2026-09-01T00:00:00Z",
            },
        ],
    )
    t = _ci_ticket(service, settings, f_sha="abc123")

    result = _run(settings)

    assert result["closed"] == 0
    assert service.get(t.id).state is State.DRAFT


def test_single_green_same_commit_not_closed(tmp_path, monkeypatch):
    # One green on the SAME failing commit — no fix commit identifiable → need 2.
    settings, service = _prepare(
        tmp_path,
        monkeypatch,
        runs=[
            {
                "name": "ci / tests",
                "head_sha": "abc123",
                "conclusion": "success",
                "id": 200,
                "html_url": "https://x/run/200",
                "created_at": "2026-09-02T02:00:00Z",
            },
        ],
    )
    t = _ci_ticket(service, settings, f_sha="abc123")

    result = _run(settings)

    assert result["closed"] == 0
    assert service.get(t.id).state is State.DRAFT


def test_blocked_ticket_closes_via_mark_done(tmp_path, monkeypatch):
    settings, service = _prepare(
        tmp_path,
        monkeypatch,
        runs=[
            {
                "name": "ci / tests",
                "head_sha": "dead02",
                "conclusion": "success",
                "id": 200,
                "html_url": "https://x/run/200",
                "created_at": "2026-09-02T02:00:00Z",
            },
            {
                "name": "ci / tests",
                "head_sha": "abc123",
                "conclusion": "failure",
                "id": 100,
                "html_url": "https://x/run/100",
                "created_at": "2026-09-01T00:00:00Z",
            },
        ],
    )
    t = _ci_ticket(service, settings, f_sha="abc123", state=State.BLOCKED)

    result = _run(settings)

    assert result["closed"] == 1
    assert service.get(t.id).state is State.DONE


def test_human_issue_approval_ticket_closes(tmp_path, monkeypatch):
    settings, service = _prepare(
        tmp_path,
        monkeypatch,
        runs=[
            {
                "name": "ci / tests",
                "head_sha": "dead02",
                "conclusion": "success",
                "id": 200,
                "html_url": "https://x/run/200",
                "created_at": "2026-09-02T02:00:00Z",
            },
            {
                "name": "ci / tests",
                "head_sha": "abc123",
                "conclusion": "failure",
                "id": 100,
                "html_url": "https://x/run/100",
                "created_at": "2026-09-01T00:00:00Z",
            },
        ],
    )
    t = _ci_ticket(service, settings, f_sha="abc123", state=State.HUMAN_ISSUE_APPROVAL)

    result = _run(settings)

    assert result["closed"] == 1
    assert service.get(t.id).state is State.DONE


def test_ticket_with_branch_and_open_pr_not_closed(tmp_path, monkeypatch):
    # A branch WITH an open PR is unmerged work in flight — keep normal flow.
    settings, service = _prepare(
        tmp_path,
        monkeypatch,
        runs=[
            {
                "name": "ci / tests",
                "head_sha": "dead02",
                "conclusion": "success",
                "id": 200,
                "html_url": "https://x/run/200",
                "created_at": "2026-09-02T02:00:00Z",
            },
            {
                "name": "ci / tests",
                "head_sha": "abc123",
                "conclusion": "failure",
                "id": 100,
                "html_url": "https://x/run/100",
                "created_at": "2026-09-01T00:00:00Z",
            },
        ],
    )
    t = _ci_ticket(
        service,
        settings,
        f_sha="abc123",
        has_branch=True,
        pr_url="https://x/pr/7",
    )

    result = _run(settings)

    assert result["closed"] == 0
    assert result["skipped"] == 1
    assert service.get(t.id).state is State.DRAFT


def test_blocked_ticket_with_branch_but_no_pr_closes(tmp_path, monkeypatch):
    # Incident 2026-09-07 (ef8e): implement created the branch, parked the
    # ticket BLOCKED, opened no PR; main green on a newer head.  The old
    # ``if ticket.branch`` guard skipped it every pass — it must be mooted.
    settings, service = _prepare(
        tmp_path,
        monkeypatch,
        runs=[
            {
                "name": "ci / tests",
                "head_sha": "dead02",
                "conclusion": "success",
                "id": 200,
                "html_url": "https://x/run/200",
                "created_at": "2026-09-02T02:00:00Z",
            },
            {
                "name": "ci / tests",
                "head_sha": "abc123",
                "conclusion": "failure",
                "id": 100,
                "html_url": "https://x/run/100",
                "created_at": "2026-09-01T00:00:00Z",
            },
        ],
    )
    t = _ci_ticket(
        service, settings, f_sha="abc123", state=State.BLOCKED, has_branch=True
    )
    assert service.get(t.id).branch == "ci-fix-1"

    result = _run(settings)

    assert result["closed"] == 1
    fresh = service.get(t.id)
    assert fresh.state is State.DONE
    done = [e for e in service.history(t.id) if e.state is State.DONE]
    assert done and "https://x/run/200" in done[-1].note
    assert "force-closed from blocked" in done[-1].note


def test_ticket_with_branch_and_closed_unmerged_pr_closes(tmp_path, monkeypatch):
    # A closed-unmerged PR is not work in flight — the ticket is mooted.
    settings, service = _prepare(
        tmp_path,
        monkeypatch,
        prs={
            "ci-fix-1": {
                "merged": False,
                "state": "closed",
                "url": "https://x/pr/8",
                "mergeable": None,
                "author": "bot",
            }
        },
        runs=[
            {
                "name": "ci / tests",
                "head_sha": "dead02",
                "conclusion": "success",
                "id": 200,
                "html_url": "https://x/run/200",
                "created_at": "2026-09-02T02:00:00Z",
            },
            {
                "name": "ci / tests",
                "head_sha": "abc123",
                "conclusion": "failure",
                "id": 100,
                "html_url": "https://x/run/100",
                "created_at": "2026-09-01T00:00:00Z",
            },
        ],
    )
    t = _ci_ticket(service, settings, f_sha="abc123", has_branch=True)

    result = _run(settings)

    assert result["closed"] == 1
    assert service.get(t.id).state is State.DONE


def test_pr_status_failure_skips_branched_ticket(tmp_path, monkeypatch):
    # Forge cannot say whether an MR exists — skip, never guess.
    settings, service = _prepare(
        tmp_path,
        monkeypatch,
        runs=[
            {
                "name": "ci / tests",
                "head_sha": "dead02",
                "conclusion": "success",
                "id": 200,
                "html_url": "https://x/run/200",
                "created_at": "2026-09-02T02:00:00Z",
            },
            {
                "name": "ci / tests",
                "head_sha": "abc123",
                "conclusion": "failure",
                "id": 100,
                "html_url": "https://x/run/100",
                "created_at": "2026-09-01T00:00:00Z",
            },
        ],
    )

    def _boom(self, *, source_branch):
        raise RuntimeError("forge down")

    monkeypatch.setattr(_FakeForge, "pr_status", _boom)
    t = _ci_ticket(service, settings, f_sha="abc123", has_branch=True)

    result = _run(settings)

    assert result["closed"] == 0
    assert service.get(t.id).state is State.DRAFT


def test_non_ci_ticket_not_touched(tmp_path, monkeypatch):
    settings, service = _prepare(
        tmp_path,
        monkeypatch,
        runs=[
            {
                "name": "ci / tests",
                "head_sha": "dead02",
                "conclusion": "success",
                "id": 200,
                "html_url": "https://x/run/200",
                "created_at": "2026-09-02T02:00:00Z",
            },
        ],
    )
    t = service.create(title="some task", description="body", source=SourceKind.USER)

    result = _run(settings)

    assert result["closed"] == 0
    assert service.get(t.id).state is State.DRAFT


def test_parse_workflow_branch_from_title_fallback():
    from types import SimpleNamespace

    ticket = SimpleNamespace(title="CI failure: ci / tests on main")
    wf, branch, source = car._parse_workflow_branch(ticket, "")
    assert wf == "ci / tests"
    assert branch == "main"
    assert source == "title"


def test_parse_workflow_branch_source_markers():
    from types import SimpleNamespace

    ticket = SimpleNamespace(title="anything")
    body = "**Workflow:** ci / tests\n**Branch:** main\n"
    wf, branch, source = car._parse_workflow_branch(ticket, body)
    assert (wf, branch, source) == ("ci / tests", "main", "markers")


def test_parse_workflow_branch_unparseable_source_none():
    from types import SimpleNamespace

    ticket = SimpleNamespace(title="Serialize release-please runs")
    wf, branch, source = car._parse_workflow_branch(ticket, "no markers here")
    assert source is None


def test_run_url_id_extracts_first_run():
    body = (
        "The weekly workflow fails on `main` ... (run "
        "[36392394152](https://github.com/damien-robotsix/robotsix-llmio/"
        "actions/runs/36392394152), commit `e31d639`)."
    )
    assert car._run_url_id(body) == 36392394152
    assert car._run_url_id("no url") is None


def test_run_url_anchor_closes_refined_llmio_ticket(tmp_path, monkeypatch):
    # robotsix-llmio ticket refined so both title and body markers are gone;
    # only the ``**Run:**`` markdown link with a backticked workflow name
    # survives.  The runner resolves the run URL to recover the anchors and
    # closes once main is green on that workflow.
    settings, service = _prepare(
        tmp_path,
        monkeypatch,
        runs=[
            {
                "name": "Refresh git dependencies",
                "head_sha": "feed01",
                "conclusion": "success",
                "id": 36400000000,
                "html_url": "https://x/run/36400000000",
                "created_at": "2026-09-29T00:00:00Z",
            },
            {
                "name": "Refresh git dependencies",
                "head_sha": "e31d639",
                "conclusion": "failure",
                "id": 36392394152,
                "html_url": "https://x/run/36392394152",
                "created_at": "2026-09-28T07:39:11Z",
            },
        ],
        runs_by_id={
            36392394152: {
                "id": 36392394152,
                "name": "Refresh git dependencies",
                "path": ".github/workflows/bump.yml",
                "head_sha": "e31d639",
                "head_branch": "main",
                "conclusion": "failure",
                "run_attempt": 1,
                "html_url": "https://x/run/36392394152",
                "created_at": "2026-09-28T07:39:11Z",
            }
        },
    )
    body = (
        "The weekly `Refresh git dependencies` workflow fails on `main` ... "
        "(run [36392394152](https://github.com/damien-robotsix/robotsix-llmio/"
        "actions/runs/36392394152), commit `e31d639`)."
    )
    t = service.create(
        title=(
            "CI failure: refresh-git-deps — re-pin shared bump.yml caller once "
            "upstream bump-setup metadata fix propagates"
        ),
        description=body,
        source=SourceKind.CI,
    )

    result = _run(settings)

    assert result["closed"] == 1
    assert service.get(t.id).state is State.DONE


def test_run_url_anchor_still_red_not_closed_hexarchy(tmp_path, monkeypatch):
    # hexarchy ticket refined into a feature ticket; body keeps the run URL.
    # The workflow is still red on main → not closed, and the skip reason is
    # a real green-check reason (not ``unparseable workflow/branch``).
    settings, service = _prepare(
        tmp_path,
        monkeypatch,
        runs=[
            {
                "name": "release-please",
                "head_sha": "beef99",
                "conclusion": "failure",
                "id": 35433604318,
                "html_url": "https://x/run/35433604318",
                "created_at": "2026-09-19T09:17:15Z",
            },
        ],
        runs_by_id={
            35433604318: {
                "id": 35433604318,
                "name": "release-please",
                "path": ".github/workflows/release-please.yml",
                "head_sha": "beef99",
                "head_branch": "main",
                "conclusion": "failure",
                "run_attempt": 1,
                "html_url": "https://x/run/35433604318",
                "created_at": "2026-09-19T09:17:15Z",
            }
        },
    )
    body = (
        "Serialize release-please runs so concurrent merges stop clobbering "
        "the release PR. Seen in run "
        "[35433604318](https://github.com/damien-robotsix/hexarchy/actions/"
        "runs/35433604318)."
    )
    t = service.create(
        title="Serialize release-please runs to avoid race",
        description=body,
        source=SourceKind.CI,
    )

    result = _run(settings)

    assert result["closed"] == 0
    assert service.get(t.id).state is State.DRAFT


def test_rerun_success_same_head_closes(tmp_path, monkeypatch):
    # A transient failure fixed by RE-RUNNING the same run: attempt 1 failed,
    # attempt 2 (same run id, same head) succeeded.  One green on the failing
    # head with ``run_attempt > 1`` is an identifiable fix → close, and the
    # note names the re-run.
    settings, service = _prepare(
        tmp_path,
        monkeypatch,
        runs=[
            {
                "name": "ci / tests",
                "head_sha": "abc123",
                "conclusion": "success",
                "run_attempt": 2,
                "id": 100,
                "html_url": "https://x/run/100",
                "created_at": "2026-09-01T02:00:00Z",
            },
            {
                "name": "ci / tests",
                "head_sha": "abc123",
                "conclusion": "failure",
                "run_attempt": 1,
                "id": 100,
                "html_url": "https://x/run/100",
                "created_at": "2026-09-01T00:00:00Z",
            },
        ],
    )
    t = _ci_ticket(service, settings, f_sha="abc123")

    result = _run(settings)

    assert result["closed"] == 1
    fresh = service.get(t.id)
    assert fresh.state is State.DONE
    done = [e for e in service.history(t.id) if e.state is State.DONE]
    assert done and "re-run attempt 2" in done[-1].note


def test_lone_first_attempt_green_same_head_logs_and_skips(
    tmp_path, monkeypatch, caplog
):
    # A lone FIRST-attempt green on the same failing head must NOT close, and
    # the skip must emit one INFO line naming the reason.
    settings, service = _prepare(
        tmp_path,
        monkeypatch,
        runs=[
            {
                "name": "ci / tests",
                "head_sha": "abc123",
                "conclusion": "success",
                "run_attempt": 1,
                "id": 200,
                "html_url": "https://x/run/200",
                "created_at": "2026-09-02T02:00:00Z",
            },
        ],
    )
    t = _ci_ticket(service, settings, f_sha="abc123")

    import logging

    with caplog.at_level(logging.INFO, logger="robotsix_mill.ci_auto_close"):
        result = _run(settings)

    assert result["closed"] == 0
    assert service.get(t.id).state is State.DRAFT
    skip_lines = [
        r.getMessage()
        for r in caplog.records
        if "lone green on failing head, attempt 1" in r.getMessage()
    ]
    assert len(skip_lines) == 1
    assert t.id in skip_lines[0]


# --- failing run aged out of the forge's run page (live mill 2026-09-09:
# --- Security Audit failed 03:27Z on 88a837f, the page held only newer runs
# --- by the next pass, main green on that workflow since 04:18Z, ticket
# --- skipped every 15 min as "failing head outside run window") ---


def test_failing_run_aged_out_of_window_closes_on_newer_green(tmp_path, monkeypatch):
    settings, service = _prepare(
        tmp_path,
        monkeypatch,
        runs=[
            {
                "name": "ci / tests",
                "head_sha": "e95be55",
                "conclusion": "success",
                "id": 300,
                "html_url": "https://x/run/300",
                "created_at": "2026-09-01T04:18:00Z",
            },
            {
                "name": "ci / tests",
                "head_sha": "d3b7e4c",
                "conclusion": "success",
                "id": 200,
                "html_url": "https://x/run/200",
                "created_at": "2026-09-01T03:45:00Z",
            },
            # the failing run (abc123 @ 00:00:00Z, per the ticket body) is
            # NOT in the page any more
        ],
    )
    t = _ci_ticket(service, settings, f_sha="abc123")

    result = _run(settings)

    assert result["closed"] == 1
    done = service.get(t.id)
    assert done.state is State.DONE
    notes = " ".join(e.note or "" for e in service.history(t.id))
    assert "https://x/run/300" in notes


def test_failing_run_aged_out_but_newest_red_stays(tmp_path, monkeypatch):
    settings, service = _prepare(
        tmp_path,
        monkeypatch,
        runs=[
            {
                "name": "ci / tests",
                "head_sha": "e95be55",
                "conclusion": "failure",
                "id": 300,
                "html_url": "https://x/run/300",
                "created_at": "2026-09-01T04:18:00Z",
            },
            {
                "name": "ci / tests",
                "head_sha": "d3b7e4c",
                "conclusion": "success",
                "id": 200,
                "html_url": "https://x/run/200",
                "created_at": "2026-09-01T03:45:00Z",
            },
        ],
    )
    t = _ci_ticket(service, settings, f_sha="abc123")

    result = _run(settings)

    assert result["closed"] == 0
    assert service.get(t.id).state is State.DRAFT


def test_window_older_than_failure_still_unanchored():
    """A page whose runs PRE-date the failure proves nothing — keep skipping."""
    runs = [
        {
            "name": "ci / tests",
            "head_sha": "old1",
            "conclusion": "success",
            "id": 50,
            "created_at": "2026-08-31T00:00:00Z",
        }
    ]
    green, reason = car._green_since_failure(
        runs, "ci / tests", "abc123", failing_created_at="2026-09-01T00:00:00Z"
    )
    assert green is None
    assert reason == "failing head outside run window"
    # and without a Created marker the behaviour is unchanged
    green, reason = car._green_since_failure(runs, "ci / tests", "abc123")
    assert (green, reason) == (None, "failing head outside run window")


# ---------------------------------------------------------------------------
# cron-only workflow dispatch after a fix merges
# ---------------------------------------------------------------------------

_CRON = ["schedule", "workflow_dispatch"]


def _stale_red_run(f_sha="abc123"):
    """A single red run whose head is the failing commit — the stale cron run
    that lingers after a fix merged (no push trigger re-runs the workflow)."""
    return [
        {
            "name": "Refresh git dependencies",
            "head_sha": f_sha,
            "conclusion": "failure",
            "id": 100,
            "html_url": "https://x/run/100",
            "created_at": "2026-09-01T00:00:00Z",
            "path": ".github/workflows/refresh-deps.yml",
        }
    ]


def test_is_cron_only():
    assert car._is_cron_only(["schedule", "workflow_dispatch"]) is True
    assert car._is_cron_only(["workflow_dispatch", "schedule", "push"]) is False
    assert car._is_cron_only(["schedule"]) is False  # no manual dispatch
    assert car._is_cron_only(["workflow_dispatch"]) is False  # no schedule
    assert car._is_cron_only(["schedule", "workflow_dispatch", "pull_request"]) is False
    assert car._is_cron_only(None) is False
    assert car._is_cron_only([]) is False


def test_cron_only_still_red_dispatches(tmp_path, monkeypatch):
    dispatched = []
    settings, service = _prepare(
        tmp_path,
        monkeypatch,
        runs=_stale_red_run(),
        triggers=_CRON,
        dispatched=dispatched,
    )
    t = _ci_ticket(service, settings, wf="Refresh git dependencies", f_sha="abc123")

    result = _run(settings)

    # Not closed — still red; the pass re-triggers the workflow instead.
    assert result["closed"] == 0
    assert dispatched == [("refresh-deps.yml", "main")]
    notes = [e.note or "" for e in service.history(t.id)]
    assert any(car._DISPATCH_NOTE in n for n in notes)
    assert any("refresh-deps.yml" in n and "on main" in n for n in notes)


def test_cron_only_dispatched_at_most_once(tmp_path, monkeypatch):
    dispatched = []
    settings, service = _prepare(
        tmp_path,
        monkeypatch,
        runs=_stale_red_run(),
        triggers=_CRON,
        dispatched=dispatched,
    )
    t = _ci_ticket(service, settings, wf="Refresh git dependencies")

    _run(settings)
    _run(settings)  # second pass must see the history marker and skip

    assert dispatched == [("refresh-deps.yml", "main")]
    marker_events = [
        e for e in service.history(t.id) if car._DISPATCH_NOTE in (e.note or "")
    ]
    assert len(marker_events) == 1


def test_push_triggered_workflow_not_dispatched(tmp_path, monkeypatch):
    dispatched = []
    settings, service = _prepare(
        tmp_path,
        monkeypatch,
        runs=_stale_red_run(),
        triggers=["push", "schedule", "workflow_dispatch"],
        dispatched=dispatched,
    )
    _ci_ticket(service, settings, wf="Refresh git dependencies")

    _run(settings)

    assert dispatched == []


def test_newer_run_than_failure_not_dispatched(tmp_path, monkeypatch):
    """A run newer than the failing commit already exists → no dispatch even
    for a cron-only workflow (a fresh run is already on its way)."""
    dispatched = []
    runs = [
        {
            "name": "Refresh git dependencies",
            "head_sha": "newhead",
            "conclusion": "failure",
            "id": 200,
            "html_url": "https://x/run/200",
            "created_at": "2026-09-02T00:00:00Z",
            "path": ".github/workflows/refresh-deps.yml",
        },
        _stale_red_run()[0],
    ]
    settings, service = _prepare(
        tmp_path, monkeypatch, runs=runs, triggers=_CRON, dispatched=dispatched
    )
    _ci_ticket(service, settings, wf="Refresh git dependencies", f_sha="abc123")

    _run(settings)

    assert dispatched == []
