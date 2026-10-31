import json
from pathlib import Path

from robotsix_mill.agents import refining
from robotsix_mill.agents.refining import (
    RefineResult,
)
from robotsix_mill.config import Settings
from robotsix_mill.core.models import TicketKind
from robotsix_mill.core.states import State
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.refine import RefineStage
from tests.agents._refine_test_helpers import (
    _FakeRunResult,
    _FakeUsage,
    _simple_agent,
)
from tests.agents.conftest import (
    _original_triage_refine,
    _single,
)

# ---------------------------------------------------------------------------
# Trivial-scope routing tests (AC: TriageResult.trivial_scope +
# run_refine_agent level override)
# ---------------------------------------------------------------------------


def test_triage_result_trivial_scope_default():
    """TriageResult.trivial_scope defaults to None when not provided."""
    from robotsix_mill.agents.refining import TriageResult

    r = TriageResult(decision="REFINE", reason="test")
    assert r.trivial_scope is None


def test_triage_result_trivial_scope_parsed_from_json():
    """TriageResult.trivial_scope parses correctly from JSON/dict input."""
    from robotsix_mill.agents.refining import TriageResult

    r = TriageResult.model_validate(
        {"decision": "REFINE", "reason": "test", "trivial_scope": True}
    )
    assert r.trivial_scope is True

    r2 = TriageResult.model_validate(
        {"decision": "REFINE", "reason": "test", "trivial_scope": False}
    )
    assert r2.trivial_scope is False


def test_triage_result_trivial_scope_false_by_default():
    """When trivial_scope is omitted, _read_triage_trivial returns False
    (conservative default — no cheap-model routing)."""
    import tempfile

    from robotsix_mill.stages.refine.orchestration import _read_triage_trivial

    # Simulate a workspace with triage_complexity.json that lacks trivial_scope.
    tmp = Path(tempfile.mkdtemp())
    artifacts = tmp / "artifacts"
    artifacts.mkdir()
    (artifacts / "triage_complexity.json").write_text(
        json.dumps({"complexity": "simple"}), encoding="utf-8"
    )

    class FakeWS:
        artifacts_dir = artifacts

    assert _read_triage_trivial(FakeWS()) is False  # type: ignore[arg-type]


def test_triage_result_exploration_findings_default():
    """TriageResult.exploration_findings defaults to None when not provided."""
    from robotsix_mill.agents.refining import TriageResult

    r = TriageResult(decision="REFINE", reason="test")
    assert r.exploration_findings is None


def test_triage_result_exploration_findings_roundtrip():
    """TriageResult.exploration_findings preserves a non-empty string
    through model_validate round-trip."""
    from robotsix_mill.agents.refining import TriageResult

    findings = (
        "- Verified `src/foo/bar.py` exists (342 lines)\n"
        "- Confirmed `Frobnicator.run()` at line 89\n"
    )
    r = TriageResult.model_validate(
        {
            "decision": "REFINE",
            "reason": "multi-file",
            "complexity": "needs-exploration",
            "exploration_findings": findings,
        }
    )
    assert r.exploration_findings == findings

    # Empty string is preserved (not coerced to None).
    r2 = TriageResult.model_validate(
        {
            "decision": "REFINE",
            "reason": "test",
            "exploration_findings": "",
        }
    )
    assert r2.exploration_findings == ""


