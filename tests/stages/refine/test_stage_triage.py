"""Tests for the RefineStage auto-approve and refine-triage scenarios.

The stage fixtures, ticket helper, and mock seams are imported from
``test_stage`` (the parent module, which retains them); only the
auto-approve / refine-triage scenario tests live here.
"""

import json

import pytest

from robotsix_mill.agents import dedup, refining
from robotsix_mill.agents.refining import RefineResult
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.stages import StageContext
from robotsix_mill.stages import refine as refine_module
from robotsix_mill.stages.refine import RefineStage
from tests.stages.refine.test_stage import (
    _apply_default_mocks,
    _mock_auto_approve,
    _mock_dedup,
    _mock_refine_ok,
    _mock_refine_raises,
    _mock_triage_refine,
    _ticket,
)


@pytest.fixture
def ctx_factory(tmp_path, fake_sandbox):
    from robotsix_mill.config import Settings

    counter = [0]

    def make(**env):
        db.reset_engine()
        s = Settings(data_dir=str(tmp_path / f"data{counter[0]}"), **env)
        db.init_db(s, board_id="test-board")
        svc = TicketService(s, board_id="test-board")
        counter[0] += 1
        from robotsix_mill.config import RepoConfig

        return StageContext(
            settings=s,
            service=svc,
            repo_config=RepoConfig(
                repo_id="test-repo",
                board_id="test-board",
                langfuse_project_name="test",
                langfuse_public_key="pk-test",
                langfuse_secret_key="sk-test",
            ),
        )

    yield make
    db.reset_engine()


# ---------------------------------------------------------------------------
# 11. auto-approve: APPROVE → READY
# ---------------------------------------------------------------------------


def test_auto_approve_approve_routes_to_ready(ctx_factory, monkeypatch):
    ctx = ctx_factory(
        require_approval="true",
        refine_triage_enabled="false",
    )
    t = _ticket(ctx, body="Add a docstring to utils.py")

    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_ok(spec_markdown="## Problem\nAdd docstring"),
        triage_auto_approve=_mock_auto_approve(
            decision="APPROVE", reason="no design decisions"
        ),
    )

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert "auto-approve: APPROVE" in out.note


# ---------------------------------------------------------------------------
# 11b. auto-approve: test-gap source short-circuits to READY without LLM
# ---------------------------------------------------------------------------


def test_auto_approve_test_gap_source_short_circuits_to_ready(
    ctx_factory,
    monkeypatch,
):
    """test_gap-sourced tickets must auto-approve deterministically and
    must NOT invoke the LLM triage. Test-gap tickets only add coverage
    so there's no design risk a human reviewer can meaningfully veto;
    three triage runs on 2026-05-28 all fell back to human and were
    rubber-stamped."""
    ctx = ctx_factory(
        require_approval="true",
        refine_triage_enabled="false",
    )
    t = ctx.service.create(
        "Add unit tests for foo.py",
        "Add unit tests for foo.py covering the bar branch — substantive "
        "body padded past the trivial-draft threshold so refine actually "
        "runs the auto-approve gate.",
        source="test_gap",
    )

    triage_calls: list = []

    def fail_if_called(**_):
        triage_calls.append(True)
        raise AssertionError("triage_auto_approve must not be called for test_gap")

    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_ok(spec_markdown="## Problem\nAdd tests"),
        triage_auto_approve=fail_if_called,
    )

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert "auto-approve: APPROVE" in out.note
    assert "test_gap" in out.note


