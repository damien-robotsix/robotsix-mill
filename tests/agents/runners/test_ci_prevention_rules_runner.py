"""Tests for the ci_prevention_rules runner: in-place ledger section rewrite,
per-bucket digest, and the pass end-to-end with a faked agent."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from robotsix_mill.agents.ci_prevention_rules import CiPreventionRulesResult
from robotsix_mill.agents.runners import ci_prevention_rules_runner as cpr
from robotsix_mill.agents.runners.diagnostic_events import (
    DiagnosticEvent,
    emit_diagnostic_event,
)
from robotsix_mill.agents.runners.pass_runner import load_memory
from robotsix_mill.config import Settings

OTHER = (
    "## Codebase conventions\n\n- Tests live under tests/<module>/.\n\n"
    "## Historical\n\n- [resolved 2026-08-01] flaky sqlite lock.\n"
)
RULES = ["Run `ruff format` on changed files before stopping.", "Register new files."]


# ---------------------------------------------------------------------------
# upsert_section / remove_section — pure text
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _openrouter_slot(fallback_slot_active):
    """These tests capture agent assembly through the OpenRouter seam; arm
    llmio's failover window so plain levels resolve the OpenRouter slot."""


def test_upsert_places_section_at_top_and_preserves_rest_byte_for_byte():
    out = cpr.upsert_section(OTHER, RULES)
    assert out.startswith(cpr.SECTION_HEADING + "\n")
    assert cpr.SECTION_END_MARKER in out
    for rule in RULES:
        assert f"- {rule}\n" in out
    # Everything after our block is the original document, untouched.
    assert out.endswith(OTHER)
    assert cpr.remove_section(out) == OTHER


def test_upsert_is_idempotent_and_rewrites_in_place_without_accreting():
    once = cpr.upsert_section(OTHER, RULES)
    twice = cpr.upsert_section(once, RULES)
    assert twice == once
    assert once.count(cpr.SECTION_HEADING) == 1

    replaced = cpr.upsert_section(once, ["Only one new rule."])
    assert replaced.count(cpr.SECTION_HEADING) == 1
    assert "Only one new rule." in replaced
    assert RULES[0] not in replaced
    assert replaced.endswith(OTHER)


def test_upsert_with_no_rules_removes_the_section():
    once = cpr.upsert_section(OTHER, RULES)
    assert cpr.upsert_section(once, []) == OTHER
    # No section, no rules: untouched.
    assert cpr.upsert_section(OTHER, []) == OTHER
    assert cpr.upsert_section("", []) == ""


def test_upsert_on_empty_ledger_yields_only_the_section():
    out = cpr.upsert_section("", RULES)
    assert out == cpr.render_section(RULES)


def test_remove_section_survives_a_hand_deleted_end_marker():
    mangled = f"{cpr.SECTION_HEADING}\n\nsome intro\n\n- a rule\n\n{OTHER}"
    assert cpr.remove_section(mangled) == OTHER


def test_remove_section_ignores_heading_quoted_mid_line():
    doc = f"- see the {cpr.SECTION_HEADING} block above\n{OTHER}"
    assert cpr.remove_section(doc) == doc


def test_section_relocated_by_an_agent_is_moved_back_to_the_top():
    misplaced = OTHER + "\n" + cpr.render_section(RULES)
    out = cpr.upsert_section(misplaced, RULES)
    assert out.startswith(cpr.SECTION_HEADING)
    assert out.count(cpr.SECTION_HEADING) == 1
    # The stray separator newline that preceded the misplaced block is
    # "other content" and is preserved as such.
    assert cpr.remove_section(out) == OTHER + "\n"


# ---------------------------------------------------------------------------
# write_rules_to_ledger
# ---------------------------------------------------------------------------


def test_write_rules_to_ledger_reports_change_and_is_idempotent(tmp_path):
    ledger = tmp_path / "board" / "implement_memory.md"
    assert cpr.write_rules_to_ledger(ledger, RULES) is True
    assert ledger.read_text() == cpr.render_section(RULES)
    assert cpr.write_rules_to_ledger(ledger, RULES) is False
    assert cpr.write_rules_to_ledger(ledger, []) is True
    assert ledger.read_text() == ""


def test_write_rules_to_ledger_does_not_create_an_empty_file(tmp_path):
    ledger = tmp_path / "board" / "implement_memory.md"
    assert cpr.write_rules_to_ledger(ledger, []) is False
    assert not ledger.exists()


def test_rules_at_top_are_what_the_implement_stage_loads(tmp_path):
    """The implement coordinator loads the ledger via ``load_memory`` with
    no size cap, so the section at the top is the first thing it reads —
    and a capped load still keeps the whole document when it fits."""
    ledger = tmp_path / "implement_memory.md"
    ledger.write_text(cpr.upsert_section(OTHER, RULES))
    loaded = load_memory(ledger)
    assert loaded.startswith(cpr.SECTION_HEADING)
    assert load_memory(ledger, max_chars=len(loaded)) == loaded