def test_triage_findings_artifact_write_read():
    """_write_triage_complexity with findings writes triage_findings.json;
    _read_triage_findings returns it; absent file returns None."""
    import tempfile

    from robotsix_mill.stages.refine.orchestration import (
        _read_triage_findings,
        _write_triage_complexity,
    )

    tmp = Path(tempfile.mkdtemp())
    artifacts = tmp / "artifacts"
    artifacts.mkdir()

    class FakeWS:
        artifacts_dir = artifacts

    ws = FakeWS()

    # No file yet → None
    assert _read_triage_findings(ws) is None  # type: ignore[arg-type]

    # Write with findings
    _write_triage_complexity(
        ws,  # type: ignore[arg-type]
        "needs-exploration",
        findings="- Verified `src/a.py` exists\n",
    )

    # Now the findings file exists
    findings = _read_triage_findings(ws)  # type: ignore[arg-type]
    assert findings == "- Verified `src/a.py` exists\n"

    # Complexity file still written
    data = json.loads(
        (artifacts / "triage_complexity.json").read_text(encoding="utf-8")
    )
    assert data["complexity"] == "needs-exploration"

    # Write without findings — findings file should NOT be created/overwritten
    # if it didn't exist before (but existing one stays).
    # Test fresh workspace: no findings
    tmp2 = Path(tempfile.mkdtemp())
    art2 = tmp2 / "artifacts"
    art2.mkdir()

    class FakeWS2:
        artifacts_dir = art2

    _write_triage_complexity(FakeWS2(), "simple")  # type: ignore[arg-type]
    assert not (art2 / "triage_findings.json").exists()

    # findings=None should not create the file
    _write_triage_complexity(FakeWS2(), "needs-exploration", findings=None)  # type: ignore[arg-type]
    assert not (art2 / "triage_findings.json").exists()

    # Empty findings should not create the file
    _write_triage_complexity(FakeWS2(), "needs-exploration", findings="")  # type: ignore[arg-type]
    assert not (art2 / "triage_findings.json").exists()

    # Corrupt JSON → None
    (artifacts / "triage_findings.json").write_text("{bad json", encoding="utf-8")
    assert _read_triage_findings(ws) is None  # type: ignore[arg-type]

    # Missing key → None
    (artifacts / "triage_findings.json").write_text(
        json.dumps({"other": "value"}), encoding="utf-8"
    )
    assert _read_triage_findings(ws) is None  # type: ignore[arg-type]


def test_run_refine_agent_passes_refine_level_to_build_agent(
    monkeypatch, settings, tmp_path
):
    """When refine_level=1 is passed, build_agent_from_definition receives
    level=1 in its overrides dict."""
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    captured_overrides: list[dict] = []

    def fake_build_agent(*a, **kw):
        captured_overrides.append(kw)
        return _simple_agent()

    monkeypatch.setattr(base_module, "build_agent_from_definition", fake_build_agent)
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    refining.run_refine_agent(
        settings=settings,
        title="Test",
        draft="test draft",
        repo_dir=repo_dir,
        refine_level=1,
    )

    assert len(captured_overrides) == 1
    assert captured_overrides[0].get("level") == 1, (
        f"Expected level=1 in overrides, got {captured_overrides[0]}"
    )


def test_run_refine_agent_no_refine_level_leaves_level_unchanged(
    monkeypatch, settings, tmp_path
):
    """When refine_level is NOT passed (None/default), build_agent_from_definition
    does NOT receive a 'level' key in its overrides — the YAML default applies."""
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    captured_overrides: list[dict] = []

    def fake_build_agent(*a, **kw):
        captured_overrides.append(kw)
        return _simple_agent()

    monkeypatch.setattr(base_module, "build_agent_from_definition", fake_build_agent)
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    refining.run_refine_agent(
        settings=settings,
        title="Test",
        draft="test draft",
        repo_dir=repo_dir,
        # refine_level not passed
    )

    assert len(captured_overrides) == 1
    assert "level" not in captured_overrides[0], (
        f"Expected no 'level' in overrides when refine_level not passed, "
        f"got {captured_overrides[0]}"
    )


# ── refine_model alias / model override ─────────────────────────────


def test_run_refine_agent_passes_refine_model_sonnet(monkeypatch, settings, tmp_path):
    """When refine_model='sonnet' is passed on the level-2 (Claude alias)
    branch, the build forwards model='sonnet' to build_agent_from_definition."""
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    captured_overrides: list[dict] = []

    def fake_build_agent(*a, **kw):
        captured_overrides.append(kw)
        return _simple_agent()

    monkeypatch.setattr(base_module, "build_agent_from_definition", fake_build_agent)
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    refining.run_refine_agent(
        settings=settings,
        title="Test",
        draft="test draft",
        repo_dir=repo_dir,
        refine_level=2,
        refine_model="sonnet",
    )

    assert len(captured_overrides) == 1
    assert captured_overrides[0].get("model") == "sonnet", (
        f"Expected model='sonnet' in overrides, got {captured_overrides[0]}"
    )