def test_auto_approve_audit_source_also_short_circuits(
    ctx_factory,
    monkeypatch,
):
    """Round 3: audit, agent_check, bc_check, completeness_check,
    module_curator, copy_paste join test_gap as deterministic
    auto-approve sources. These are mill-internal periodic agents
    whose drafts are dead-code / prompt / config / docstring
    cleanups — historically every one was rubber-stamped without
    rejection, so the LLM triage was pure toil."""
    for source in (
        "audit",
        "agent_check",
        "bc_check",
        "completeness_check",
        "module_curator",
        "copy_paste",
    ):
        ctx = ctx_factory(
            require_approval="true",
            refine_triage_enabled="false",
        )
        t = ctx.service.create(
            f"{source} proposal",
            "Substantive ticket body padded above the trivial-draft "
            "threshold so refine actually exercises the auto-approve "
            "gate against the source-based rule.",
            source=source,
        )
        # Under the collapsed post-refine check there is no separate
        # auto-approve LLM call to suppress — the combined call runs for
        # spec conciseness regardless of source.  What must hold is the
        # DECISION: the deterministic source rule routes to READY even
        # when the auto-approve verdict says NEEDS_APPROVAL.
        _apply_default_mocks(
            monkeypatch,
            run_refine_agent=_mock_refine_ok(
                spec_markdown="## Problem\nDo a thing",
            ),
            triage_auto_approve=_mock_auto_approve(
                decision="NEEDS_APPROVAL", reason="should be overridden"
            ),
        )

        out = RefineStage().run(t, ctx)
        assert out.next_state is State.READY, (
            f"{source}: expected READY, got {out.next_state}"
        )
        assert source in (out.note or ""), out.note


# ---------------------------------------------------------------------------
# 12. auto-approve: NEEDS_APPROVAL → HUMAN_ISSUE_APPROVAL
# ---------------------------------------------------------------------------


def test_auto_approve_needs_approval_routes_to_human(ctx_factory, monkeypatch):
    ctx = ctx_factory(
        require_approval="true",
        refine_triage_enabled="false",
    )
    t = _ticket(ctx, body="Redesign the auth module")

    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_ok(spec_markdown="## Problem\nRedesign auth"),
        triage_auto_approve=_mock_auto_approve(
            decision="NEEDS_APPROVAL", reason="new API design"
        ),
    )

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.HUMAN_ISSUE_APPROVAL
    assert "auto-approve: NEEDS_APPROVAL" in out.note


# ---------------------------------------------------------------------------
# 13. auto-approve triage failure → fallback to human
# ---------------------------------------------------------------------------


def test_auto_approve_triage_failure_falls_back_to_human(ctx_factory, monkeypatch):
    ctx = ctx_factory(
        require_approval="true",
        refine_triage_enabled="false",
    )
    t = _ticket(ctx, body="Update config defaults")

    _apply_default_mocks(
        monkeypatch,
        run_refine_agent=_mock_refine_ok(spec_markdown="## Problem\nUpdate config"),
        triage_auto_approve=lambda *a, **k: (_ for _ in ()).throw(
            Exception("LLM timeout")
        ),
    )

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.HUMAN_ISSUE_APPROVAL
    assert "triage failed" in out.note


# ---------------------------------------------------------------------------
# 14. refine triage SKIP → bypasses agent
# ---------------------------------------------------------------------------


def test_refine_triage_skip_bypasses_agent(ctx_factory, monkeypatch):
    """When triage returns SKIP and the draft contains backtick-quoted
    file paths, the refine agent is bypassed and those paths are written
    to file_map.json (fast path preserved)."""
    ctx = ctx_factory(require_approval="false", refine_triage_enabled="true")
    t = _ticket(ctx, body="Add docstring to foo() in `src/bar.py`")

    agent_called = []
    monkeypatch.setattr(
        refining, "run_refine_agent", lambda *a, **k: agent_called.append(1)
    )
    monkeypatch.setattr(
        refining,
        "triage_refine",
        _mock_triage_refine(decision="SKIP", reason="already precise"),
    )
    monkeypatch.setattr(
        dedup,
        "run_dedup_check",
        _mock_dedup(duplicate_of=None, already_done=None, reason="no match"),
    )
    monkeypatch.setattr(
        refine_module, "load_memory", lambda memory_file, max_chars=None: ""
    )
    monkeypatch.setattr(refine_module, "persist_memory", lambda memory_file, text: None)

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    assert len(agent_called) == 0
    assert "triage SKIP" in out.note
    ws = ctx.service.workspace(t)
    assert (ws.artifacts_dir / "draft-original.md").exists()
    # Fast path: file_map.json was written from extracted paths.
    file_map_path = ws.artifacts_dir / "file_map.json"
    assert file_map_path.exists()
    file_map = json.loads(file_map_path.read_text(encoding="utf-8"))
    assert len(file_map) == 1
    assert file_map[0]["file"] == "src/bar.py"
    assert file_map[0]["note"] == "from draft"


