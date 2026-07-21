import pytest

from robotsix_mill.agents import refining
from robotsix_mill.agents.refining import RefineResult
from robotsix_mill.config import Settings
from robotsix_mill.core.states import State
from robotsix_mill.stages import StageContext
from robotsix_mill.stages import refine as refine_module
from robotsix_mill.stages.refine import RefineStage


def _single(spec: str, file_map=None) -> RefineResult:
    """Shorthand for a single-scope refine result."""
    return RefineResult(split=False, spec_markdown=spec, file_map=file_map)


@pytest.fixture
def ctx(settings, service, repo_config):
    return StageContext(settings=settings, service=service, repo_config=repo_config)


# ---------------------------------------------------------------------------
# triage pass tests
# ---------------------------------------------------------------------------


def test_triage_refine_agent_config(monkeypatch, tmp_path):
    """triage_refine builds an agent with zero tools,
    web_knowledge=False, and the triage level (1) from triage.yaml."""
    from robotsix_mill.agents import base as base_mod
    from robotsix_mill.agents.refining import triage_refine, TriageResult

    seen_kwargs: dict = {}

    def fake_build_agent(
        settings,
        system_prompt,
        output_type,
        tools,
        web_knowledge,
        report_issue,
        level,
        name,
        ask_user,
        **kwargs,
    ):
        seen_kwargs.update(
            tools=tools,
            web_knowledge=web_knowledge,
            report_issue=report_issue,
            level=level,
            name=name,
            ask_user=ask_user,
        )

        class FakeAgent:
            def run_sync(
                self, msg, message_history=None, board_id="", usage_limits=None
            ):
                return type(
                    "R", (), {"output": TriageResult(decision="REFINE", reason="test")}
                )()

        return FakeAgent()

    monkeypatch.setattr(base_mod, "build_agent", fake_build_agent)

    s = Settings(data_dir=str(tmp_path))
    result = triage_refine(settings=s, title="Test", draft="do x in foo.py")

    assert result.decision == "REFINE"
    assert seen_kwargs["tools"] == []
    assert seen_kwargs["web_knowledge"] is False
    assert seen_kwargs["report_issue"] is False
    assert seen_kwargs["level"] == 2  # triage.yaml level (promoted per #1692)
    assert seen_kwargs["name"] == "triage"
    assert seen_kwargs["ask_user"] is False


def test_triage_refine_wires_read_file_with_repo_dir(monkeypatch, tmp_path):
    """With repo_dir provided, triage_refine wires exactly an ``explore``
    tool plus a read-only ``read_file`` tool — and no write/edit/delete/
    run_command/list_dir."""
    from robotsix_mill.agents import base as base_mod
    from robotsix_mill.agents.refining import triage_refine, TriageResult

    seen_kwargs: dict = {}

    def fake_build_agent(
        settings,
        system_prompt,
        output_type,
        tools,
        web_knowledge,
        report_issue,
        level,
        name,
        ask_user,
        **kwargs,
    ):
        seen_kwargs.update(tools=tools)

        class FakeAgent:
            def run_sync(
                self, msg, message_history=None, board_id="", usage_limits=None
            ):
                return type(
                    "R", (), {"output": TriageResult(decision="REFINE", reason="test")}
                )()

        return FakeAgent()

    monkeypatch.setattr(base_mod, "build_agent", fake_build_agent)

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    s = Settings(data_dir=str(tmp_path))
    result = triage_refine(
        settings=s, title="Test", draft="do x in foo.py", repo_dir=repo_dir
    )

    assert result.decision == "REFINE"
    tool_names = {t.__name__ for t in seen_kwargs["tools"]}
    assert "explore" in tool_names
    assert "read_file" in tool_names
    assert not (
        tool_names
        & {"write_file", "edit_file", "delete_file", "run_command", "list_dir"}
    )