def test_run_refine_agent_passes_refine_model_opus(monkeypatch, settings, tmp_path):
    """When refine_model='opus' is passed, build_agent_from_definition receives
    model='opus' in overrides."""
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    captured_overrides: list[dict] = []

    def fake_build_agent(*a, **kw):
        captured_overrides.append(kw)
        return _simple_agent()

    monkeypatch.setattr(base_module, "build_agent_from_definition", fake_build_agent)
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    refining.run_refine_agent(
        settings=settings,
        title="Test",
        draft="test draft",
        repo_dir=repo_dir,
        refine_level=2,
        refine_model="opus",
    )

    assert len(captured_overrides) == 1
    assert captured_overrides[0].get("model") == "opus", (
        f"Expected model='opus' in overrides, got {captured_overrides[0]}"
    )


def test_run_refine_agent_downgrade_skips_model_override(
    monkeypatch, settings, tmp_path
):
    """When refine_level=1 (DeepSeek downgrade), model override is NOT applied
    even when refine_model is passed."""
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    captured_overrides: list[dict] = []

    def fake_build_agent(*a, **kw):
        captured_overrides.append(kw)
        return _simple_agent()

    monkeypatch.setattr(base_module, "build_agent_from_definition", fake_build_agent)
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    refining.run_refine_agent(
        settings=settings,
        title="Test",
        draft="test draft",
        repo_dir=repo_dir,
        refine_level=1,
        refine_model="sonnet",
    )

    assert len(captured_overrides) == 1
    assert "model" not in captured_overrides[0], (
        f"Expected no 'model' in overrides when refine_level=1 (DeepSeek path), "
        f"got {captured_overrides[0]}"
    )
    # level override still applies for DeepSeek routing
    assert captured_overrides[0].get("level") == 1, (
        f"Expected level=1 override still present, got {captured_overrides[0]}"
    )


def test_run_refine_agent_default_level_ignores_model_alias(
    monkeypatch, settings, tmp_path
):
    """At the YAML default level (3 since 2026-09-07) the Claude model-alias
    hook does not apply: the tier's own model is used as-is, so a passed
    refine_model must NOT reach build_agent_from_definition."""
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    captured_overrides: list[dict] = []

    def fake_build_agent(*a, **kw):
        captured_overrides.append(kw)
        return _simple_agent()

    monkeypatch.setattr(base_module, "build_agent_from_definition", fake_build_agent)
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    refining.run_refine_agent(
        settings=settings,
        title="Test",
        draft="test draft",
        repo_dir=repo_dir,
        refine_model="sonnet",
    )

    assert len(captured_overrides) == 1
    assert "model" not in captured_overrides[0]
    assert "level" not in captured_overrides[0]


def test_run_refine_agent_no_refine_model_no_override(monkeypatch, settings, tmp_path):
    """When refine_model is None/not passed, no model override is injected
    — build_agent_from_definition uses its own default (Opus for level 3)."""
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    captured_overrides: list[dict] = []

    def fake_build_agent(*a, **kw):
        captured_overrides.append(kw)
        return _simple_agent()

    monkeypatch.setattr(base_module, "build_agent_from_definition", fake_build_agent)
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    refining.run_refine_agent(
        settings=settings,
        title="Test",
        draft="test draft",
        repo_dir=repo_dir,
        # refine_model not passed → None → no model in overrides
    )

    assert len(captured_overrides) == 1
    assert "model" not in captured_overrides[0], (
        f"Expected no 'model' in overrides when refine_model not passed, "
        f"got {captured_overrides[0]}"
    )


# ── request_limit_override / per-tier turn cap ──────────────────────


def test_run_refine_agent_uses_request_limit_override(monkeypatch, settings):
    """When request_limit_override is passed, UsageLimits uses it instead
    of settings.refine_request_limit."""
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    captured: dict = {}

    class _MockAgent:
        def run_sync(self, user_prompt, *, message_history=None, usage_limits=None):
            captured["usage_limits"] = usage_limits
            return _FakeRunResult(
                output=RefineResult(split=False, spec_markdown="## Problem\nok\n"),
                finish_reason="stop",
                all_messages=[],
            )

        def close(self):
            pass

    monkeypatch.setattr(
        base_module, "build_agent_from_definition", lambda *a, **kw: _MockAgent()
    )
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    refining.run_refine_agent(
        settings=settings,
        title="t",
        draft="d",
        request_limit_override=40,
    )

    assert captured["usage_limits"] is not None
    assert captured["usage_limits"].request_limit == 40


