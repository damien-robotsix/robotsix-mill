"""Unit tests for ``ValidationMixin`` in isolation.

``ValidationMixin`` (``src/robotsix_mill/stages/implement/validation.py``)
is otherwise exercised only indirectly through the full-stage integration
tests in ``test_implement.py``.  These tests pin its standalone heuristics
— prerequisite gating, baseline-fix bookkeeping, scope-guardrail routing —
and the pure ``smoke_paths_match`` gate, with git/sandbox/LLM seams mocked.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from robotsix_mill.agents.testing import smoke_paths_match
from robotsix_mill.core.models import SourceKind
from robotsix_mill.core.states import State
from robotsix_mill.stages.implement import validation as validation_mod
from robotsix_mill.stages.implement.validation import ValidationMixin


# ---------------------------------------------------------------------------
# _baseline_fix_title — deterministic, pure
# ---------------------------------------------------------------------------


def test_baseline_fix_title_uses_short_sha_and_branch():
    title = ValidationMixin._baseline_fix_title(
        SimpleNamespace(), "abcdef1234567890", "main"
    )
    assert title == "baseline: pre-existing test failures — main abcdef12"


def test_baseline_fix_title_stable_for_same_inputs():
    # Two callers (idempotency guard + spawn) must derive the SAME title.
    a = ValidationMixin._baseline_fix_title(
        SimpleNamespace(), "deadbeefcafe", "develop"
    )
    b = ValidationMixin._baseline_fix_title(
        SimpleNamespace(), "deadbeefcafe", "develop"
    )
    assert a == b == "baseline: pre-existing test failures — develop deadbeef"


# ---------------------------------------------------------------------------
# _baseline_fix_already_resolved
# ---------------------------------------------------------------------------


def _service_with_deps(deps_by_id):
    """Build a fake ctx.service exposing _parse_depends_on + get."""
    return SimpleNamespace(
        _parse_depends_on=lambda ticket: list(deps_by_id.keys()),
        get=lambda dep_id: deps_by_id.get(dep_id),
    )


def _dep(dep_id, *, title, state, source=SourceKind.IMPLEMENT_BASELINE_DEPENDENCY):
    return SimpleNamespace(id=dep_id, title=title, state=state, source=source)


def test_baseline_fix_already_resolved_returns_done_dep_id():
    title = "baseline: pre-existing test failures — main abcdef12"
    deps = {"fix-1": _dep("fix-1", title=title, state=State.DONE)}
    ctx = SimpleNamespace(service=_service_with_deps(deps))
    assert (
        ValidationMixin._baseline_fix_already_resolved(ctx, SimpleNamespace(), title)
        == "fix-1"
    )


def test_baseline_fix_already_resolved_accepts_closed_state():
    title = "baseline: pre-existing test failures — main abcdef12"
    deps = {"fix-1": _dep("fix-1", title=title, state=State.CLOSED)}
    ctx = SimpleNamespace(service=_service_with_deps(deps))
    assert (
        ValidationMixin._baseline_fix_already_resolved(ctx, SimpleNamespace(), title)
        == "fix-1"
    )


def test_baseline_fix_already_resolved_ignores_unfinished_or_mismatched():
    title = "baseline: pre-existing test failures — main abcdef12"
    deps = {
        # right title, still in progress → not resolved
        "fix-open": _dep("fix-open", title=title, state=State.READY),
        # done, but different title (different base sha) → not a match
        "fix-other": _dep(
            "fix-other",
            title="baseline: pre-existing test failures — main 99999999",
            state=State.DONE,
        ),
        # done + right title but wrong source kind → not a baseline dep
        "fix-wrong-source": _dep(
            "fix-wrong-source",
            title=title,
            state=State.DONE,
            source=SourceKind.USER,
        ),
    }
    ctx = SimpleNamespace(service=_service_with_deps(deps))
    assert (
        ValidationMixin._baseline_fix_already_resolved(ctx, SimpleNamespace(), title)
        is None
    )


def test_baseline_fix_already_resolved_no_deps():
    ctx = SimpleNamespace(service=_service_with_deps({}))
    assert (
        ValidationMixin._baseline_fix_already_resolved(
            ctx, SimpleNamespace(), "any-title"
        )
        is None
    )


# ---------------------------------------------------------------------------
# _run_prerequisite_gate
# ---------------------------------------------------------------------------


def _prereq_ctx():
    return SimpleNamespace(repo_config=SimpleNamespace(sandbox_image=None))


def test_prerequisite_gate_disabled_is_noop(monkeypatch):
    called = False

    def _boom(*a, **kw):
        nonlocal called
        called = True
        raise AssertionError("checker must not run when gate disabled")

    monkeypatch.setattr(validation_mod.prerequisite, "run_prerequisite_check", _boom)
    out = ValidationMixin._run_prerequisite_gate(
        _prereq_ctx(),
        SimpleNamespace(id="T-1"),
        "spec",
        Path("/repo"),
        SimpleNamespace(prerequisite_gate_enabled=False),
    )
    assert out is None
    assert called is False


def test_prerequisite_gate_blocks_on_unmet(monkeypatch):
    monkeypatch.setattr(
        validation_mod.prerequisite,
        "run_prerequisite_check",
        lambda *a, **kw: {"unmet": ["symbol foo.bar", "import baz"]},
    )
    out = ValidationMixin._run_prerequisite_gate(
        _prereq_ctx(),
        SimpleNamespace(id="T-1"),
        "spec",
        Path("/repo"),
        SimpleNamespace(prerequisite_gate_enabled=True),
    )
    assert out is not None
    assert out.next_state is State.BLOCKED
    assert "foo.bar" in out.note and "baz" in out.note


def test_prerequisite_gate_proceeds_when_all_met(monkeypatch):
    monkeypatch.setattr(
        validation_mod.prerequisite,
        "run_prerequisite_check",
        lambda *a, **kw: {"unmet": []},
    )
    out = ValidationMixin._run_prerequisite_gate(
        _prereq_ctx(),
        SimpleNamespace(id="T-1"),
        "spec",
        Path("/repo"),
        SimpleNamespace(prerequisite_gate_enabled=True),
    )
    assert out is None


def test_prerequisite_gate_swallows_checker_error(monkeypatch):
    def _raise(*a, **kw):
        raise RuntimeError("sandbox blew up")

    monkeypatch.setattr(validation_mod.prerequisite, "run_prerequisite_check", _raise)
    out = ValidationMixin._run_prerequisite_gate(
        _prereq_ctx(),
        SimpleNamespace(id="T-1"),
        "spec",
        Path("/repo"),
        SimpleNamespace(prerequisite_gate_enabled=True),
    )
    # Best-effort: a checker fault must never block the ticket.
    assert out is None


# ---------------------------------------------------------------------------
# _run_scope_guardrail — deterministic branches (no LLM)
# ---------------------------------------------------------------------------


def _scope_ctx():
    return SimpleNamespace(
        repo_config=SimpleNamespace(),
        service=SimpleNamespace(add_step_event=lambda *a, **kw: None),
    )


def _scope_settings(**over):
    base = dict(scope_triage_enabled=False, scope_triage_max_files=0)
    base.update(over)
    return SimpleNamespace(**base)


def _call_guardrail(ctx, settings, file_map, ticket_id="T-1"):
    return ValidationMixin._run_scope_guardrail(
        ctx,
        SimpleNamespace(id=ticket_id),
        Path("/repo"),
        "branch",
        "summary",
        None,
        file_map,
        settings,
        "spec",
        None,
    )


def test_scope_guardrail_skips_when_no_file_map(monkeypatch):
    monkeypatch.setattr(validation_mod, "target_branch_for", lambda *a: "main")
    res = _call_guardrail(_scope_ctx(), _scope_settings(), file_map=None)
    assert res.action == "skip_iteration"
    assert res.outcome is None


def test_scope_guardrail_passes_when_all_changes_in_scope(monkeypatch):
    monkeypatch.setattr(validation_mod, "target_branch_for", lambda *a: "main")
    monkeypatch.setattr(validation_mod.git_ops, "introduced_files", lambda *a: ["a.py"])
    res = _call_guardrail(_scope_ctx(), _scope_settings(), file_map={"a.py"})
    assert res.action == "skip_iteration"
    assert res.outcome is None


def test_scope_guardrail_honours_directory_prefix_entries(monkeypatch):
    # A trailing-"/" file_map entry covers every file under it.
    monkeypatch.setattr(validation_mod, "target_branch_for", lambda *a: "main")
    monkeypatch.setattr(
        validation_mod.git_ops,
        "introduced_files",
        lambda *a: [".deps/pkg/x.py", ".deps/pkg/y.py"],
    )
    res = _call_guardrail(_scope_ctx(), _scope_settings(), file_map={".deps/"})
    assert res.action == "skip_iteration"


def test_scope_guardrail_blocks_out_of_scope_when_triage_disabled(monkeypatch):
    monkeypatch.setattr(validation_mod, "target_branch_for", lambda *a: "main")
    monkeypatch.setattr(
        validation_mod.git_ops, "introduced_files", lambda *a: ["a.py", "b.py"]
    )
    # Treat the stray file as text so it isn't auto-cleaned as an artifact.
    monkeypatch.setattr(validation_mod, "_is_binary_artifact", lambda *a: False)
    finalized = {}
    monkeypatch.setattr(
        ValidationMixin,
        "_finalize",
        classmethod(lambda cls, *a, **kw: finalized.update(ok=kw.get("ok"))),
        raising=False,
    )
    res = _call_guardrail(
        _scope_ctx(), _scope_settings(scope_triage_enabled=False), file_map={"a.py"}
    )
    assert res.action == "return"
    assert res.outcome.next_state is State.BLOCKED
    assert "b.py" in res.outcome.note
    assert finalized == {"ok": False}


def test_scope_guardrail_flood_guard_blocks_without_llm(monkeypatch):
    monkeypatch.setattr(validation_mod, "target_branch_for", lambda *a: "main")
    flood = [f"gen/file_{i}.py" for i in range(5)]
    monkeypatch.setattr(validation_mod.git_ops, "introduced_files", lambda *a: flood)
    monkeypatch.setattr(validation_mod, "_is_binary_artifact", lambda *a: False)
    monkeypatch.setattr(
        ValidationMixin,
        "_finalize",
        classmethod(lambda cls, *a, **kw: None),
        raising=False,
    )

    def _no_triage(*a, **kw):
        raise AssertionError("flood guard must skip the scope-triage LLM")

    from robotsix_mill.agents import scope_triage as st

    monkeypatch.setattr(st, "run_scope_triage_agent", _no_triage)
    res = _call_guardrail(
        # triage enabled, but the flood guard fires first.
        _scope_ctx(),
        _scope_settings(scope_triage_enabled=True, scope_triage_max_files=2),
        file_map={"in_scope.py"},
    )
    assert res.action == "return"
    assert res.outcome.next_state is State.BLOCKED
    assert "flood guard" in res.outcome.note


# ---------------------------------------------------------------------------
# smoke_paths_match — pure path-scoped smoke gate
# ---------------------------------------------------------------------------


def test_smoke_paths_match_empty_globs_runs_unconditionally():
    assert smoke_paths_match(["any/file.py"], []) is True


def test_smoke_paths_match_shallow_glob_matches():
    assert (
        smoke_paths_match(
            ["src/robotsix_mill/runtime/static/board.css"],
            ["src/robotsix_mill/runtime/static/*.css"],
        )
        is True
    )


def test_smoke_paths_match_recursive_glob_matches():
    assert (
        smoke_paths_match(
            ["src/robotsix_mill/runtime/static/board.js"],
            ["src/robotsix_mill/runtime/**"],
        )
        is True
    )


def test_smoke_paths_match_no_overlap_returns_false():
    assert (
        smoke_paths_match(
            ["docs/readme.md", "src/robotsix_mill/core/models.py"],
            ["src/robotsix_mill/runtime/static/*.css"],
        )
        is False
    )