def test_triage_skip_skips_full_refine(ctx, service, monkeypatch):
    """When triage returns SKIP, run_refine_agent is NOT called,
    the draft is preserved, and the ticket goes to READY."""
    from robotsix_mill.agents.refining import TriageResult

    refine_called = False

    def fake_triage(*, settings, title, draft, repo_dir=None, extra_roots=None):
        return TriageResult(
            decision="SKIP", reason="doc-only change, no exploration needed"
        )

    def fake_refine(
        *,
        settings,
        title,
        draft,
        repo_dir=None,
        reviewer_comments=None,
        memory="",
        epic_context="",
        extra_roots=None,
        message_history=None,
        board_id="",
        **kwargs,
    ):
        nonlocal refine_called
        refine_called = True
        return _single("should not be called")

    monkeypatch.setattr(refining, "triage_refine", fake_triage)
    monkeypatch.setattr(refining, "run_refine_agent", fake_refine)

    t = service.create(
        "Update README", "Change the version badge in `src/main.py` line 5."
    )
    out = RefineStage().run(t, ctx)

    assert not refine_called
    assert out.next_state is State.READY
    assert "triage SKIP:" in out.note
    assert "doc-only change" in out.note


def test_prescriptive_spec_deterministic_skip(ctx, service, monkeypatch):
    """When a draft contains >50 lines of fenced code blocks, triage_skip
    short-circuits WITHOUT calling triage_refine — the draft is treated as
    an already-implementation-ready prescriptive spec."""
    from robotsix_mill.agents.refining import TriageResult
    from robotsix_mill.stages.refine._triage import _count_code_block_lines

    refine_called = False
    triage_called = False

    def fake_refine(**kw):
        nonlocal refine_called
        refine_called = True
        return _single("should not be called")

    def fake_triage(**kw):
        nonlocal triage_called
        triage_called = True
        return TriageResult(decision="REFINE", reason="should not be called")

    monkeypatch.setattr(refining, "run_refine_agent", fake_refine)
    monkeypatch.setattr(refining, "triage_refine", fake_triage)

    # Build a draft with exactly 50 code-block lines (at threshold).
    code_body = "\n".join(f"    line_{i:03d}()" for i in range(50))
    draft = f"## Problem\n\nExact implementation:\n\n```python\n{code_body}\n```"

    assert _count_code_block_lines(draft) == 50

    t = service.create("Add feature", draft)
    # Use the default ctx (require_approval=False) so the ticket
    # routes to READY, not HUMAN_ISSUE_APPROVAL.
    out = RefineStage().run(t, ctx)

    assert not triage_called, "triage_refine should NOT have been called"
    assert not refine_called, "run_refine_agent should NOT have been called"
    assert out.next_state is State.READY
    assert "prescriptive spec" in out.note


def test_prescriptive_spec_below_threshold_still_triages(ctx, service, monkeypatch):
    """A draft with <50 code-block lines should still go through triage."""
    from robotsix_mill.agents.refining import TriageResult
    from robotsix_mill.stages.refine._triage import _count_code_block_lines

    triage_called = False

    def fake_triage(**kw):
        nonlocal triage_called
        triage_called = True
        return TriageResult(decision="SKIP", reason="already precise enough")

    monkeypatch.setattr(refining, "triage_refine", fake_triage)
    monkeypatch.setattr(refining, "run_refine_agent", lambda **kw: _single("unused"))

    # Build a draft with only 3 code-block lines (small snippet).
    draft = "## Problem\n\nExample:\n\n```python\nx = 1\ny = 2\nz = 3\n```"

    assert _count_code_block_lines(draft) == 3

    t = service.create("Small snippet", draft)
    out = RefineStage().run(t, ctx)

    assert triage_called, "triage_refine SHOULD have been called"
    assert "triage SKIP" in out.note


def test_triage_skip_goes_to_human_issue_approval_when_gated(
    ctx, service, monkeypatch, repo_config
):
    """When triage returns SKIP and require_approval=True, the ticket
    transitions to HUMAN_ISSUE_APPROVAL."""
    from robotsix_mill.agents.refining import TriageResult

    monkeypatch.setattr(
        refining,
        "triage_refine",
        lambda **_: TriageResult(decision="SKIP", reason="config-only"),
    )
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single("unused"))

    t = service.create("Add env var", "Add FOO=bar to `src/config.py` line 42.")

    from robotsix_mill.config import Settings as S

    gated = S(data_dir=str(ctx.settings.data_dir), require_approval="true")
    gated_ctx = StageContext(settings=gated, service=service, repo_config=repo_config)
    out = RefineStage().run(t, gated_ctx)

    assert out.next_state is State.HUMAN_ISSUE_APPROVAL
    assert "triage SKIP:" in out.note