def test_run_refine_agent_no_request_limit_override_uses_default(
    monkeypatch,
    settings,
):
    """When request_limit_override is not passed, settings.refine_request_limit
    is used (the existing behaviour preserved)."""
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    captured: dict = {}

    class _MockAgent:
        def run_sync(self, user_prompt, *, message_history=None, usage_limits=None):
            captured["usage_limits"] = usage_limits
            return _FakeRunResult(
                output=RefineResult(split=False, spec_markdown="## Problem\nok\n"),
                finish_reason="stop",
                all_messages=[],
            )

        def close(self):
            pass

    monkeypatch.setattr(
        base_module, "build_agent_from_definition", lambda *a, **kw: _MockAgent()
    )
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    monkeypatch.setattr(settings, "refine_request_limit", 77)
    refining.run_refine_agent(
        settings=settings,
        title="t",
        draft="d",
    )

    assert captured["usage_limits"] is not None
    assert captured["usage_limits"].request_limit == 77


def test_warning_logged_when_usage_exceeds_threshold(monkeypatch, settings, caplog):
    """When the refine agent uses > refine_usage_warning_threshold of its
    request_limit, a warning is logged."""
    import logging

    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    effective_limit = settings.refine_request_limit
    high_usage = int(effective_limit * 0.9)

    class _MockAgent:
        def run_sync(self, user_prompt, *, message_history=None, usage_limits=None):
            return _FakeRunResult(
                output=RefineResult(split=False, spec_markdown="## Problem\nok\n"),
                finish_reason="stop",
                all_messages=[],
                usage=_FakeUsage(requests=high_usage),
            )

        def close(self):
            pass

    monkeypatch.setattr(
        base_module, "build_agent_from_definition", lambda *a, **kw: _MockAgent()
    )
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    with caplog.at_level(logging.WARNING):
        refining.run_refine_agent(
            settings=settings,
            title="t",
            draft="d",
        )

    assert any(
        "near request_limit exhaustion" in rec.message for rec in caplog.records
    ), "Expected warning about near-exhaustion but none logged"


def test_no_warning_when_usage_below_threshold(monkeypatch, settings, caplog):
    """When the refine agent uses ≤ refine_usage_warning_threshold of its
    request_limit, no warning is logged."""
    import logging

    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    effective_limit = settings.refine_request_limit
    low_usage = int(effective_limit * 0.5)

    class _MockAgent:
        def run_sync(self, user_prompt, *, message_history=None, usage_limits=None):
            return _FakeRunResult(
                output=RefineResult(split=False, spec_markdown="## Problem\nok\n"),
                finish_reason="stop",
                all_messages=[],
                usage=_FakeUsage(requests=low_usage),
            )

        def close(self):
            pass

    monkeypatch.setattr(
        base_module, "build_agent_from_definition", lambda *a, **kw: _MockAgent()
    )
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    with caplog.at_level(logging.WARNING):
        refining.run_refine_agent(
            settings=settings,
            title="t",
            draft="d",
        )

    assert not any(
        "near request_limit exhaustion" in rec.message for rec in caplog.records
    ), "Unexpected near-exhaustion warning for low usage"


def test_split_child_fast_path_sets_simple_complexity(ctx, service, monkeypatch):
    """Split-child fast path writes triage_complexity.json as 'simple',
    suppressing exploration on the already-refined child."""
    parent = service.create("Parent task", "big feature", kind=TicketKind.TASK)
    # Close the parent with a "split into" note to trigger fast-path.
    service.transition(parent.id, State.CLOSED, "split into 1 children: child-1")
    child = service.create(
        "Already refined child",
        "## Problem\nrefined child\n## Scope\n- thing\n## Acceptance criteria\n- [ ] works\n",
        parent_id=parent.id,
    )

    refine_called = False

    def spy_refine(**kwargs):
        nonlocal refine_called
        refine_called = True
        return _single("should not be called")

    monkeypatch.setattr(refining, "run_refine_agent", spy_refine)

    out = RefineStage().run(child, ctx)

    assert not refine_called, "Split-child fast path must skip refine agent"
    assert out.next_state is State.READY

    # Verify complexity artifact was written as "simple"
    from robotsix_mill.stages.refine.orchestration import _read_triage_complexity

    ws = service.workspace(child)
    assert _read_triage_complexity(ws) == "simple"


# ---------------------------------------------------------------------------
# Auto-approve input reduction tests
# ---------------------------------------------------------------------------