def test_implement_stage_loads_ledger_uncapped(monkeypatch):
    """Pin the contract the top-placement relies on: the implement stage
    does not pass ``max_chars`` (tail truncation would drop the top)."""
    import inspect

    from robotsix_mill.stages.implement import phase_coordinator as pc

    src = inspect.getsource(pc.PhaseCoordinatorMixin._load_implement_context)
    assert "load_memory(" in src
    assert "max_chars" not in src


# ---------------------------------------------------------------------------
# build_digest
# ---------------------------------------------------------------------------


def _ev(ticket, bucket, cat="CI_FAILURE", key="k", root="", rule=""):
    return DiagnosticEvent(
        category=cat,
        ticket_id=ticket,
        repo_id="b",
        reason="failing checks: ci / tests",
        normalized_key=key,
        timestamp="2026-08-30T00:00:00+00:00",
        bucket=bucket,
        root_cause=root,
        prevention_rule=rule,
    )


def test_build_digest_groups_by_bucket_most_impactful_first():
    failures = [
        _ev("t1", "mypy", root="error: Incompatible types", rule="Run mypy."),
        _ev("t2", "mypy", root="error: Incompatible types", rule="Run mypy."),
        _ev("t3", "mypy", root="error: Missing return", rule="Run mypy."),
        _ev("t4", "ruff-format", root="Would reformat: a.py", rule="Run ruff."),
        _ev("t5", "", root="weird"),  # legacy event → unknown
    ]
    resolved = [
        _ev("t1", "mypy", cat="CI_FIX_RESOLVED", root="Added the annotation."),
    ]
    digest = cpr.build_digest(failures, resolved)
    assert digest.startswith("````ci-failure-digest")
    assert (
        digest.index("### mypy")
        < digest.index("### ruff-format")
        < digest.index("### unknown")
    )
    assert "failures: 3 | distinct tickets: 3 | resolved by ci_fix: 1" in digest
    assert "- default rule: Run mypy." in digest
    assert "  - error: Incompatible types" in digest
    assert digest.count("error: Incompatible types") == 1  # samples deduped
    assert "- how ci_fix fixed them:" in digest
    assert "  - Added the annotation." in digest


def test_build_digest_empty():
    assert "(no CI_FAILURE events in the window)" in cpr.build_digest([], [])


# ---------------------------------------------------------------------------
# run_ci_prevention_rules_pass — end to end with a faked agent
# ---------------------------------------------------------------------------