def test_triage_refine_calls_full_refine(ctx, service, monkeypatch):
    """When triage returns REFINE, run_refine_agent IS called normally."""
    from robotsix_mill.agents.refining import TriageResult

    refine_called = False

    def fake_triage(*, settings, title, draft, repo_dir=None, extra_roots=None):
        return TriageResult(
            decision="REFINE", reason="ambiguous scope, needs exploration"
        )

    def spy_refine(
        *,
        settings,
        title,
        draft,
        repo_dir=None,
        reviewer_comments=None,
        memory="",
        epic_context="",
        extra_roots=None,
        message_history=None,
        board_id="",
        **kwargs,
    ):
        nonlocal refine_called
        refine_called = True
        return _single("## Problem\nrefined\n")

    monkeypatch.setattr(refining, "triage_refine", fake_triage)
    monkeypatch.setattr(refining, "run_refine_agent", spy_refine)

    t = service.create("Add feature X", "make it work with the thing")
    out = RefineStage().run(t, ctx)

    assert refine_called
    assert out.next_state is State.READY
    assert out.note == "refined"


def test_triage_feature_flag_off_calls_full_refine(
    ctx, service, monkeypatch, repo_config
):
    """When refine_triage_enabled=False, triage_refine is never called
    and full refine runs."""
    refine_called = False
    triage_called = False

    def fake_triage(*, settings, title, draft, repo_dir=None, extra_roots=None):
        nonlocal triage_called
        triage_called = True
        from robotsix_mill.agents.refining import TriageResult

        return TriageResult(decision="SKIP", reason="should not be reached")

    def spy_refine(
        *,
        settings,
        title,
        draft,
        repo_dir=None,
        reviewer_comments=None,
        memory="",
        epic_context="",
        extra_roots=None,
        message_history=None,
        board_id="",
        **kwargs,
    ):
        nonlocal refine_called
        refine_called = True
        return _single("## Problem\nrefined\n")

    monkeypatch.setattr(refining, "triage_refine", fake_triage)
    monkeypatch.setattr(refining, "run_refine_agent", spy_refine)

    t = service.create("Update README", "Change the version badge in README.md line 5.")

    from robotsix_mill.config import Settings as S

    disabled = S(
        data_dir=str(ctx.settings.data_dir),
        refine_triage_enabled="false",
        require_approval="false",
    )
    disabled_ctx = StageContext(
        settings=disabled, service=service, repo_config=repo_config
    )
    out = RefineStage().run(t, disabled_ctx)

    assert not triage_called
    assert refine_called
    assert out.next_state is State.READY


def test_triage_sendback_always_refines(ctx, service, monkeypatch):
    """When the ticket has reviewer comments (sendback), triage is
    skipped and full refine runs even though the draft looks trivial."""
    from robotsix_mill.agents.refining import TriageResult

    refine_called = False
    triage_called = False

    def fake_triage(*, settings, title, draft, repo_dir=None, extra_roots=None):
        nonlocal triage_called
        triage_called = True
        return TriageResult(decision="SKIP", reason="should not be reached")

    def spy_refine(
        *,
        settings,
        title,
        draft,
        repo_dir=None,
        reviewer_comments=None,
        memory="",
        epic_context="",
        extra_roots=None,
        message_history=None,
        board_id="",
        **kwargs,
    ):
        nonlocal refine_called
        refine_called = True
        # Verify reviewer comments were passed through.
        assert reviewer_comments is not None
        assert "please fix x" in reviewer_comments
        return _single("## Problem\nrefined with feedback\n")

    monkeypatch.setattr(refining, "triage_refine", fake_triage)
    monkeypatch.setattr(refining, "run_refine_agent", spy_refine)

    t = service.create("Update README", "Change the version badge in README.md line 5.")
    # Add a reviewer comment to simulate sendback.
    service.add_comment(t.id, "please fix x")

    out = RefineStage().run(t, ctx)

    assert not triage_called
    assert refine_called
    assert out.next_state is State.READY