def test_summarize_spec_for_auto_approve_short_spec_unchanged():
    """A spec within the 2000-char limit is returned unchanged."""
    from robotsix_mill.stages.refine.helpers import _summarize_spec_for_auto_approve

    short = "## Problem\nFix typo\n## Scope\n- README.md\n## Acceptance criteria\n- [ ] typo fixed\n"
    assert _summarize_spec_for_auto_approve(short) == short


def test_summarize_spec_for_auto_approve_truncates_long_spec():
    """A spec exceeding the 2000-char limit is truncated with a note."""
    from robotsix_mill.stages.refine.helpers import _summarize_spec_for_auto_approve

    # Build a spec longer than 2000 chars.
    header = "## Problem\nLong spec\n## Scope\n"
    filler = "- line " + "x" * 80 + "\n"
    long_spec = header + filler * 100  # ~8000+ chars

    result = _summarize_spec_for_auto_approve(long_spec)
    assert len(result) <= 2005  # within limit + truncation note
    assert "… (truncated for auto-approve" in result
    assert result.startswith(header)


def test_auto_approve_receives_summarized_spec(
    ctx, service, monkeypatch, tmp_path, repo_config
):
    """triage_auto_approve receives a summarized (not full) spec payload."""
    # Build a spec that's long enough to trigger summarization.
    header = "## Problem\nAdd pagination\n## Scope\n"
    filler = "- list " + "x" * 80 + "\n"
    long_spec = header + filler * 80  # well over 2000 chars

    captured_spec: list[str] = []

    def fake_auto_approve(*, settings, spec):
        captured_spec.append(spec)
        return refining.AutoApproveResult(decision="APPROVE", reason="precise spec")

    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(long_spec))
    monkeypatch.setattr(refining, "triage_auto_approve", fake_auto_approve)

    gated = Settings(
        data_dir=str(tmp_path),
        require_approval="true",
        # Disable the combined post-refine check so _resolve_next_state
        # takes the legacy triage_auto_approve fallback — the
        # summarization seam this test verifies.
        spec_review_enabled="false",
    )
    gated_ctx = StageContext(settings=gated, service=service, repo_config=repo_config)

    t = service.create("Add pagination", "add pagination to list endpoints")
    out = RefineStage().run(t, gated_ctx)

    assert out.next_state is State.READY
    assert len(captured_spec) == 1
    # The captured spec should be summarized, not the full long_spec.
    assert captured_spec[0] != long_spec
    assert len(captured_spec[0]) < len(long_spec)
    assert "… (truncated for auto-approve" in captured_spec[0]
    # But it must still contain the Problem header.
    assert "## Problem" in captured_spec[0]


def test_auto_approve_yaml_no_budget_discipline_note():
    """The auto-approve.yaml system prompt must NOT contain the
    'Budget discipline' section."""

    import yaml as _yaml

    path = (
        Path(__file__).parent.parent.parent / "agent_definitions" / "auto-approve.yaml"
    )
    prompt = _yaml.safe_load(path.read_text())["system_prompt"]
    assert "Budget discipline" not in prompt, (
        "auto-approve.yaml must not contain the 'Budget discipline' note"
    )


# ---------------------------------------------------------------------------
# Memory DB tests (acceptance criterion 6)
# ---------------------------------------------------------------------------


def test_memory_db_round_trip(tmp_path):
    """Write memory to DB, read it back — content matches."""
    from robotsix_mill.config import Settings as S
    from robotsix_mill.core.db import (
        load_memory_db,
        persist_memory_db,
        reset_engine,
    )

    reset_engine()
    s = S(data_dir=str(tmp_path))
    board_id = "test-board"
    name = "refine"

    # Initially empty.
    assert load_memory_db(s, board_id, name) == ""

    # Write some content.
    content = "## Refine run 2026-06-19\nObserved: tests need better fixtures.\n"
    persist_memory_db(s, board_id, name, content)

    # Read back.
    assert load_memory_db(s, board_id, name) == content

    # Second write updates.
    content2 = content + "## Refine run 2026-06-20\nObserved: use DB for memory.\n"
    persist_memory_db(s, board_id, name, content2)
    assert load_memory_db(s, board_id, name) == content2


