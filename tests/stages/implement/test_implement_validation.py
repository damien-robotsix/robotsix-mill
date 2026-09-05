"""Unit tests for ``ValidationMixin`` in isolation.

``ValidationMixin`` (``src/robotsix_mill/stages/implement/validation.py``)
is otherwise exercised only indirectly through the full-stage integration
tests in ``test_implement.py``.  These tests pin its standalone heuristics
— prerequisite gating, baseline-fix bookkeeping, scope-guardrail routing —
and the pure ``smoke_paths_match`` gate, with git/sandbox/LLM seams mocked.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import robotsix_mill.stages.implement as implement_facade
from robotsix_mill.agents.testing import smoke_paths_match
from robotsix_mill.core.models import SourceKind
from robotsix_mill.core.states import State
from robotsix_mill.stages.base import Outcome
from robotsix_mill.stages.implement import validation as validation_mod
from robotsix_mill.stages.implement._shared import (
    _ScopeGuardrailResult,
)
from robotsix_mill.stages.implement.validation import (
    ValidationMixin,
)

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
    assert "foo.bar" in out.note
    assert "baz" in out.note


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
    base = {
        "scope_triage_enabled": False,
        "scope_triage_max_files": 0,
        # 0 = ceiling disabled, so existing cases exercise only the
        # newly-added count unless they opt in explicitly.
        "scope_triage_hard_max_files": 0,
    }
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
    # ``changed`` is now computed at the top of the method (for the
    # repo-settings write-path guard) before the no-file_map short-circuit,
    # so stub introduced_files like the sibling tests do.
    monkeypatch.setattr(validation_mod.git_ops, "introduced_files", lambda *a: [])
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


def _flood_case(monkeypatch, *, changed, pre_existing, **settings_kw):
    """Drive the guardrail with a given diff and a given origin/<target> tree."""
    monkeypatch.setattr(validation_mod, "target_branch_for", lambda *a: "main")
    monkeypatch.setattr(validation_mod.git_ops, "introduced_files", lambda *a: changed)
    monkeypatch.setattr(
        validation_mod.git_ops, "tracked_paths_at", lambda *a: set(pre_existing)
    )
    monkeypatch.setattr(validation_mod, "_is_binary_artifact", lambda *a: False)
    monkeypatch.setattr(
        ValidationMixin,
        "_finalize",
        classmethod(lambda cls, *a, **kw: None),
        raising=False,
    )
    triage_calls: list[int] = []
    from robotsix_mill.agents import scope_triage as st

    monkeypatch.setattr(
        st,
        "run_scope_triage_agent",
        lambda *a, **kw: (triage_calls.append(1), _TriageStub())[1],
    )
    kw = {"scope_triage_enabled": True, "scope_triage_max_files": 2}
    kw.update(settings_kw)
    res = _call_guardrail(_scope_ctx(), _scope_settings(**kw), file_map={"in_scope.py"})
    return res, triage_calls


def _TriageStub():
    """The real verdict type, so the routing that follows behaves normally.

    ESCALATE is the neutral outcome — it neither expands scope nor reverts
    files, which keeps these tests focused on whether triage ran at all.
    """
    from robotsix_mill.agents.scope_triage import ScopeTriageVerdict

    return ScopeTriageVerdict(action="ESCALATE", justification="stub")


def test_flood_guard_ignores_edits_to_pre_existing_files(monkeypatch):
    """A wide refactor is not an artifact flood.

    Removing a cross-cutting setting touches its schema, docs, CLI and every
    call site — 79 files on the ticket that exposed this. Every one already
    existed on origin/main, so none of them is flood evidence.
    """
    changed = [f"src/mod_{i}.py" for i in range(40)]
    res, triage_calls = _flood_case(monkeypatch, changed=changed, pre_existing=changed)
    assert "flood guard" not in (res.outcome.note or "" if res.outcome else "")
    assert triage_calls, "triage must run for a legitimate wide refactor"


def test_flood_guard_still_fires_on_newly_added_files(monkeypatch):
    """A real flood is new paths the branch introduced."""
    changed = [f"node_modules/pkg/file_{i}.js" for i in range(40)]
    res, _ = _flood_case(monkeypatch, changed=changed, pre_existing=["src/app.py"])
    assert res.action == "return"
    assert res.outcome.next_state is State.BLOCKED
    assert "NEWLY ADDED" in res.outcome.note


def test_flood_guard_message_reports_both_counts(monkeypatch):
    """The note must say how many were new vs how many were out of scope at
    all — otherwise the operator cannot tell which case they are looking at."""
    changed = [f"gen/new_{i}.py" for i in range(10)] + [
        f"src/old_{i}.py" for i in range(10)
    ]
    res, _ = _flood_case(
        monkeypatch,
        changed=changed,
        pre_existing=[f"src/old_{i}.py" for i in range(10)],
    )
    assert "10 NEWLY ADDED" in res.outcome.note
    assert "20 out-of-scope files total" in res.outcome.note


def test_flood_guard_hard_ceiling_fires_on_pre_existing_files(monkeypatch):
    """The prompt-overflow protection survives: a refactor big enough to
    swamp the triage prompt still stops, just at a far higher threshold."""
    changed = [f"src/mod_{i}.py" for i in range(30)]
    res, _ = _flood_case(
        monkeypatch,
        changed=changed,
        pre_existing=changed,
        scope_triage_hard_max_files=10,
    )
    assert res.action == "return"
    assert res.outcome.next_state is State.BLOCKED
    assert "hard ceiling" in res.outcome.note


def test_flood_guard_counts_everything_when_ls_tree_fails(monkeypatch):
    """An empty tracked set means 'unknown', not 'nothing is tracked'.

    Treating a git failure as 'no files pre-existed' would be fine here, but
    treating it as 'everything pre-existed' would silently disarm the guard.
    """
    changed = [f"src/mod_{i}.py" for i in range(40)]
    res, _ = _flood_case(monkeypatch, changed=changed, pre_existing=[])
    assert res.action == "return"
    assert res.outcome.next_state is State.BLOCKED


# ---------------------------------------------------------------------------
# _is_binary_artifact — binary detection heuristic
# ---------------------------------------------------------------------------


def test_is_binary_artifact_extension_match(tmp_path):
    # Known binary extension → True regardless of content.
    f = tmp_path / "lib.o"
    f.write_text("// C source pretending to be object file")
    assert validation_mod._is_binary_artifact(tmp_path, str(f), "main") is True


def test_is_binary_artifact_null_byte_in_untracked_file(tmp_path):
    # Untracked file (no prior git history) with a null byte → binary.
    f = tmp_path / "uv"
    f.write_bytes(b"\x7fELF\x00\x01\x02\x03")
    assert validation_mod._is_binary_artifact(tmp_path, str(f), "main") is True


def test_is_binary_artifact_no_null_byte_text_file(tmp_path):
    # Regular text file → not binary.
    f = tmp_path / "README.md"
    f.write_text("# Hello\n")
    assert validation_mod._is_binary_artifact(tmp_path, str(f), "main") is False


def test_is_binary_artifact_nonexistent_file(tmp_path):
    # File that doesn't exist on disk → not binary.
    assert (
        validation_mod._is_binary_artifact(
            tmp_path, str(tmp_path / "nonexistent"), "main"
        )
        is False
    )


def test_is_binary_artifact_osi_error_is_silent(tmp_path, monkeypatch):
    # When open() raises OSError, the function should return False (not crash).
    import builtins

    f = tmp_path / "unreadable"
    f.write_text("content")

    def _raising_open(file, *a, **kw):
        if str(file) == str(f):
            raise OSError("permission denied")
        return open(file, *a, **kw)

    monkeypatch.setattr(builtins, "open", _raising_open)
    # Extension check passes first, so use a path with no binary extension.
    assert validation_mod._is_binary_artifact(tmp_path, str(f), "main") is False


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


# ---------------------------------------------------------------------------
# _run_baseline_check — the pre-loop test gate on the base branch
# ---------------------------------------------------------------------------

_REMOTE_SHA = "remote-sha-aaaaaaaa"
_HEAD_SHA = "head-sha-bbbbbbbb"


def _baseline_ctx(tmp_path, *, deps_by_id=None, repo_config=None):
    """Fake ctx whose workspace.artifacts_dir is a real temp dir."""
    deps_by_id = deps_by_id or {}
    notes: list[tuple] = []
    service = SimpleNamespace(
        workspace=lambda ticket: SimpleNamespace(artifacts_dir=tmp_path),
        add_history_note=lambda tid, note: notes.append((tid, note)),
        _parse_depends_on=lambda ticket: list(deps_by_id.keys()),
        get=lambda dep_id: deps_by_id.get(dep_id),
    )
    ctx = SimpleNamespace(
        service=service,
        repo_config=repo_config if repo_config is not None else SimpleNamespace(),
    )
    ctx.history_notes = notes
    return ctx


def _install_baseline_seams(
    monkeypatch,
    *,
    remote_sha=_REMOTE_SHA,
    head_sha=_HEAD_SHA,
    target="main",
    test_result=(True, "ok"),
    raise_in_agent=False,
):
    """Patch the git/target/test-agent seams; return a recorder namespace."""
    monkeypatch.setattr(validation_mod, "target_branch_for", lambda *a: target)
    monkeypatch.setattr(
        validation_mod.git_ops, "remote_branch_sha", lambda *a: remote_sha
    )
    monkeypatch.setattr(validation_mod.git_ops, "head_sha", lambda *a: head_sha)
    checkouts: list = []
    monkeypatch.setattr(
        validation_mod.git_ops, "checkout", lambda repo, ref: checkouts.append(ref)
    )
    agent_calls: list[dict] = []

    def _fake_agent(**kwargs):
        agent_calls.append(kwargs)
        if raise_in_agent:
            raise RuntimeError("boom")
        return test_result

    # run_test_agent is resolved as ``_facade.run_test_agent`` where
    # _facade is the implement package.
    monkeypatch.setattr(implement_facade, "run_test_agent", _fake_agent)
    return SimpleNamespace(checkouts=checkouts, agent_calls=agent_calls)


def _install_spawn_finalize(monkeypatch):
    """Patch _spawn_baseline_fix + _finalize to focus on routing."""
    spawn_calls: list = []
    finalize_calls: list = []
    sentinel = Outcome(State.BLOCKED, "baseline blocked")

    def _spawn(cls, *a, **kw):
        spawn_calls.append((a, kw))
        return sentinel

    monkeypatch.setattr(
        ValidationMixin, "_spawn_baseline_fix", classmethod(_spawn), raising=False
    )
    monkeypatch.setattr(
        ValidationMixin,
        "_finalize",
        classmethod(lambda cls, *a, **kw: finalize_calls.append(kw)),
        raising=False,
    )
    return SimpleNamespace(
        spawn_calls=spawn_calls, finalize_calls=finalize_calls, sentinel=sentinel
    )


def _call_baseline(ctx, *, branch="feature", repo_dir=Path("/repo")):
    return ValidationMixin._run_baseline_check(
        ctx,
        SimpleNamespace(id="T-1"),
        repo_dir,
        branch,
        False,
        SimpleNamespace(),
    )


def _write_cache(tmp_path, base_sha, passed, diagnosis="cached diag"):
    (tmp_path / "baseline_check.json").write_text(
        json.dumps({"passed": passed, "diagnosis": diagnosis, "base_sha": base_sha}),
        encoding="utf-8",
    )


def test_baseline_check_idempotency_short_circuit(tmp_path, monkeypatch):
    # A completed baseline-fix this ticket depends on satisfies the gate.
    seams = _install_baseline_seams(monkeypatch)
    monkeypatch.setattr(
        ValidationMixin,
        "_baseline_fix_already_resolved",
        classmethod(lambda cls, ctx, ticket, title: "fix-1"),
    )
    ctx = _baseline_ctx(tmp_path)
    out = _call_baseline(ctx)
    assert out is None
    # history note recorded, test agent never run, no cache written
    assert ctx.history_notes
    assert "fix-1" in ctx.history_notes[0][1]
    assert seams.agent_calls == []
    assert not (tmp_path / "baseline_check.json").exists()


def test_baseline_check_cache_hit_same_sha_passed(tmp_path, monkeypatch):
    seams = _install_baseline_seams(monkeypatch)
    _write_cache(tmp_path, _REMOTE_SHA, True)
    out = _call_baseline(_baseline_ctx(tmp_path))
    assert out is None
    assert seams.agent_calls == []


def test_baseline_check_cache_hit_same_sha_failed(tmp_path, monkeypatch):
    seams = _install_baseline_seams(monkeypatch)
    rec = _install_spawn_finalize(monkeypatch)
    _write_cache(tmp_path, _REMOTE_SHA, False)
    out = _call_baseline(_baseline_ctx(tmp_path))
    assert out is rec.sentinel
    assert out.next_state is State.BLOCKED
    assert rec.spawn_calls
    assert seams.agent_calls == []


def test_baseline_check_cache_hit_sha_advanced_was_passing(tmp_path, monkeypatch):
    seams = _install_baseline_seams(monkeypatch)
    _write_cache(tmp_path, "old-stale-sha", True)
    out = _call_baseline(_baseline_ctx(tmp_path))
    # A passing baseline stays valid even after the base advances.
    assert out is None
    assert seams.agent_calls == []


def test_baseline_check_cache_hit_sha_advanced_was_failing_reruns(
    tmp_path, monkeypatch
):
    seams = _install_baseline_seams(monkeypatch, test_result=(True, "ok"))
    _write_cache(tmp_path, "old-stale-sha", False)
    out = _call_baseline(_baseline_ctx(tmp_path))
    # Base advanced + previously failing → re-run the gate.
    assert out is None
    assert len(seams.agent_calls) == 1


def test_baseline_check_cache_miss_run_pass(tmp_path, monkeypatch):
    seams = _install_baseline_seams(monkeypatch, test_result=(True, "all green"))
    out = _call_baseline(_baseline_ctx(tmp_path), branch="feature")
    assert out is None
    # cache persisted with the run result
    cache = json.loads((tmp_path / "baseline_check.json").read_text(encoding="utf-8"))
    assert cache == {
        "passed": True,
        "diagnosis": "all green",
        "base_sha": _REMOTE_SHA,
    }
    # base checked out, then branch restored
    assert seams.checkouts == [_REMOTE_SHA, "feature"]


def test_baseline_check_cache_miss_run_fail(tmp_path, monkeypatch):
    seams = _install_baseline_seams(monkeypatch, test_result=(False, "boom diag"))
    rec = _install_spawn_finalize(monkeypatch)
    out = _call_baseline(_baseline_ctx(tmp_path))
    assert out is rec.sentinel
    assert out.next_state is State.BLOCKED
    assert rec.finalize_calls
    assert rec.finalize_calls[0].get("ok") is False
    assert rec.spawn_calls
    cache = json.loads((tmp_path / "baseline_check.json").read_text(encoding="utf-8"))
    assert cache["passed"] is False
    assert seams.agent_calls


def test_baseline_check_restores_branch_when_agent_raises(tmp_path, monkeypatch):
    seams = _install_baseline_seams(monkeypatch, raise_in_agent=True)
    _install_spawn_finalize(monkeypatch)
    with pytest.raises(RuntimeError):
        _call_baseline(_baseline_ctx(tmp_path), branch="feature")
    # finally: restores the working branch; base was checked out first
    assert seams.checkouts[0] == _REMOTE_SHA
    assert seams.checkouts[-1] == "feature"
    # cache is not written when the agent blows up
    assert not (tmp_path / "baseline_check.json").exists()


def test_baseline_check_remote_sha_fallback_to_head(tmp_path, monkeypatch):
    seams = _install_baseline_seams(
        monkeypatch, remote_sha=None, head_sha=_HEAD_SHA, test_result=(True, "ok")
    )
    out = _call_baseline(_baseline_ctx(tmp_path))
    assert out is None
    # base_sha falls back to head_sha; base checkout targets the branch name
    cache = json.loads((tmp_path / "baseline_check.json").read_text(encoding="utf-8"))
    assert cache["base_sha"] == _HEAD_SHA
    assert seams.checkouts[0] == "main"


def test_baseline_check_corrupt_cache_treated_as_miss(tmp_path, monkeypatch):
    seams = _install_baseline_seams(monkeypatch, test_result=(True, "ok"))
    (tmp_path / "baseline_check.json").write_text("{not valid json", encoding="utf-8")
    out = _call_baseline(_baseline_ctx(tmp_path))
    # Falls through to running the gate rather than raising.
    assert out is None
    assert len(seams.agent_calls) == 1


def test_baseline_check_passes_retry_on_failure(tmp_path, monkeypatch):
    seams = _install_baseline_seams(monkeypatch, test_result=(True, "ok"))
    _call_baseline(_baseline_ctx(tmp_path))
    assert seams.agent_calls[0]["retry_on_failure"] is True


# ---------------------------------------------------------------------------
# _vendored_dep_roots — helper-level (real git repo via tmp_path)
# ---------------------------------------------------------------------------


def _git_init(repo: Path) -> None:
    """``git init`` + minimal config in *repo*."""
    import subprocess as sp

    sp.run(["git", "-C", str(repo), "init", "-b", "main"], check=True)
    sp.run(
        ["git", "-C", str(repo), "config", "user.email", "test@test"],
        check=True,
    )
    sp.run(
        ["git", "-C", str(repo), "config", "user.name", "Tester"],
        check=True,
    )


def _make_files(root: Path, files: list[str]) -> None:
    """Create each file (and its parent dirs) relative to *root*."""
    for f in files:
        fp = root / f
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(f"content of {f}", encoding="utf-8")


def _commit_all(repo: Path) -> None:
    """Stage everything and commit."""
    import subprocess as sp

    sp.run(["git", "-C", str(repo), "add", "."], check=True)
    sp.run(
        ["git", "-C", str(repo), "commit", "-m", "init"],
        check=True,
    )


def test_vendored_dep_roots_untracked_dist_info_signature(tmp_path):
    """Untracked dir with >=2 distinct .dist-info markers → returned."""
    _git_init(tmp_path)
    _make_files(
        tmp_path,
        [
            ".pkgs/anyio-4.0.dist-info/METADATA",
            ".pkgs/idna-3.0.dist-info/RECORD",
            ".pkgs/idna/__init__.py",
        ],
    )
    # Commit ONLY tracked.txt so .pkgs/ stays untracked.
    (tmp_path / "tracked.txt").write_text("tracked")
    import subprocess as sp

    sp.run(["git", "-C", str(tmp_path), "add", "tracked.txt"], check=True)
    sp.run(["git", "-C", str(tmp_path), "commit", "-m", "tracked only"], check=True)

    result = validation_mod._vendored_dep_roots(
        tmp_path,
        [
            ".pkgs/anyio-4.0.dist-info/METADATA",
            ".pkgs/idna-3.0.dist-info/RECORD",
            ".pkgs/idna/__init__.py",
        ],
        "main",
    )
    assert result == {".pkgs"}


def test_vendored_dep_roots_tracked_dir_not_returned(tmp_path):
    """Git-tracked dir with same vendored signature → NOT returned."""
    _git_init(tmp_path)
    _make_files(
        tmp_path,
        [
            ".pkgs/anyio-4.0.dist-info/METADATA",
            ".pkgs/idna-3.0.dist-info/RECORD",
            ".pkgs/idna/__init__.py",
        ],
    )
    _commit_all(tmp_path)

    result = validation_mod._vendored_dep_roots(
        tmp_path,
        [
            ".pkgs/anyio-4.0.dist-info/METADATA",
            ".pkgs/idna-3.0.dist-info/RECORD",
            ".pkgs/idna/__init__.py",
        ],
        "main",
    )
    assert result == set()


def test_vendored_dep_roots_normal_dir_no_markers(tmp_path):
    """Normal dir with .py files and no markers → NOT returned."""
    _git_init(tmp_path)
    _make_files(
        tmp_path,
        [
            "mypkg/module.py",
            "mypkg/utils.py",
        ],
    )
    # Commit ONLY tracked.txt so mypkg/ stays untracked.
    (tmp_path / "tracked.txt").write_text("tracked")
    import subprocess as sp

    sp.run(["git", "-C", str(tmp_path), "add", "tracked.txt"], check=True)
    sp.run(["git", "-C", str(tmp_path), "commit", "-m", "tracked only"], check=True)

    result = validation_mod._vendored_dep_roots(
        tmp_path,
        ["mypkg/module.py", "mypkg/utils.py"],
        "main",
    )
    assert result == set()


def test_vendored_dep_roots_top_level_files_skipped(tmp_path):
    """Top-level files (no '/') are never vendored roots."""
    _git_init(tmp_path)
    (tmp_path / "README.md").write_text("readme")
    _commit_all(tmp_path)

    result = validation_mod._vendored_dep_roots(
        tmp_path,
        ["README.md"],
        "main",
    )
    assert result == set()


def test_vendored_dep_roots_node_modules_is_strong_marker(tmp_path):
    """A single `node_modules` alone is sufficient (strong marker)."""
    _git_init(tmp_path)
    _make_files(
        tmp_path,
        [
            "frontend/node_modules/.package-lock.json",
            "frontend/node_modules/react/index.js",
            "frontend/src/app.tsx",
        ],
    )
    # Commit ONLY tracked.txt so frontend/ stays untracked.
    (tmp_path / "tracked.txt").write_text("tracked")
    import subprocess as sp

    sp.run(["git", "-C", str(tmp_path), "add", "tracked.txt"], check=True)
    sp.run(["git", "-C", str(tmp_path), "commit", "-m", "tracked only"], check=True)

    result = validation_mod._vendored_dep_roots(
        tmp_path,
        [
            "frontend/node_modules/.package-lock.json",
            "frontend/node_modules/react/index.js",
            "frontend/src/app.tsx",
        ],
        "main",
    )
    assert result == {"frontend"}


def test_vendored_dep_roots_single_dist_info_not_enough(tmp_path):
    """One .dist-info alone is insufficient (K=2 threshold)."""
    _git_init(tmp_path)
    _make_files(
        tmp_path,
        [
            ".deps/six-1.16.0.dist-info/METADATA",
            ".deps/six.py",
        ],
    )
    # Commit ONLY tracked.txt so .deps/ stays untracked.
    (tmp_path / "tracked.txt").write_text("tracked")
    import subprocess as sp

    sp.run(["git", "-C", str(tmp_path), "add", "tracked.txt"], check=True)
    sp.run(["git", "-C", str(tmp_path), "commit", "-m", "tracked only"], check=True)

    result = validation_mod._vendored_dep_roots(
        tmp_path,
        [
            ".deps/six-1.16.0.dist-info/METADATA",
            ".deps/six.py",
        ],
        "main",
    )
    assert result == set()


# ---------------------------------------------------------------------------
# _run_scope_guardrail — EXPAND cap (split proposal)
# ---------------------------------------------------------------------------


def test_scope_guardrail_expand_cap_blocks_and_splits(monkeypatch):
    """After 2 prior EXPAND events, the 3rd triggers a split proposal."""
    from robotsix_mill.agents.scope_triage import ScopeTriageVerdict

    monkeypatch.setattr(validation_mod, "target_branch_for", lambda *a: "main")
    monkeypatch.setattr(
        validation_mod.git_ops,
        "introduced_files",
        lambda *a: ["in_scope.py", "new_module.py"],
    )
    monkeypatch.setattr(validation_mod, "_is_binary_artifact", lambda *a: False)
    monkeypatch.setattr(validation_mod, "_vendored_dep_roots", lambda *a, **kw: set())
    monkeypatch.setattr(
        ValidationMixin,
        "_finalize",
        classmethod(lambda cls, *a, **kw: None),
        raising=False,
    )
    monkeypatch.setattr(validation_mod.git_ops, "tracked_paths_at", lambda *a: set())

    # Two prior EXPAND events in history.
    prior_events = [
        SimpleNamespace(note="scope-triage EXPAND: added `foo.py` (added: foo.py)"),
        SimpleNamespace(note="scope-triage EXPAND: added `bar.py` (added: bar.py)"),
    ]
    created_tickets: list[dict] = []
    transitioned: list[tuple] = []

    def _fake_create(title, desc, **kw):
        created_tickets.append({"title": title, "desc": desc, **kw})
        return SimpleNamespace(id=f"child-{len(created_tickets)}")

    def _fake_transition(tid, state, note=""):
        transitioned.append((tid, state, note))

    ctx = SimpleNamespace(
        repo_config=SimpleNamespace(),
        service=SimpleNamespace(
            add_step_event=lambda *a, **kw: None,
            history=lambda tid: prior_events,
            create=_fake_create,
            transition=_fake_transition,
        ),
    )

    def _expand_triage(*a, **kw):
        return ScopeTriageVerdict(
            action="EXPAND",
            justification="legitimate expansion",
            expand_files=["new_module.py"],
        )

    from robotsix_mill.agents import scope_triage as st

    monkeypatch.setattr(st, "run_scope_triage_agent", _expand_triage)

    res = ValidationMixin._run_scope_guardrail(
        ctx,
        SimpleNamespace(id="T-1", board_id=None, priority=False),
        Path("/repo"),
        "branch",
        "summary",
        None,
        {"in_scope.py"},
        _scope_settings(scope_triage_enabled=True, scope_triage_max_files=0),
        "spec",
        None,
    )
    assert res.action == "return"
    assert res.outcome is not None
    assert res.outcome.next_state is State.BLOCKED
    assert "EXPAND cap reached" in res.outcome.note
    assert "child-1" in res.outcome.note
    assert len(created_tickets) == 1
    assert "new_module.py" in created_tickets[0]["desc"]
    # A freshly-created child is already DRAFT — no transition is applied,
    # so no illegal draft -> draft transition is attempted.
    assert transitioned == []


def test_scope_guardrail_expand_cap_split_create_failure_does_not_block_parent(
    monkeypatch,
):
    """A scope-split child-creation failure must NOT FATALLY block the parent."""
    from robotsix_mill.agents.scope_triage import ScopeTriageVerdict

    monkeypatch.setattr(validation_mod, "target_branch_for", lambda *a: "main")
    monkeypatch.setattr(
        validation_mod.git_ops,
        "introduced_files",
        lambda *a: ["in_scope.py", "new_module.py"],
    )
    monkeypatch.setattr(validation_mod, "_is_binary_artifact", lambda *a: False)
    monkeypatch.setattr(validation_mod, "_vendored_dep_roots", lambda *a, **kw: set())
    monkeypatch.setattr(
        ValidationMixin,
        "_finalize",
        classmethod(lambda cls, *a, **kw: None),
        raising=False,
    )
    monkeypatch.setattr(validation_mod.git_ops, "tracked_paths_at", lambda *a: set())

    prior_events = [
        SimpleNamespace(note="scope-triage EXPAND: added `foo.py` (added: foo.py)"),
        SimpleNamespace(note="scope-triage EXPAND: added `bar.py` (added: bar.py)"),
    ]

    def _failing_create(*a, **kw):
        raise RuntimeError("db full")

    ctx = SimpleNamespace(
        repo_config=SimpleNamespace(),
        service=SimpleNamespace(
            add_step_event=lambda *a, **kw: None,
            history=lambda tid: prior_events,
            create=_failing_create,
        ),
    )

    def _expand_triage(*a, **kw):
        return ScopeTriageVerdict(
            action="EXPAND",
            justification="legitimate expansion",
            expand_files=["new_module.py"],
        )

    from robotsix_mill.agents import scope_triage as st

    monkeypatch.setattr(st, "run_scope_triage_agent", _expand_triage)

    res = ValidationMixin._run_scope_guardrail(
        ctx,
        SimpleNamespace(id="T-2", board_id=None, priority=False),
        Path("/repo"),
        "branch",
        "summary",
        None,
        {"in_scope.py"},
        _scope_settings(scope_triage_enabled=True, scope_triage_max_files=0),
        "spec",
        None,
    )
    # The failure is swallowed — the parent is still parked to BLOCKED
    # gracefully rather than raising a Fatal TransitionError.
    assert res.action == "return"
    assert res.outcome is not None
    assert res.outcome.next_state is State.BLOCKED
    assert "no scope-split child was spawned" in res.outcome.note


def test_scope_guardrail_expand_under_cap_proceeds(monkeypatch):
    """With fewer than 2 prior EXPAND events, expansion proceeds normally."""
    from robotsix_mill.agents.scope_triage import ScopeTriageVerdict

    monkeypatch.setattr(validation_mod, "target_branch_for", lambda *a: "main")
    monkeypatch.setattr(
        validation_mod.git_ops,
        "introduced_files",
        lambda *a: ["in_scope.py", "other.py"],
    )
    monkeypatch.setattr(validation_mod, "_is_binary_artifact", lambda *a: False)
    monkeypatch.setattr(validation_mod, "_vendored_dep_roots", lambda *a, **kw: set())
    monkeypatch.setattr(
        ValidationMixin,
        "_finalize",
        classmethod(lambda cls, *a, **kw: None),
        raising=False,
    )
    monkeypatch.setattr(validation_mod.git_ops, "tracked_paths_at", lambda *a: set())

    # Only one prior EXPAND event — under the cap.
    prior_events = [
        SimpleNamespace(note="scope-triage EXPAND: added `foo.py` (added: foo.py)"),
    ]

    ctx = SimpleNamespace(
        repo_config=SimpleNamespace(),
        service=SimpleNamespace(
            add_step_event=lambda *a, **kw: None,
            history=lambda tid: prior_events,
        ),
    )

    def _expand_triage(*a, **kw):
        return ScopeTriageVerdict(
            action="EXPAND",
            justification="legitimate expansion",
            expand_files=["new_module.py"],
        )

    from robotsix_mill.agents import scope_triage as st

    monkeypatch.setattr(st, "run_scope_triage_agent", _expand_triage)

    res = ValidationMixin._run_scope_guardrail(
        ctx,
        SimpleNamespace(id="T-1"),
        Path("/repo"),
        "branch",
        "summary",
        None,
        {"in_scope.py"},
        _scope_settings(scope_triage_enabled=True, scope_triage_max_files=0),
        "spec",
        None,
    )
    # Under cap → normal EXPAND flow (continue to re-run agent).
    assert res.action == "continue"
    assert "new_module.py" in res.file_map


# ---------------------------------------------------------------------------
# _run_scope_guardrail — vendored-dep filtering (guardrail-level)
# ---------------------------------------------------------------------------


def test_scope_guardrail_vendored_dep_flood_skips_guard(monkeypatch):
    """60 vendored files + low cap → skip_iteration, LLM never called."""
    monkeypatch.setattr(validation_mod, "target_branch_for", lambda *a: "main")

    # 60 files under `.local-packages/` — dist-info markers → vendored.
    flood = []
    for i in range(30):
        flood.append(f".local-packages/pkg{i}-{i}.0.dist-info/METADATA")
        flood.append(f".local-packages/pkg{i}-{i}.0.dist-info/RECORD")
    monkeypatch.setattr(validation_mod.git_ops, "introduced_files", lambda *a: flood)
    monkeypatch.setattr(validation_mod, "_is_binary_artifact", lambda *a: False)

    # Monkeypatch _vendored_dep_roots to avoid needing a real git repo.
    monkeypatch.setattr(
        validation_mod,
        "_vendored_dep_roots",
        lambda repo_dir, paths, target: {".local-packages"},
    )

    def _no_triage(*a, **kw):
        raise AssertionError("vendored-dep flood must skip the scope-triage LLM")

    from robotsix_mill.agents import scope_triage as st

    monkeypatch.setattr(st, "run_scope_triage_agent", _no_triage)

    res = ValidationMixin._run_scope_guardrail(
        SimpleNamespace(
            repo_config=SimpleNamespace(),
            service=SimpleNamespace(add_step_event=lambda *a, **kw: None),
        ),
        SimpleNamespace(id="T-1"),
        Path("/repo"),
        "branch",
        "summary",
        None,
        {"in_scope.py"},
        SimpleNamespace(
            scope_triage_enabled=True,
            scope_triage_max_files=2,
            scope_triage_hard_max_files=0,
        ),
        "spec",
        None,
    )
    assert res.action == "skip_iteration"
    assert res.outcome is None


def test_scope_guardrail_genuine_flood_still_blocks(monkeypatch):
    """60 real source files (non-vendored) → flood guard fires, BLOCKED."""
    monkeypatch.setattr(validation_mod, "target_branch_for", lambda *a: "main")

    flood = [f"gen/file_{i}.py" for i in range(60)]
    monkeypatch.setattr(validation_mod.git_ops, "introduced_files", lambda *a: flood)
    monkeypatch.setattr(validation_mod, "_is_binary_artifact", lambda *a: False)

    # _vendored_dep_roots returns empty → these are genuine files.
    monkeypatch.setattr(
        validation_mod,
        "_vendored_dep_roots",
        lambda repo_dir, paths, target: set(),
    )

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

    res = ValidationMixin._run_scope_guardrail(
        SimpleNamespace(
            repo_config=SimpleNamespace(),
            service=SimpleNamespace(add_step_event=lambda *a, **kw: None),
        ),
        SimpleNamespace(id="T-1"),
        Path("/repo"),
        "branch",
        "summary",
        None,
        {"in_scope.py"},
        SimpleNamespace(
            scope_triage_enabled=True,
            scope_triage_max_files=10,
            scope_triage_hard_max_files=0,
        ),
        "spec",
        None,
    )
    assert res.action == "return"
    assert res.outcome.next_state is State.BLOCKED
    assert "flood guard" in res.outcome.note


def test_scope_guardrail_vendored_dirs_logged(monkeypatch, caplog):
    """Vendored dirs are logged via log.info AND add_step_event."""
    import logging

    caplog.set_level(logging.INFO, logger="robotsix_mill.stages.implement")
    monkeypatch.setattr(validation_mod, "target_branch_for", lambda *a: "main")

    flood = [
        ".pip-packages/anyio-4.0.dist-info/METADATA",
        ".pip-packages/idna-3.0.dist-info/RECORD",
        ".pip-packages/idna/__init__.py",
    ]
    monkeypatch.setattr(validation_mod.git_ops, "introduced_files", lambda *a: flood)
    monkeypatch.setattr(validation_mod, "_is_binary_artifact", lambda *a: False)
    monkeypatch.setattr(
        validation_mod,
        "_vendored_dep_roots",
        lambda repo_dir, paths, target: {".pip-packages"},
    )

    step_events: list[tuple] = []

    res = ValidationMixin._run_scope_guardrail(
        SimpleNamespace(
            repo_config=SimpleNamespace(),
            service=SimpleNamespace(
                add_step_event=lambda tid, msg: step_events.append((tid, msg))
            ),
        ),
        SimpleNamespace(id="T-vendored"),
        Path("/repo"),
        "branch",
        "summary",
        None,
        {"in_scope.py"},
        SimpleNamespace(
            scope_triage_enabled=True,
            scope_triage_max_files=0,
            scope_triage_hard_max_files=0,
        ),
        "spec",
        None,
    )
    # All vendored files removed → skip_iteration (empty out_of_scope).
    assert res.action == "skip_iteration"

    # Check caplog.
    log_msgs = [r.message for r in caplog.records if r.levelname == "INFO"]
    assert any("auto-ignored vendored-dep dir" in m for m in log_msgs)
    assert any(".pip-packages" in m for m in log_msgs)
    assert any("T-vendored" in m for m in log_msgs)

    # Check step events.
    assert len(step_events) == 1
    assert "auto-ignored vendored-dep dir" in step_events[0][1]
    assert ".pip-packages" in step_events[0][1]


# ---------------------------------------------------------------------------
# _run_scope_guardrail — standard-config auto-revert
# ---------------------------------------------------------------------------


def test_scope_guardrail_standard_config_auto_reverted(monkeypatch):
    """Standard config files out-of-scope are auto-reverted, others remain."""
    monkeypatch.setattr(validation_mod, "target_branch_for", lambda *a: "main")

    # .pre-commit-config.yaml and docker-compose.yml are standard configs;
    # extra_file.py is a genuine out-of-scope file.
    introduced = [
        ".pre-commit-config.yaml",
        "docker-compose.yml",
        "extra_file.py",
    ]
    monkeypatch.setattr(
        validation_mod.git_ops, "introduced_files", lambda *a: introduced
    )
    monkeypatch.setattr(validation_mod, "_is_binary_artifact", lambda *a: False)

    # _vendored_dep_roots returns empty → files pass through vendored filter.
    monkeypatch.setattr(
        validation_mod,
        "_vendored_dep_roots",
        lambda repo_dir, paths, target: set(),
    )

    # Capture git checkout calls.
    checkout_calls: list[list[str]] = []

    def _fake_checkout(args, **kwargs):
        checkout_calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(validation_mod.subprocess, "run", _fake_checkout)

    step_events: list[tuple] = []

    # Short-circuit scope-triage LLM — we only care that the standard
    # config files were reverted before the remaining file reaches triage.
    monkeypatch.setattr(
        ValidationMixin,
        "_run_scope_triage_classification",
        classmethod(
            lambda cls, *a, **kw: _ScopeGuardrailResult(
                action="return",
                outcome=Outcome(State.BLOCKED, "scope violation: extra_file.py"),
                file_map={"in_scope.py"},
            )
        ),
        raising=False,
    )

    res = ValidationMixin._run_scope_guardrail(
        SimpleNamespace(
            repo_config=SimpleNamespace(),
            service=SimpleNamespace(
                add_step_event=lambda tid, msg: step_events.append((tid, msg))
            ),
        ),
        SimpleNamespace(id="T-stdcfg"),
        Path("/repo"),
        "branch",
        "summary",
        None,
        {"in_scope.py"},
        SimpleNamespace(
            scope_triage_enabled=True,
            scope_triage_max_files=10,
            scope_triage_hard_max_files=0,
        ),
        "spec",
        None,
    )

    # Verify git checkout was called for both standard config files.
    # The recorder sees every git invocation, so filter to checkouts — the
    # guard also runs an ls-tree to tell new files from pre-existing ones.
    checkout_calls = [a for a in checkout_calls if "checkout" in a]
    assert len(checkout_calls) == 2
    # args: ["git", "-C", "/repo", "checkout", "origin/main", "--", "<path>"]
    checked_out = {args[6] for args in checkout_calls}
    assert checked_out == {".pre-commit-config.yaml", "docker-compose.yml"}

    # Verify step event was logged.
    assert any("auto-REVERT (standard config)" in ev[1] for ev in step_events)
    assert any(".pre-commit-config.yaml" in ev[1] for ev in step_events)
    assert any("docker-compose.yml" in ev[1] for ev in step_events)

    # extra_file.py should have been passed through to triage (mocked to
    # return BLOCKED above).
    assert res.action == "return"
    assert "extra_file.py" in res.outcome.note


def test_scope_guardrail_standard_config_all_reverted_skips_guard(monkeypatch):
    """When ALL out-of-scope files are standard configs, skip_iteration."""
    monkeypatch.setattr(validation_mod, "target_branch_for", lambda *a: "main")

    introduced = [".pre-commit-config.yaml", "docker-compose.yml", "mkdocs.yml"]
    monkeypatch.setattr(
        validation_mod.git_ops, "introduced_files", lambda *a: introduced
    )
    monkeypatch.setattr(validation_mod, "_is_binary_artifact", lambda *a: False)
    monkeypatch.setattr(
        validation_mod,
        "_vendored_dep_roots",
        lambda repo_dir, paths, target: set(),
    )

    def _fake_checkout(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(validation_mod.subprocess, "run", _fake_checkout)

    res = ValidationMixin._run_scope_guardrail(
        SimpleNamespace(
            repo_config=SimpleNamespace(),
            service=SimpleNamespace(add_step_event=lambda *a, **kw: None),
        ),
        SimpleNamespace(id="T-stdcfg-all"),
        Path("/repo"),
        "branch",
        "summary",
        None,
        {"in_scope.py"},
        SimpleNamespace(
            scope_triage_enabled=True,
            scope_triage_max_files=10,
            scope_triage_hard_max_files=0,
        ),
        "spec",
        None,
    )
    # All files were standard configs → skip_iteration.
    assert res.action == "skip_iteration"
    assert res.outcome is None


# ---------------------------------------------------------------------------
# classify_baseline_verdict — pure decision helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ci_conclusion", "network_dependent", "expected"),
    [
        # CI green + sandbox fail → proceed
        ("success", True, "proceed"),
        ("success", False, "proceed"),
        # CI red + sandbox fail → block
        ("failure", True, "block"),
        ("failure", False, "block"),
        # CI unknown (None) + network-error signature → proceed
        (None, True, "proceed"),
        # CI unknown (None) + real assertion failure → block
        (None, False, "block"),
        # CI pending + network-error → proceed
        ("pending", True, "proceed"),
        # CI pending + real failure → block
        ("pending", False, "block"),
    ],
)
def test_classify_baseline_verdict(ci_conclusion, network_dependent, expected):
    assert (
        validation_mod.classify_baseline_verdict(ci_conclusion, network_dependent)
        == expected
    )


# ---------------------------------------------------------------------------
# _run_baseline_check — CI cross-check integration tests
# ---------------------------------------------------------------------------


def test_baseline_check_ci_green_overrides_sandbox_fail(tmp_path, monkeypatch):
    """When sandbox fails but GitHub CI is green → proceed with warning."""
    _install_baseline_seams(
        monkeypatch,
        test_result=(
            False,
            "JSONDecodeError: Expecting value: line 1 column 1 (char 0)",
        ),
    )
    ctx = _baseline_ctx(tmp_path)

    # Stub forge.commit_ci_conclusion to return green.
    monkeypatch.setattr(
        validation_mod,
        "get_forge",
        lambda *a, **kw: SimpleNamespace(
            commit_ci_conclusion=lambda sha: {
                "conclusion": "success",
                "failing": [],
                "pending": [],
            }
        ),
    )

    out = _call_baseline(ctx)
    # Must proceed (return None), not block.
    assert out is None

    # Cache must be written as passing.
    cache_path = tmp_path / "baseline_check.json"
    assert cache_path.exists()
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cache["passed"] is True
    assert cache["base_sha"] == _REMOTE_SHA
    assert "GitHub CI" in cache["diagnosis"]

    # History note must be recorded.
    assert ctx.history_notes
    assert any("GitHub CI" in note for _, note in ctx.history_notes)


def test_baseline_check_ci_red_still_blocks(tmp_path, monkeypatch):
    """When sandbox fails AND GitHub CI is red → still BLOCKED."""
    _install_baseline_seams(monkeypatch, test_result=(False, "some real failure"))
    rec = _install_spawn_finalize(monkeypatch)
    ctx = _baseline_ctx(tmp_path)

    monkeypatch.setattr(
        validation_mod,
        "get_forge",
        lambda *a, **kw: SimpleNamespace(
            commit_ci_conclusion=lambda sha: {
                "conclusion": "failure",
                "failing": [{"name": "CI"}],
                "pending": [],
            }
        ),
    )

    out = _call_baseline(ctx)
    assert out is rec.sentinel
    assert out.next_state is State.BLOCKED

    # Cache must be written as failing (original behavior preserved).
    cache_path = tmp_path / "baseline_check.json"
    assert cache_path.exists()
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cache["passed"] is False


def test_baseline_check_ci_unavailable_network_signature_proceeds(
    tmp_path, monkeypatch
):
    """CI unavailable + network-error signature → proceed."""
    _install_baseline_seams(
        monkeypatch,
        test_result=(False, "httpx.ConnectError: Connection refused"),
    )
    ctx = _baseline_ctx(tmp_path)

    monkeypatch.setattr(
        validation_mod,
        "get_forge",
        lambda *a, **kw: SimpleNamespace(commit_ci_conclusion=lambda sha: None),
    )

    out = _call_baseline(ctx)
    assert out is None
    cache_path = tmp_path / "baseline_check.json"
    assert cache_path.exists()
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cache["passed"] is True
    assert "network-error" in cache["diagnosis"]


def test_baseline_check_ci_unavailable_real_failure_blocks(tmp_path, monkeypatch):
    """CI unavailable + real assertion failure → block (conservative)."""
    _install_baseline_seams(
        monkeypatch,
        test_result=(False, "AssertionError: assert 1 == 2"),
    )
    rec = _install_spawn_finalize(monkeypatch)
    ctx = _baseline_ctx(tmp_path)

    monkeypatch.setattr(
        validation_mod,
        "get_forge",
        lambda *a, **kw: SimpleNamespace(commit_ci_conclusion=lambda sha: None),
    )

    out = _call_baseline(ctx)
    assert out is rec.sentinel
    assert out.next_state is State.BLOCKED
    assert rec.finalize_calls
    assert rec.spawn_calls


def test_baseline_check_forge_raises_treated_as_unavailable(tmp_path, monkeypatch):
    """When forge.commit_ci_conclusion raises, treat as CI unavailable."""
    _install_baseline_seams(
        monkeypatch,
        test_result=(False, "ConnectError: Connection refused"),
    )
    ctx = _baseline_ctx(tmp_path)

    monkeypatch.setattr(
        validation_mod,
        "get_forge",
        lambda *a, **kw: SimpleNamespace(
            commit_ci_conclusion=lambda sha: (_ for _ in ()).throw(RuntimeError("boom"))
        ),
    )

    out = _call_baseline(ctx)
    # CI unavailable + network signature → proceed
    assert out is None
    cache_path = tmp_path / "baseline_check.json"
    assert cache_path.exists()
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cache["passed"] is True


# ---------------------------------------------------------------------------
# _run_baseline_check — write_spec_fingerprint=False propagation
# ---------------------------------------------------------------------------


def test_baseline_check_passes_write_spec_fingerprint_false(tmp_path, monkeypatch):
    """_run_baseline_check must call _finalize with
    write_spec_fingerprint=False so baseline failures don't persist a
    spec fingerprint that would trigger the stale-re-spawn guard on
    the next preflight."""
    _install_baseline_seams(monkeypatch, test_result=(False, "boom diag"))
    rec = _install_spawn_finalize(monkeypatch)
    _call_baseline(_baseline_ctx(tmp_path))

    # _finalize must have been called.
    assert rec.finalize_calls
    # The write_spec_fingerprint kwarg must be False.
    finalize_kw = rec.finalize_calls[0]
    assert finalize_kw.get("write_spec_fingerprint") is False, (
        f"_run_baseline_check must pass write_spec_fingerprint=False, "
        f"got: {finalize_kw}"
    )


# ---------------------------------------------------------------------------
# _revert_standard_configs — the spec carve-out
# ---------------------------------------------------------------------------


class TestStandardConfigSpecCarveOut:
    """A standard config file the spec names is the deliverable, not drift."""

    @staticmethod
    def _repo(tmp_path: Path) -> Path:
        repo = tmp_path / "r"
        repo.mkdir()

        def run(*a: str) -> None:
            subprocess.run(
                ["git", "-C", str(repo), *a], check=True, capture_output=True
            )

        run("init", "-q", "-b", "main")
        run("config", "user.email", "t@t")
        run("config", "user.name", "t")
        (repo / "docker-compose.yml").write_text("services:\n  app: {}\n")
        run("add", "-A")
        run("commit", "-qm", "base")
        run("update-ref", "refs/remotes/origin/main", "HEAD")
        # The ticket's actual work.
        (repo / "docker-compose.yml").write_text(
            "services:\n  app:\n    healthcheck:\n      test: [CMD, true]\n"
        )
        return repo

    def _run(self, tmp_path, spec):
        repo = self._repo(tmp_path)
        events: list[str] = []
        ctx = SimpleNamespace(
            service=SimpleNamespace(add_step_event=lambda _tid, msg: events.append(msg))
        )
        ticket = SimpleNamespace(id="t1")
        remaining, skip = ValidationMixin._revert_standard_configs(
            ctx,
            ticket,
            repo,
            "main",
            ["docker-compose.yml"],
            {"src/app.py"},
            None,
            spec,
        )
        content = (repo / "docker-compose.yml").read_text()
        return remaining, skip, events, content

    def test_spec_named_compose_survives_and_reaches_scope_triage(self, tmp_path):
        """The whole deliverable must not be reverted out from under the ticket.

        Live: hexarchy e5c4 ("Add service-level healthcheck stanza to root
        docker-compose.yml") burned five attempts across eight days because
        every pass reverted the one file it existed to change.
        """
        spec = "Add a healthcheck stanza to docker-compose.yml for local dev."
        remaining, skip, events, content = self._run(tmp_path, spec)

        assert remaining == ["docker-compose.yml"], "must fall through to triage"
        assert skip is None
        assert events == [], "no auto-revert event"
        assert "healthcheck" in content, "the ticket's edit must survive"

    def test_unnamed_compose_is_still_auto_reverted(self, tmp_path):
        """Incidental scaffolding regeneration keeps its cheap auto-revert."""
        spec = "Rename the archive handler in src/app.py."
        remaining, skip, events, content = self._run(tmp_path, spec)

        assert remaining == []
        assert skip is not None and skip.action == "skip_iteration"
        assert len(events) == 1 and "auto-REVERT" in events[0]
        assert "healthcheck" not in content, "scaffolding drift must be undone"


# ---------------------------------------------------------------------------
# _run_scope_guardrail — triage agent error routing
# ---------------------------------------------------------------------------


def _triage_raising(monkeypatch, exc):
    """Wire the guardrail so the diff has one out-of-scope file and the
    scope-triage agent raises *exc*."""
    monkeypatch.setattr(validation_mod, "target_branch_for", lambda *a: "main")
    monkeypatch.setattr(
        validation_mod.git_ops, "introduced_files", lambda *a: ["a.py", "b.py"]
    )
    monkeypatch.setattr(validation_mod, "_is_binary_artifact", lambda *a: False)
    monkeypatch.setattr(
        validation_mod.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(stdout="+x"),
    )
    finalized = {}
    monkeypatch.setattr(
        ValidationMixin,
        "_finalize",
        classmethod(lambda cls, *a, **kw: finalized.update(ok=kw.get("ok"))),
        raising=False,
    )
    from robotsix_mill.agents import scope_triage as st

    def _raise(*a, **kw):
        raise exc

    monkeypatch.setattr(st, "run_scope_triage_agent", _raise)
    return finalized


def _run_triage_guardrail():
    return _call_guardrail(
        _scope_ctx(),
        _scope_settings(scope_triage_enabled=True, scope_triage_max_files=10),
        file_map={"a.py"},
    )


def test_scope_triage_usage_exhaustion_propagates_to_worker(monkeypatch):
    """Claude quota exhaustion is infrastructure: the exception must reach
    the worker (which parks until the reset) instead of becoming a BLOCKED
    'scope-triage agent error' note (ticket ...-c504, 2026-08-30)."""

    class ClaudeSDKUsageExhaustedError(RuntimeError):
        pass

    exc = ClaudeSDKUsageExhaustedError(
        "You've hit your session limit · resets 12pm (UTC)"
    )
    finalized = _triage_raising(monkeypatch, exc)
    with pytest.raises(ClaudeSDKUsageExhaustedError):
        _run_triage_guardrail()
    assert finalized == {}, "must not finalize the branch for an infra error"


def test_scope_triage_cooldown_exhausted_propagates_to_worker(monkeypatch):
    """An llmio tier in cooldown with no fallback left is a model outage
    (ticket ...-b346, 2026-08-29), not a scope verdict."""
    exc = RuntimeError(
        "scope triage: level2 is in cooldown and fallback depth (2) exhausted"
    )
    _triage_raising(monkeypatch, exc)
    with pytest.raises(RuntimeError, match="cooldown"):
        _run_triage_guardrail()


def test_scope_triage_structured_output_failure_still_escalates(monkeypatch):
    """A genuine agent failure keeps today's ESCALATE-to-human behaviour."""
    exc = ValueError("structured output did not validate against ScopeTriageVerdict")
    finalized = _triage_raising(monkeypatch, exc)
    res = _run_triage_guardrail()
    assert res.action == "return"
    assert res.outcome.next_state is State.BLOCKED
    assert "scope-triage agent error (ValueError:" in res.outcome.note
    assert "b.py" in res.outcome.note
    assert finalized == {"ok": False}