# ---------------------------------------------------------------------------
# 14b. refine triage SKIP + no paths → falls through to refine agent
# ---------------------------------------------------------------------------


def test_refine_triage_skip_no_paths_writes_empty_file_map(ctx_factory, monkeypatch):
    """When triage returns SKIP but the draft has no backtick-quoted
    file paths (e.g. top-level config like pyproject.toml with no '/'
    directory separator), write an empty file_map.json ([]) and return
    the SKIP Outcome — do NOT fall through to the expensive refine agent."""
    ctx = ctx_factory(require_approval="false", refine_triage_enabled="true")
    # Draft with no backtick-quoted paths (bare filename with no
    # directory separator won't match the regex).
    t = _ticket(ctx, body="Add docstring to foo() in bar.py")

    refine_called = []
    monkeypatch.setattr(
        refining,
        "run_refine_agent",
        lambda *a, **k: (refine_called.append(1), None)[1],
    )
    from robotsix_mill.agents import pre_refine_classifier
    from robotsix_mill.agents.pre_refine_classifier import PreRefineClassifierResult

    monkeypatch.setattr(
        pre_refine_classifier,
        "run_pre_refine_classifier",
        lambda **kw: PreRefineClassifierResult(
            triage_decision="SKIP", triage_reason="already precise"
        ),
    )
    monkeypatch.setattr(
        refine_module, "load_memory", lambda memory_file, max_chars=None: ""
    )
    monkeypatch.setattr(refine_module, "persist_memory", lambda memory_file, text: None)

    out = RefineStage().run(t, ctx)

    # Refine agent was NOT called — bypassed entirely.
    assert len(refine_called) == 0
    assert out.next_state is State.READY
    assert "triage SKIP" in out.note
    ws = ctx.service.workspace(t)
    # Empty file_map.json was written by the SKIP fallthrough.
    file_map_path = ws.artifacts_dir / "file_map.json"
    assert file_map_path.exists()
    file_map = json.loads(file_map_path.read_text(encoding="utf-8"))
    assert file_map == []


# ---------------------------------------------------------------------------
# 15. refine triage exception → fall through to full refine
# ---------------------------------------------------------------------------


def test_refine_triage_exception_falls_through_to_refine(ctx_factory, monkeypatch):
    ctx = ctx_factory(require_approval="false", refine_triage_enabled="true")
    t = _ticket(ctx, body="Fix the thing")

    refine_called = []
    monkeypatch.setattr(
        refining,
        "triage_refine",
        lambda *a, **k: (_ for _ in ()).throw(Exception("timeout")),
    )
    monkeypatch.setattr(
        dedup,
        "run_dedup_check",
        _mock_dedup(duplicate_of=None, already_done=None, reason="no match"),
    )

    def _refine(*a, **k):
        refine_called.append(1)
        return RefineResult(spec_markdown="## Problem\nDone")

    monkeypatch.setattr(refining, "run_refine_agent", _refine)
    monkeypatch.setattr(
        refine_module, "load_memory", lambda memory_file, max_chars=None: ""
    )
    monkeypatch.setattr(refine_module, "persist_memory", lambda memory_file, text: None)

    out = RefineStage().run(t, ctx)

    # The classifier failing must not park the raw draft at the human gate;
    # the full refine pass owns the analysis.
    assert out.next_state is not State.HUMAN_ISSUE_APPROVAL
    assert len(refine_called) == 1


# ---------------------------------------------------------------------------
# 16. refine agent RuntimeError → BLOCKED
# ---------------------------------------------------------------------------


def test_refine_agent_runtime_error_blocks(ctx_factory, monkeypatch):
    ctx = ctx_factory(refine_triage_enabled="false")
    t = _ticket(ctx, body="Fix the thing")

    monkeypatch.setattr(
        dedup,
        "run_dedup_check",
        _mock_dedup(duplicate_of=None, already_done=None, reason="no match"),
    )
    monkeypatch.setattr(
        refining,
        "run_refine_agent",
        _mock_refine_raises(RuntimeError("OPENROUTER_API_KEY is not set")),
    )
    monkeypatch.setattr(
        refine_module, "load_memory", lambda memory_file, max_chars=None: ""
    )
    monkeypatch.setattr(refine_module, "persist_memory", lambda memory_file, text: None)

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "OPENROUTER_API_KEY" in out.note