def test_memory_db_truncation(tmp_path):
    """Long content is truncated with max_chars, keeping most recent entries."""
    from robotsix_mill.config import Settings as S
    from robotsix_mill.core.db import (
        load_memory_db,
        persist_memory_db,
        reset_engine,
    )

    reset_engine()
    s = S(data_dir=str(tmp_path))
    board_id = "test-board"
    name = "refine"

    # Build content with many entries, newest last.
    entries = []
    for i in range(20):
        entries.append(
            f"## Refine run 2026-06-{i + 1:02d}\n- **Observation**: entry {i}\n"
        )
    content = "\n".join(entries)  # ~20 entries

    persist_memory_db(s, board_id, name, content)

    # Read with a cap that keeps only the most recent entries.
    result = load_memory_db(s, board_id, name, max_chars=500)
    assert len(result) <= 550  # within cap + truncation note
    assert "entry 19" in result  # most recent
    assert "entry 18" in result  # second most recent
    assert "entry 0" not in result  # oldest dropped
    assert "truncated" in result.lower()


def test_memory_db_migration_from_legacy_file(tmp_path):
    """First persist_memory_db migrates content from legacy Markdown file."""
    from robotsix_mill.config import Settings as S
    from robotsix_mill.core.db import (
        load_memory_db,
        persist_memory_db,
        reset_engine,
    )

    reset_engine()
    s = S(data_dir=str(tmp_path))
    board_id = "test-board"
    name = "refine"

    # Create legacy file.
    legacy_dir = tmp_path / board_id
    legacy_dir.mkdir(parents=True, exist_ok=True)
    legacy_path = legacy_dir / f"{name}_memory.md"
    legacy_content = "## Prior observations\nCross-cutting pattern: use DB.\n"
    legacy_path.write_text(legacy_content, encoding="utf-8")

    # Write new content — new text is non-empty, so legacy is NOT
    # prepended (only carried over when no new text is given).
    new_content = "## Refine run 2026-06-20\nNew observation.\n"
    persist_memory_db(s, board_id, name, new_content)

    # Read back — should contain only the new content (legacy
    # is not prepended when new text is provided).
    result = load_memory_db(s, board_id, name)
    assert "New observation" in result
    assert "Cross-cutting pattern: use DB" not in result

    # Legacy file should be renamed.
    migrated_path = legacy_dir / f"{name}_memory.md.migrated"
    assert migrated_path.exists()
    assert not legacy_path.exists()


def test_memory_db_cross_cutting_entries_survive_retention(tmp_path):
    """Cross-cutting entries (general patterns) survive retention when
    they are among the most recent entries; oldest per-ticket entries
    are dropped first."""
    from robotsix_mill.config import Settings as S
    from robotsix_mill.core.db import (
        load_memory_db,
        persist_memory_db,
        reset_engine,
    )

    reset_engine()
    s = S(data_dir=str(tmp_path))
    board_id = "test-board"
    name = "refine"

    # Build content: many old per-ticket entries first, then a
    # cross-cutting pattern at the end (most recent).
    content = ""
    for i in range(60):
        content += f"## Refine run 2026-06-{i + 1:02d} — ticket {i}\n"
        content += f"- Per-ticket observation number {i} with some extra padding text\n"
    # Cross-cutting pattern at the end (newest entry).
    content += (
        "## Cross-cutting: shared libs live in external repos\n"
        "- src/<pkg>/, Hatchling.\n"
    )

    persist_memory_db(s, board_id, name, content)

    # Read with a cap that triggers truncation.
    result = load_memory_db(s, board_id, name, max_chars=2000)
    # Cross-cutting knowledge is recent — should survive.
    assert "Cross-cutting" in result
    # Oldest ticket entries should be gone.
    assert "ticket 0" not in result
    # The truncation note signals oldest content was dropped.
    assert "truncated" in result.lower()


# ---------------------------------------------------------------------------
# TriageResult MIGRATE decision tests
# ---------------------------------------------------------------------------


def test_triage_result_accepts_migrate_decision():
    """TriageResult.model_validate accepts decision='MIGRATE' with a target_board."""
    from robotsix_mill.agents.refining import TriageResult

    r = TriageResult.model_validate(
        {
            "decision": "MIGRATE",
            "reason": "belongs to robotsix-mill board",
            "target_board": "robotsix-mill",
        }
    )
    assert r.decision == "MIGRATE"
    assert r.target_board == "robotsix-mill"
    assert r.reason == "belongs to robotsix-mill board"