def test_triage_failure_falls_through_to_refine(ctx, service, monkeypatch):
    """When triage_refine raises, a warning is logged and full refine
    proceeds normally."""
    refine_called = False

    def boom_triage(*, settings, title, draft):
        raise RuntimeError("triage model down")

    def spy_refine(
        *,
        settings,
        title,
        draft,
        repo_dir=None,
        reviewer_comments=None,
        memory="",
        epic_context="",
        extra_roots=None,
        message_history=None,
        board_id="",
        **kwargs,
    ):
        nonlocal refine_called
        refine_called = True
        return _single("## Problem\nrefined\n")

    monkeypatch.setattr(refining, "triage_refine", boom_triage)
    monkeypatch.setattr(refining, "run_refine_agent", spy_refine)

    t = service.create("Add X", "make x happen")
    out = RefineStage().run(t, ctx)

    assert refine_called
    assert out.next_state is State.READY
    assert out.note == "refined"


# ---------------------------------------------------------------------------
# auto-approve triage tests
# ---------------------------------------------------------------------------


def test_auto_approve_approve_skips_human_gate(
    ctx, service, monkeypatch, tmp_path, repo_config
):
    """When triage_auto_approve returns APPROVE, the ticket goes straight
    to READY even when require_approval=true.  Uses a precise multi-file
    feature spec to demonstrate the relaxed criteria."""
    spec = (
        "## Problem\nUsers need to export their data in CSV format.\n"
        "## Scope\n- src/export/csv_writer.py: add write_csv() function\n"
        "- src/cli/export.py: wire --format csv flag\n"
        "- tests/export/test_csv_writer.py: add round-trip test\n"
        "## Acceptance criteria\n"
        "- [ ] write_csv() produces valid RFC 4180 CSV\n"
        "- [ ] --format csv flag triggers CSV export path\n"
        "- [ ] round-trip test passes: write then parse matches input\n"
    )

    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))
    monkeypatch.setattr(
        refining,
        "triage_auto_approve",
        lambda **_: refining.AutoApproveResult(
            decision="APPROVE",
            reason="precise multi-file feature, no design decisions",
        ),
    )

    gated = Settings(
        data_dir=str(tmp_path),
        require_approval="true",
        auto_approve_enabled="true",
    )
    gated_ctx = StageContext(settings=gated, service=service, repo_config=repo_config)

    t = service.create("CSV export", "add CSV export feature")
    out = RefineStage().run(t, gated_ctx)

    assert out.next_state is State.READY
    assert (
        "auto-approve: APPROVE — precise multi-file feature, no design decisions"
        in out.note
    )


def test_auto_approve_needs_approval_goes_to_human(
    ctx, service, monkeypatch, tmp_path, repo_config
):
    """When triage_auto_approve returns NEEDS_APPROVAL, the ticket goes to
    HUMAN_ISSUE_APPROVAL when gated.  The spec here is ambiguous about scope
    — the implementer would have to guess where to make changes."""
    spec = (
        "## Problem\nImprove error handling across the application.\n"
        "## Scope\n- Various files\n"
        "## Acceptance criteria\n- [ ] errors are handled better\n"
    )

    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))
    monkeypatch.setattr(
        refining,
        "triage_auto_approve",
        lambda **_: refining.AutoApproveResult(
            decision="NEEDS_APPROVAL",
            reason="ambiguous scope, unclear acceptance criteria",
        ),
    )

    gated = Settings(
        data_dir=str(tmp_path),
        require_approval="true",
        auto_approve_enabled="true",
    )
    gated_ctx = StageContext(settings=gated, service=service, repo_config=repo_config)

    t = service.create("Improve errors", "improve error handling")
    out = RefineStage().run(t, gated_ctx)

    assert out.next_state is State.HUMAN_ISSUE_APPROVAL
    assert (
        "auto-approve: NEEDS_APPROVAL — ambiguous scope, unclear acceptance criteria"
        in out.note
    )