@pytest.fixture
def board(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("MILL_DATA_DIR", str(data_dir))
    settings = Settings()
    assert settings.data_dir == data_dir
    repo_config = SimpleNamespace(repo_id="repo-a", board_id="board-a")
    return settings, repo_config


def _fake_agent(monkeypatch, rules, seen):
    def _run(*, settings, digest, definition):
        seen.append((digest, definition))
        return CiPreventionRulesResult(rules=rules)

    monkeypatch.setattr(cpr, "run_ci_prevention_rules_agent", _run)


def test_pass_writes_rules_into_implement_ledger_and_files_no_tickets(
    board, monkeypatch
):
    settings, repo_config = board
    ledger = settings.memory_file_for("implement", "board-a")
    ledger.parent.mkdir(parents=True)
    ledger.write_text(OTHER)
    for i in range(4):
        emit_diagnostic_event(
            settings,
            "board-a",
            "CI_FAILURE",
            f"t-{i}",
            "failing checks: ci / tests",
            f"key-{i}",
            bucket="ruff-format",
            root_cause="Would reformat: src/x.py",
            prevention_rule="Run ruff format.",
        )
    seen: list = []
    _fake_agent(
        monkeypatch,
        [
            "  Run `ruff format` before stopping. ",
            "",
            "run `ruff format` BEFORE stopping.",
        ],
        seen,
    )

    result = cpr.run_ci_prevention_rules_pass("sess", repo_config)

    assert result.drafts_created == []
    assert result.rules_written == [
        "Run `ruff format` before stopping."
    ]  # cleaned+deduped
    assert "4 CI_FAILURE event(s) → 1 prevention rule(s)" in result.summary
    assert "rewritten" in result.summary
    text = ledger.read_text()
    assert text.startswith(cpr.SECTION_HEADING)
    assert "- Run `ruff format` before stopping.\n" in text
    assert text.endswith(OTHER)
    # The agent saw the digest and the built-in definition.
    ((digest, definition),) = seen
    assert "### ruff-format" in digest
    assert definition.name == "ci_prevention_rules"
    assert definition.level == 1
    assert definition.output_type == "CiPreventionRulesResult"


def test_pass_is_idempotent_and_clips_to_max_rules(board, monkeypatch):
    settings, repo_config = board
    monkeypatch.setenv("MILL_CI_PREVENTION_MAX_RULES", "2")
    for i in range(3):
        emit_diagnostic_event(
            settings, "board-a", "CI_FAILURE", f"t-{i}", "r", "k", bucket="mypy"
        )
    _fake_agent(monkeypatch, ["a", "b", "c", "d"], [])

    first = cpr.run_ci_prevention_rules_pass("s1", repo_config)
    assert first.rules_written == ["a", "b"]
    ledger = settings.memory_file_for("implement", "board-a")
    after_first = ledger.read_text()

    second = cpr.run_ci_prevention_rules_pass("s2", repo_config)
    assert "unchanged" in second.summary
    assert ledger.read_text() == after_first
    assert after_first.count(cpr.SECTION_HEADING) == 1


def test_pass_honours_max_events_window(board, monkeypatch):
    settings, repo_config = board
    monkeypatch.setenv("MILL_CI_PREVENTION_RULES_MAX_EVENTS", "2")
    for i in range(5):
        emit_diagnostic_event(
            settings, "board-a", "CI_FAILURE", f"t-{i}", "r", "k", bucket="mypy"
        )
    seen: list = []
    _fake_agent(monkeypatch, ["x"], seen)
    result = cpr.run_ci_prevention_rules_pass("s", repo_config)
    assert "2 CI_FAILURE event(s)" in result.summary
    assert "failures: 2 | distinct tickets: 2" in seen[0][0]


def test_pass_removes_section_when_no_events_and_never_calls_agent(board, monkeypatch):
    settings, repo_config = board
    ledger = settings.memory_file_for("implement", "board-a")
    ledger.parent.mkdir(parents=True)
    ledger.write_text(cpr.upsert_section(OTHER, RULES))

    def _boom(**_kw):
        raise AssertionError("agent must not run without events")

    monkeypatch.setattr(cpr, "run_ci_prevention_rules_agent", _boom)
    result = cpr.run_ci_prevention_rules_pass("s", repo_config)
    assert "section removed" in result.summary
    assert ledger.read_text() == OTHER


def test_pass_with_empty_rules_removes_stale_section(board, monkeypatch):
    settings, repo_config = board
    ledger = settings.memory_file_for("implement", "board-a")
    ledger.parent.mkdir(parents=True)
    ledger.write_text(cpr.upsert_section(OTHER, RULES))
    emit_diagnostic_event(settings, "board-a", "CI_FAILURE", "t", "r", "k")
    _fake_agent(monkeypatch, [], [])
    result = cpr.run_ci_prevention_rules_pass("s", repo_config)
    assert result.rules_written == []
    assert ledger.read_text() == OTHER


def test_pass_uses_definition_override_when_given(board, monkeypatch):
    settings, repo_config = board
    emit_diagnostic_event(settings, "board-a", "CI_FAILURE", "t", "r", "k")
    seen: list = []
    _fake_agent(monkeypatch, ["x"], seen)
    override = SimpleNamespace(name="ci_prevention_rules", level=3)
    cpr.run_ci_prevention_rules_pass("s", repo_config, definition_override=override)
    assert seen[0][1] is override


def test_pass_requires_repo_config():
    with pytest.raises(ValueError, match="repo_config is required"):
        cpr.run_ci_prevention_rules_pass("s", None)


def test_agent_failure_leaves_ledger_untouched(board, monkeypatch):
    settings, repo_config = board
    ledger = settings.memory_file_for("implement", "board-a")
    ledger.parent.mkdir(parents=True)
    before = cpr.upsert_section(OTHER, RULES)
    ledger.write_text(before)
    emit_diagnostic_event(settings, "board-a", "CI_FAILURE", "t", "r", "k")

    def _boom(**_kw):
        raise RuntimeError("model down")

    monkeypatch.setattr(cpr, "run_ci_prevention_rules_agent", _boom)
    with pytest.raises(RuntimeError):
        cpr.run_ci_prevention_rules_pass("s", repo_config)
    assert ledger.read_text() == before


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_pass_is_wired_everywhere():
    from robotsix_mill.agents import workflow_portability as wp
    from robotsix_mill.cli import _RUNNERS
    from robotsix_mill.runtime.routes._passes import _PASS_REGISTRY
    from robotsix_mill.runtime.worker.poll_loops import PollLoopsMixin

    assert wp.kind_for("ci_prevention_rules") == "llm_agent"
    assert _PASS_REGISTRY["ci_prevention_rules"]["runner_func"] == (
        "run_ci_prevention_rules_pass"
    )
    assert _RUNNERS["ci-prevention-rules"]["function"] == "run_ci_prevention_rules_pass"
    assert "ci_prevention_rules" in PollLoopsMixin._CUSTOM_LLM_AGENT_RUNNERS