def test_triage_result_migrate_without_target_board_is_valid():
    """TriageResult with decision='MIGRATE' but no target_board is still
    valid at the model level (the stage validates, not the model)."""
    from robotsix_mill.agents.refining import TriageResult

    r = TriageResult.model_validate(
        {"decision": "MIGRATE", "reason": "belongs elsewhere"}
    )
    assert r.decision == "MIGRATE"
    assert r.target_board is None


def test_triage_result_migrate_target_board_preserved():
    """target_board is preserved through model round-trip."""
    from robotsix_mill.agents.refining import TriageResult

    r = TriageResult(
        decision="MIGRATE",
        reason="misrouted",
        target_board="other-board",
    )
    assert r.target_board == "other-board"
    # Round-trip through dict
    d = r.model_dump()
    r2 = TriageResult.model_validate(d)
    assert r2.decision == "MIGRATE"
    assert r2.target_board == "other-board"


def test_triage_prompt_includes_registered_boards(monkeypatch):
    """The triage prompt built by triage_refine includes a registered-boards
    catalog section when get_repos_config succeeds."""
    # Restore the real triage_refine (autouse fixture replaced it).
    monkeypatch.setattr(refining, "triage_refine", _original_triage_refine)

    from unittest.mock import patch

    from robotsix_mill.agents.refining import TriageResult, triage_refine
    from robotsix_mill.config import RepoConfig, ReposRegistry

    # Build a fake repos registry with a few boards.
    fake_registry = ReposRegistry(
        repos={
            "repo-a": RepoConfig(
                repo_id="repo-a",
                board_id="board-a",
                langfuse_project_name="test",
                langfuse_public_key="pk-test",
                langfuse_secret_key="sk-test",
            ),
            "repo-b": RepoConfig(
                repo_id="repo-b",
                board_id="board-b",
                langfuse_project_name="test",
                langfuse_public_key="pk-test",
                langfuse_secret_key="sk-test",
            ),
            "repo-c": RepoConfig(
                repo_id="repo-c",
                board_id="board-c",
                langfuse_project_name="test",
                langfuse_public_key="pk-test",
                langfuse_secret_key="sk-test",
            ),
        }
    )

    # Capture the user prompt passed to the agent.
    captured_prompts: list[str] = []

    class _FakeAgent:
        def run_sync(self, user_prompt, *, usage_limits=None):
            captured_prompts.append(user_prompt)
            from types import SimpleNamespace

            return SimpleNamespace(output=TriageResult(decision="REFINE", reason="ok"))

    with (
        patch(
            "robotsix_mill.agents.base.build_agent_from_definition",
            return_value=_FakeAgent(),
        ),
        patch(
            "robotsix_mill.config.get_repos_config",
            return_value=fake_registry,
        ),
    ):
        from types import SimpleNamespace as _NS

        triage_refine(
            settings=_NS(triage_request_limit=10),
            title="Test ticket",
            draft="test draft",
            repo_dir=None,
        )

    assert len(captured_prompts) == 1
    prompt = captured_prompts[0]
    assert "# Registered boards" in prompt
    assert "valid target_board" in prompt
    assert "- board-a" in prompt
    assert "- board-b" in prompt
    assert "- board-c" in prompt


def test_triage_prompt_graceful_on_repos_config_failure(monkeypatch):
    """When get_repos_config raises, the triage prompt does NOT include the
    registered-boards section, and triage_refine still succeeds."""
    # Restore the real triage_refine (autouse fixture replaced it).
    monkeypatch.setattr(refining, "triage_refine", _original_triage_refine)

    from unittest.mock import patch

    from robotsix_mill.agents.refining import TriageResult, triage_refine

    captured_prompts: list[str] = []

    class _FakeAgent:
        def run_sync(self, user_prompt, *, usage_limits=None):
            captured_prompts.append(user_prompt)
            from types import SimpleNamespace

            return SimpleNamespace(output=TriageResult(decision="REFINE", reason="ok"))

    with (
        patch(
            "robotsix_mill.agents.base.build_agent_from_definition",
            return_value=_FakeAgent(),
        ),
        patch(
            "robotsix_mill.config.get_repos_config",
            side_effect=RuntimeError("boom"),
        ),
    ):
        from types import SimpleNamespace as _NS

        triage_refine(
            settings=_NS(triage_request_limit=10),
            title="Test ticket",
            draft="test draft",
            repo_dir=None,
        )

    assert len(captured_prompts) == 1
    prompt = captured_prompts[0]
    assert "# Registered boards" not in prompt