def test_auto_approve_failure_falls_back_to_human(
    ctx, service, monkeypatch, tmp_path, repo_config
):
    """When triage_auto_approve raises, the ticket falls back to
    HUMAN_ISSUE_APPROVAL when gated."""
    spec = "## Problem\nFix typo in README\n## Scope\n- README.md line 5\n## Acceptance criteria\n- [ ] typo is fixed\n"

    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))
    monkeypatch.setattr(
        refining,
        "triage_auto_approve",
        lambda **_: (_ for _ in ()).throw(RuntimeError("auto-approve model down")),
    )

    gated = Settings(
        data_dir=str(tmp_path),
        require_approval="true",
        auto_approve_enabled="true",
    )
    gated_ctx = StageContext(settings=gated, service=service, repo_config=repo_config)

    t = service.create("Fix typo", "fix a typo in README.md")
    out = RefineStage().run(t, gated_ctx)

    assert out.next_state is State.HUMAN_ISSUE_APPROVAL
    assert "auto-approve: triage failed — falling back to human approval" in out.note


def test_auto_approve_flag_off_never_called(
    ctx, service, monkeypatch, tmp_path, repo_config
):
    """When auto_approve_enabled=false, triage_auto_approve is never called
    and the ticket follows normal gated behaviour."""
    spec = "## Problem\nFix typo in README\n## Scope\n- README.md line 5\n## Acceptance criteria\n- [ ] typo is fixed\n"

    auto_approve_called = False

    def fake_auto_approve(*, settings, spec):
        nonlocal auto_approve_called
        auto_approve_called = True
        return refining.AutoApproveResult(
            decision="APPROVE", reason="should not be reached"
        )

    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))
    monkeypatch.setattr(refining, "triage_auto_approve", fake_auto_approve)

    gated = Settings(
        data_dir=str(tmp_path),
        require_approval="true",
        auto_approve_enabled="false",
    )
    gated_ctx = StageContext(settings=gated, service=service, repo_config=repo_config)

    t = service.create("Fix typo", "fix a typo in README.md")
    out = RefineStage().run(t, gated_ctx)

    assert not auto_approve_called
    assert out.next_state is State.HUMAN_ISSUE_APPROVAL


def test_auto_approve_precise_multifile_feature_approved(
    ctx, service, monkeypatch, tmp_path, repo_config
):
    """A precise, well-specified multi-file feature spec with clear
    acceptance criteria → APPROVE, ticket goes to READY."""
    spec = (
        "## Problem\nAdd pagination to the list-endpoints response.\n"
        "## Scope\n"
        "- src/api/list.py: accept ?page= and ?per_page= query params\n"
        "- src/db/queries.py: add LIMIT/OFFSET to list queries\n"
        "- tests/api/test_list.py: test paginated responses\n"
        "## Acceptance criteria\n"
        "- [ ] GET /items?page=2&per_page=10 returns second page of 10 items\n"
        "- [ ] default per_page=20 when not specified\n"
        "- [ ] page < 1 returns 400\n"
    )

    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))
    monkeypatch.setattr(
        refining,
        "triage_auto_approve",
        lambda **_: refining.AutoApproveResult(
            decision="APPROVE",
            reason="precise multi-file feature, no design decisions",
        ),
    )

    gated = Settings(
        data_dir=str(tmp_path),
        require_approval="true",
        auto_approve_enabled="true",
    )
    gated_ctx = StageContext(settings=gated, service=service, repo_config=repo_config)

    t = service.create("Add pagination", "add pagination to list endpoints")
    out = RefineStage().run(t, gated_ctx)

    assert out.next_state is State.READY


def test_auto_approve_ambiguous_spec_needs_approval(
    ctx, service, monkeypatch, tmp_path, repo_config
):
    """A spec with ambiguous scope where the implementer would have to
    guess → NEEDS_APPROVAL, ticket goes to HUMAN_ISSUE_APPROVAL."""
    spec = (
        "## Problem\nMake the app faster.\n"
        "## Scope\n- Improve performance\n"
        "## Acceptance criteria\n- [ ] app is faster\n"
    )

    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))
    monkeypatch.setattr(
        refining,
        "triage_auto_approve",
        lambda **_: refining.AutoApproveResult(
            decision="NEEDS_APPROVAL",
            reason="ambiguous scope, implementer must guess",
        ),
    )

    gated = Settings(
        data_dir=str(tmp_path),
        require_approval="true",
        auto_approve_enabled="true",
    )
    gated_ctx = StageContext(settings=gated, service=service, repo_config=repo_config)

    t = service.create("Make faster", "make the app faster")
    out = RefineStage().run(t, gated_ctx)

    assert out.next_state is State.HUMAN_ISSUE_APPROVAL


def test_auto_approve_architecture_decision_needs_approval(
    ctx, service, monkeypatch, tmp_path, repo_config
):
    """A spec introducing a new abstraction/module boundary →
    NEEDS_APPROVAL, ticket goes to HUMAN_ISSUE_APPROVAL."""
    spec = (
        "## Problem\nIntroduce a plugin system so third-party extensions\n"
        "can hook into the request pipeline.\n"
        "## Scope\n"
        "- src/core/plugin.py: new Plugin base class and registry\n"
        "- src/core/pipeline.py: refactor to call plugin hooks\n"
        "- src/core/__init__.py: export plugin API as public interface\n"
        "## Acceptance criteria\n"
        "- [ ] plugins can register before_request and after_response hooks\n"
        "- [ ] hooks fire in registration order\n"
        "- [ ] a faulty plugin does not crash the pipeline\n"
    )

    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: _single(spec))
    monkeypatch.setattr(
        refining,
        "triage_auto_approve",
        lambda **_: refining.AutoApproveResult(
            decision="NEEDS_APPROVAL",
            reason="new plugin abstraction, public API change",
        ),
    )

    gated = Settings(
        data_dir=str(tmp_path),
        require_approval="true",
        auto_approve_enabled="true",
    )
    gated_ctx = StageContext(settings=gated, service=service, repo_config=repo_config)

    t = service.create("Plugin system", "add plugin system")
    out = RefineStage().run(t, gated_ctx)

    assert out.next_state is State.HUMAN_ISSUE_APPROVAL


# ---------------------------------------------------------------------------
# Classification-pin tests — new permissive auto-approve criteria
# ---------------------------------------------------------------------------


def _ctx(require_approval=True, auto_approve_enabled=False):
    from types import SimpleNamespace

    return SimpleNamespace(
        settings=SimpleNamespace(
            require_approval=require_approval,
            auto_approve_enabled=auto_approve_enabled,
        )
    )


def test_auto_approve_yaml_criteria_permissive_tie_breaker():
    """The auto-approve YAML must encode the permissive tie-breaker and the
    five NEEDS_APPROVAL gates. Protects against prompt regressions."""
    import yaml as _yaml
    from pathlib import Path

    text = (
        Path(__file__).parents[2] / "agent_definitions" / "auto-approve.yaml"
    ).read_text()
    data = _yaml.safe_load(text)
    prompt = data["system_prompt"]
    assert "when unsure, return APPROVE" in prompt
    prompt_lower = prompt.lower()
    for phrase in [
        "authentication",  # gate 1 — security
        "destructive",  # gate 2 — irreversible
        "cross-repo",  # gate 3 — infra/CI
        "public-api",  # gate 4 — breaking
        "external runtime dependency",  # gate 5 — new dep
    ]:
        assert phrase in prompt_lower, (
            f"Expected criteria phrase {phrase!r} missing from auto-approve.yaml"
        )


_ROUTINE_SPEC_CASES = [
    (
        "new_internal_module",
        "## Problem\nAdd ExportManager to handle CSV exports.\n"
        "## Scope\n- src/export/manager.py: new ExportManager class\n"
        "## Acceptance criteria\n- [ ] ExportManager.export() returns bytes\n",
    ),
    (
        "new_pydantic_schema",
        "## Problem\nAdd ExportConfig schema.\n"
        "## Scope\n- src/schemas/export.py: new Pydantic model\n"
        "## Acceptance criteria\n- [ ] ExportConfig validates required fields\n",
    ),
    (
        "ui_change",
        "## Problem\nUpdate button styling on the dashboard.\n"
        "## Scope\n- src/templates/dashboard.html: change CSS class\n"
        "## Acceptance criteria\n- [ ] Button shows correct colour\n",
    ),
    (
        "tests_only",
        "## Problem\nAdd missing tests for ExportManager.\n"
        "## Scope\n- tests/test_export_manager.py: five unit tests\n"
        "## Acceptance criteria\n- [ ] All five tests pass\n",
    ),
    (
        "docs_only",
        "## Problem\nDocument the new export endpoints.\n"
        "## Scope\n- docs/export.md: add usage section\n"
        "## Acceptance criteria\n- [ ] Section present and accurate\n",
    ),
    (
        "internal_endpoint",
        "## Problem\nAdd GET /internal/health endpoint.\n"
        "## Scope\n- src/routes/internal.py: register route\n"
        "## Acceptance criteria\n- [ ] 200 on GET /internal/health\n",
    ),
    (
        "refactor",
        "## Problem\nExtract _parse_csv helper from CsvImporter.\n"
        "## Scope\n- src/importers/csv.py: extract helper function\n"
        "## Acceptance criteria\n- [ ] All existing tests pass\n",
    ),
]

_HIGH_RISK_SPEC_CASES = [
    (
        "auth_secrets",
        "## Problem\nRotate JWT signing secret.\n"
        "## Scope\n- src/auth/jwt.py: update secret key handling\n"
        "## Acceptance criteria\n- [ ] New secret applied on startup\n",
    ),
    (
        "destructive_migration",
        "## Problem\nRemove legacy columns from users table.\n"
        "## Scope\n- migrations/0042_drop_columns.py: DROP COLUMN legacy_flag, legacy_data\n"
        "## Acceptance criteria\n- [ ] Columns removed; migration irreversible\n",
    ),
    (
        "cross_repo_ci",
        "## Problem\nUpdate shared CI deploy workflow.\n"
        "## Scope\n- .github/workflows/deploy.yml: change shared deploy step\n"
        "## Acceptance criteria\n- [ ] CI pipeline updated across all repos\n",
    ),
    (
        "breaking_public_api",
        "## Problem\nRemove deprecated public endpoint GET /api/v1/users.\n"
        "## Scope\n- src/api/v1/users.py: remove route; external callers must migrate\n"
        "## Acceptance criteria\n- [ ] Endpoint removed from public API\n",
    ),
    (
        "new_external_runtime_dep",
        "## Problem\nAdd cryptography package for FIPS-compliant hashing.\n"
        "## Scope\n- pyproject.toml: add cryptography>=41 to runtime dependencies\n"
        "## Acceptance criteria\n- [ ] cryptography importable at runtime\n",
    ),
]


@pytest.mark.parametrize("label,spec", _ROUTINE_SPEC_CASES)
def test_auto_approve_criteria_approve_routine(label, spec, monkeypatch):
    """Routine specs (new internal module / schema / UI / tests / docs /
    internal endpoint / refactor) must route to APPROVE under new criteria."""
    monkeypatch.setattr(
        refining,
        "triage_auto_approve",
        lambda *, settings, spec, **kw: refining.AutoApproveResult(
            decision="APPROVE", reason=f"routine: {label}"
        ),
    )
    state, note = refine_module._resolve_next_state(
        _ctx(auto_approve_enabled=True),
        spec,
        "t1",
    )
    assert state is State.READY, f"{label!r} expected APPROVE → READY, got {state}"
    assert "APPROVE" in (note or "")


@pytest.mark.parametrize("label,spec", _HIGH_RISK_SPEC_CASES)
def test_auto_approve_criteria_needs_approval_high_risk(label, spec, monkeypatch):
    """High-risk specs (auth, destructive, cross-repo CI, breaking public API,
    new external runtime dep) must route to NEEDS_APPROVAL under new criteria."""
    monkeypatch.setattr(
        refining,
        "triage_auto_approve",
        lambda *, settings, spec, **kw: refining.AutoApproveResult(
            decision="NEEDS_APPROVAL", reason=f"high-risk: {label}"
        ),
    )
    state, note = refine_module._resolve_next_state(
        _ctx(auto_approve_enabled=True),
        spec,
        "t1",
    )
    assert state is State.HUMAN_ISSUE_APPROVAL, (
        f"{label!r} expected NEEDS_APPROVAL → HUMAN_ISSUE_APPROVAL, got {state}"
    )
    assert "NEEDS_APPROVAL" in (note or "")
