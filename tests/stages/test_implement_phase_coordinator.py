"""Unit tests for ``PhaseCoordinatorMixin`` in isolation.

These exercise the orchestration methods that actually live in
``src/robotsix_mill/stages/implement/phase_coordinator.py`` — the bounded
fix loop / circuit breaker (``_implement_loop``), artifact/context loading
(``_load_implement_context``), the memory-board resolver
(``_memory_board_id``), the pause router (``_maybe_handle_pause``), and the
artifact-persistence/commit step (``_finalize``).

The single-pass collaborator (``_run_single_implement_pass``, which lives on
the sibling ``ImplementationLogicMixin``) and the sandbox/git/pause seams are
mocked; a REAL ``TicketService`` (per-test SQLite) and a REAL ``Workspace``
are used, per repo convention. No full ``ImplementStage().run(...)`` flow is
driven here — that is covered by ``tests/stages/test_implement.py``.
"""

import json
import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from robotsix_mill.agents import coding
from robotsix_mill.agents.testing import ENV_ERROR_PREFIX
from robotsix_mill.core import db
from robotsix_mill.core.models import TicketKind
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.base import Outcome
from robotsix_mill.stages.implement import ImplementStage
from robotsix_mill.stages.implement import phase_coordinator as pc
from robotsix_mill.stages.implement._shared import (
    _ImplementContext,
    _SinglePassResult,
)


# --- fixtures / helpers (copied from tests/stages/test_implement.py) ------


@pytest.fixture
def ctx_factory(tmp_path, fake_sandbox):
    from robotsix_mill.config import Settings

    created = []

    def make(**env):
        db.reset_engine()
        s = Settings(data_dir=str(tmp_path / f"data{len(created)}"), **env)
        db.init_db(s, board_id="test-board")
        svc = TicketService(s, board_id="test-board")
        created.append(s)
        from robotsix_mill.config import RepoConfig

        return StageContext(
            settings=s,
            service=svc,
            repo_config=RepoConfig(
                repo_id="test-repo",
                langfuse_project_name="test",
                langfuse_public_key="pk-test",
                langfuse_secret_key="sk-test",
            ),
        )

    yield make
    db.reset_engine()


def _ticket(ctx, title="Add feature", body="Please add feature.txt"):
    t = ctx.service.create(title, body)
    ctx.service.transition(t.id, State.READY)
    return ctx.service.get(t.id)


# --- shared test doubles for the loop -------------------------------------


def _ic(**over):
    """Build an ``_ImplementContext`` with sensible defaults."""
    defaults = dict(
        spec="spec",
        memory_text="",
        reference_files=None,
        file_map=None,
        feedback=None,
        previous_attempt_summary=None,
        open_thread_ids=None,
    )
    defaults.update(over)
    return _ImplementContext(**defaults)


class _PassRecorder:
    """Stand-in for ``_run_single_implement_pass`` — returns queued results
    and records the ``ic`` threaded into each call."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []  # the ``ic`` passed to each invocation, in order

    def __call__(
        self,
        ctx,
        ticket,
        repo_dir,
        branch,
        settings,
        ic,
        attempt,
        max_iters,
        resume_history,
        resuming,
        extra_roots=None,
    ):
        self.calls.append(ic)
        return self.results[len(self.calls) - 1]


class _FinalizeRecorder:
    """Stand-in for ``_finalize`` — records each call's keyword args."""

    def __init__(self):
        self.calls = []

    def __call__(
        self,
        ctx,
        ticket,
        repo_dir,
        branch,
        summary,
        *,
        ok,
        reference_files=None,
        extra_roots=None,
    ):
        self.calls.append(
            {"summary": summary, "ok": ok, "reference_files": reference_files}
        )


def _setup_loop(monkeypatch, results, base_ic=None):
    """Patch the three loop collaborators on ``ImplementStage`` and return
    ``(pass_recorder, finalize_recorder)``."""
    rec = _PassRecorder(results)
    fin = _FinalizeRecorder()
    ic = base_ic if base_ic is not None else _ic()
    monkeypatch.setattr(ImplementStage, "_run_single_implement_pass", rec)
    monkeypatch.setattr(
        ImplementStage,
        "_load_implement_context",
        lambda ctx, ticket, settings: ic,
    )
    monkeypatch.setattr(ImplementStage, "_finalize", fin)
    return rec, fin


# --- 1. _implement_loop: routing & circuit breaker ------------------------


@pytest.mark.parametrize(
    "action,state",
    [
        ("return", State.DOCUMENTING),
        ("pause", State.AWAITING_USER_REPLY),
        ("proceed", State.DOCUMENTING),
        ("escalate", State.BLOCKED),
    ],
)
def test_loop_terminal_actions_return_outcome_directly(
    action, state, ctx_factory, tmp_path, monkeypatch
):
    """``return`` / ``pause`` / ``proceed`` / ``escalate`` each return the
    pass's ``outcome`` directly with no further passes and no finalize."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    settings = SimpleNamespace(max_fix_iterations=5)
    outcome = Outcome(state, "x")
    rec, fin = _setup_loop(
        monkeypatch,
        [_SinglePassResult(next_action=action, outcome=outcome)],
    )

    out = ImplementStage._implement_loop(ctx, t, tmp_path, "mill/x", False, settings)

    assert out is outcome
    assert len(rec.calls) == 1
    assert fin.calls == []


def test_loop_retry_threads_updated_ic(ctx_factory, tmp_path, monkeypatch):
    """On ``retry`` with a non-None ``result.ic``, the updated context
    replaces ``ic`` for the next pass."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    settings = SimpleNamespace(max_fix_iterations=3)
    new_ic = _ic(spec="updated-spec", feedback="diag-a")
    results = [
        _SinglePassResult(next_action="retry", feedback="diag-a", ic=new_ic),
        _SinglePassResult(next_action="proceed", outcome=Outcome(State.DOCUMENTING)),
    ]
    rec, fin = _setup_loop(monkeypatch, results)

    out = ImplementStage._implement_loop(ctx, t, tmp_path, "mill/x", False, settings)

    assert out.next_state is State.DOCUMENTING
    assert len(rec.calls) == 2
    # First pass got the base ic; the second got the retry's updated ic.
    assert rec.calls[1] is new_ic
    assert rec.calls[0] is not new_ic


def test_loop_zero_iterations_runs_single_pass(ctx_factory, tmp_path, monkeypatch):
    """``max_fix_iterations == 0`` floors to exactly ONE pass via
    ``max(1, …)``."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    settings = SimpleNamespace(max_fix_iterations=0)
    outcome = Outcome(State.DOCUMENTING)
    rec, fin = _setup_loop(
        monkeypatch,
        [_SinglePassResult(next_action="proceed", outcome=outcome)],
    )

    out = ImplementStage._implement_loop(ctx, t, tmp_path, "mill/x", False, settings)

    assert len(rec.calls) == 1
    assert out is outcome


def test_loop_exhausts_iterations_defensive_fallback(
    ctx_factory, tmp_path, monkeypatch
):
    """Every pass returns ``retry`` with a DISTINCT diagnosis (no circuit
    breaker fires) → loop exhausts ``max_iters`` and falls through to the
    defensive fallback: ``_finalize(ok=False)`` + BLOCKED-resumable."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    settings = SimpleNamespace(max_fix_iterations=3)
    results = [
        _SinglePassResult(next_action="retry", feedback=f"diag-{i}") for i in range(3)
    ]
    rec, fin = _setup_loop(monkeypatch, results)

    out = ImplementStage._implement_loop(ctx, t, tmp_path, "mill/x", False, settings)

    assert len(rec.calls) == 3  # pass count == max(1, max_fix_iterations)
    assert out.next_state is State.BLOCKED
    assert out.note == "implement loop exhausted — resumable"
    assert len(fin.calls) == 1
    assert fin.calls[0]["ok"] is False


def test_loop_env_error_two_repeat_short_circuits(ctx_factory, tmp_path, monkeypatch):
    """Two consecutive ``retry`` passes carrying an identical
    ``ENV_ERROR_PREFIX`` diagnosis (read from ``result.feedback``) short-
    circuit to BLOCKED before ``max_iters`` is reached."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    settings = SimpleNamespace(max_fix_iterations=8)
    env_diag = f"{ENV_ERROR_PREFIX} command not found in sandbox: 'yamllint'"
    results = [
        _SinglePassResult(next_action="retry", feedback=env_diag),
        _SinglePassResult(next_action="retry", feedback=env_diag),
    ]
    rec, fin = _setup_loop(monkeypatch, results)

    out = ImplementStage._implement_loop(ctx, t, tmp_path, "mill/x", False, settings)

    assert len(rec.calls) == 2  # short-circuited at the 2nd identical env-error
    assert out.next_state is State.BLOCKED
    assert "environment failure not fixable by code edits" in out.note
    assert len(fin.calls) == 1
    assert fin.calls[0]["ok"] is False


def test_loop_triple_identical_diag_short_circuits(ctx_factory, tmp_path, monkeypatch):
    """Three consecutive ``retry`` passes with the SAME non-empty, non-env
    diagnosis (read from ``result.ic.feedback`` when ``result.feedback`` is
    None) short-circuit to BLOCKED mentioning 'identical diagnosis'."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    settings = SimpleNamespace(max_fix_iterations=8)
    diag = "test_foo assertion failed: expected 1 got 2"
    ic_with_diag = _ic(feedback=diag)
    results = [
        _SinglePassResult(next_action="retry", feedback=None, ic=ic_with_diag)
        for _ in range(3)
    ]
    rec, fin = _setup_loop(monkeypatch, results)

    out = ImplementStage._implement_loop(ctx, t, tmp_path, "mill/x", False, settings)

    assert len(rec.calls) == 3  # short-circuited after 3 identical diagnoses
    assert out.next_state is State.BLOCKED
    assert "identical diagnosis" in out.note
    assert len(fin.calls) == 1
    assert fin.calls[0]["ok"] is False


def test_loop_empty_diag_does_not_trip_triple_repeat(
    ctx_factory, tmp_path, monkeypatch
):
    """An empty-string diagnosis must NOT trip the triple-repeat guard:
    three empty ``retry`` passes fall through to the exhaustion fallback,
    not the 'identical diagnosis' short-circuit."""
    ctx = ctx_factory()
    t = _ticket(ctx)
    settings = SimpleNamespace(max_fix_iterations=3)
    results = [_SinglePassResult(next_action="retry", feedback="") for _ in range(3)]
    rec, fin = _setup_loop(monkeypatch, results)

    out = ImplementStage._implement_loop(ctx, t, tmp_path, "mill/x", False, settings)

    assert len(rec.calls) == 3
    assert out.next_state is State.BLOCKED
    # Exhaustion fallback — NOT the identical-diagnosis short-circuit.
    assert out.note == "implement loop exhausted — resumable"
    assert fin.calls[0]["ok"] is False


# --- 2. _load_implement_context: artifact/context loading -----------------


def test_load_context_file_map_present(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)
    ws.write_description("the spec")
    (ws.artifacts_dir / "file_map.json").write_text(
        json.dumps([{"file": "a.py"}, {"file": "b.py"}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(pc, "load_memory", lambda p: "MEMTEXT")

    ic = ImplementStage._load_implement_context(ctx, t, ctx.settings)

    assert ic.file_map == {"a.py", "b.py"}
    assert ic.spec == "the spec"
    assert ic.memory_text == "MEMTEXT"


def test_load_context_file_map_absent_warns(ctx_factory, monkeypatch, caplog):
    ctx = ctx_factory()
    t = _ticket(ctx)
    monkeypatch.setattr(pc, "load_memory", lambda p: "")

    with caplog.at_level(logging.WARNING, logger="robotsix_mill.stages.implement"):
        ic = ImplementStage._load_implement_context(ctx, t, ctx.settings)

    assert ic.file_map is None
    assert any("skipping scope enforcement" in m for m in caplog.messages), (
        f"expected file_map-skip warning, got: {caplog.messages}"
    )


def test_load_context_file_map_empty_is_none(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "file_map.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(pc, "load_memory", lambda p: "")

    ic = ImplementStage._load_implement_context(ctx, t, ctx.settings)

    assert ic.file_map is None


def test_load_context_reference_files(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)
    payload = [{"path": "base_class.py"}, {"path": "wip.txt"}]
    (ws.artifacts_dir / "reference_files.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    monkeypatch.setattr(pc, "load_memory", lambda p: "")

    ic = ImplementStage._load_implement_context(ctx, t, ctx.settings)

    assert ic.reference_files == payload


def test_load_context_reference_files_absent(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx)
    monkeypatch.setattr(pc, "load_memory", lambda p: "")

    ic = ImplementStage._load_implement_context(ctx, t, ctx.settings)

    assert ic.reference_files is None


def test_load_context_previous_summary(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "implement_summary.md").write_text(
        "  prior pass summary \n", encoding="utf-8"
    )
    monkeypatch.setattr(pc, "load_memory", lambda p: "")

    ic = ImplementStage._load_implement_context(ctx, t, ctx.settings)

    assert ic.previous_attempt_summary == "prior pass summary"


def test_load_context_previous_summary_absent(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx)
    monkeypatch.setattr(pc, "load_memory", lambda p: "")

    ic = ImplementStage._load_implement_context(ctx, t, ctx.settings)

    assert ic.previous_attempt_summary is None


def test_load_context_epic_prepended(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    epic = ctx.service.create(
        "Global Epic", "High-level goal: unify UX", kind=TicketKind.EPIC
    )
    child = ctx.service.create("Add dark mode", "child body", parent_id=epic.id)
    ctx.service.transition(child.id, State.READY)
    child = ctx.service.get(child.id)
    ws = ctx.service.workspace(child)
    ws.write_description("CHILD SPEC")
    monkeypatch.setattr(pc, "load_memory", lambda p: "")

    ic = ImplementStage._load_implement_context(ctx, child, ctx.settings)

    epic_ctx = ctx.service.get_epic_context(child)
    assert epic_ctx  # non-empty
    assert ic.spec == epic_ctx + "\n\n" + "CHILD SPEC"


def test_load_context_feedback_filters_mill_system(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx)
    ctx.service.add_comment(t.id, "real review feedback", author="reviewer")
    ctx.service.add_comment(t.id, "mill trace breadcrumb", author="mill")
    ctx.service.add_comment(t.id, "system escalation ping", author="system")
    monkeypatch.setattr(pc, "load_memory", lambda p: "")

    ic = ImplementStage._load_implement_context(ctx, t, ctx.settings)

    assert ic.feedback is not None
    assert "real review feedback" in ic.feedback
    assert "mill trace breadcrumb" not in ic.feedback
    assert "system escalation ping" not in ic.feedback
    # The open root comment populates open_thread_ids.
    assert ic.open_thread_ids is not None
    assert len(ic.open_thread_ids) == 1


def test_load_context_blocked_resume_skips_feedback(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx)
    ctx.service.add_comment(t.id, "real review feedback", author="reviewer")
    t.blocked_from = "READY"  # mark as a BLOCKED resume → no comments read
    monkeypatch.setattr(pc, "load_memory", lambda p: "")

    ic = ImplementStage._load_implement_context(ctx, t, ctx.settings)

    assert ic.feedback is None
    assert ic.open_thread_ids is None


def test_load_context_memory_wiring(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx)
    captured = {}

    def _fake_load_memory(path):
        captured["path"] = path
        return "THE MEMORY"

    monkeypatch.setattr(pc, "load_memory", _fake_load_memory)

    ic = ImplementStage._load_implement_context(ctx, t, ctx.settings)

    assert ic.memory_text == "THE MEMORY"
    assert captured["path"] == ctx.settings.memory_file_for("implement", "test-board")


# --- 3. _memory_board_id --------------------------------------------------


def test_memory_board_id_uses_repo_config(ctx_factory):
    ctx = ctx_factory()
    t = _ticket(ctx)
    assert ImplementStage._memory_board_id(ctx, t) == ctx.repo_config.repo_id
    assert ImplementStage._memory_board_id(ctx, t) == "test-board"


def test_memory_board_id_meta_uses_ticket_board(ctx_factory):
    ctx = ctx_factory()
    t = _ticket(ctx)
    ctx.repo_config = None  # meta board: no registered repo_config
    t.board_id = "meta"
    assert ImplementStage._memory_board_id(ctx, t) == "meta"


# --- 4. _maybe_handle_pause -----------------------------------------------


def test_maybe_handle_pause_no_pause_returns_none(ctx_factory, tmp_path, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)
    monkeypatch.setattr(pc, "check_for_pause", lambda new_msgs: False)
    saved = []
    monkeypatch.setattr(pc, "save_conversation_state", lambda *a, **kw: saved.append(a))
    fin = _FinalizeRecorder()
    monkeypatch.setattr(ImplementStage, "_finalize", fin)

    res = ImplementStage._maybe_handle_pause(
        ctx, t, tmp_path, "mill/x", ws, "summary", None, b"state", b"msgs", None
    )

    assert res is None
    assert saved == []
    assert fin.calls == []
    assert ctx.service.get(t.id).state is State.READY


def test_maybe_handle_pause_pauses(ctx_factory, tmp_path, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)
    monkeypatch.setattr(pc, "check_for_pause", lambda new_msgs: True)
    saved = []
    monkeypatch.setattr(
        pc,
        "save_conversation_state",
        lambda ws_, conv, name: saved.append((ws_, conv, name)),
    )
    fin = _FinalizeRecorder()
    monkeypatch.setattr(ImplementStage, "_finalize", fin)

    res = ImplementStage._maybe_handle_pause(
        ctx, t, tmp_path, "mill/x", ws, "the summary", ["a.py"], b"CONV", b"MSGS", None
    )

    assert saved == [(ws, b"CONV", "implement")]
    assert len(fin.calls) == 1
    assert fin.calls[0]["ok"] is False
    assert ctx.service.get(t.id).state is State.AWAITING_USER_REPLY
    assert res.next_action == "pause"
    assert res.outcome.next_state is State.AWAITING_USER_REPLY


# --- 5. _finalize: artifact persistence & commit --------------------------


class _FakeGitOps:
    """Records ``commit_all`` calls; ``has_changes`` answers from a set of
    repo-dir paths (as strings) known to have changes."""

    def __init__(self, changed):
        self.changed = {str(p) for p in changed}
        self.commits = []

    def has_changes(self, repo):
        return str(repo) in self.changed

    def commit_all(self, repo, message):
        self.commits.append((str(repo), message))


def test_finalize_writes_artifacts_and_commits(ctx_factory, tmp_path, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    fake = _FakeGitOps(changed={repo_dir})
    monkeypatch.setattr(pc, "git_ops", fake)

    ImplementStage._finalize(
        ctx,
        t,
        repo_dir,
        "mill/x",
        "the summary",
        ok=True,
        reference_files=["a.py", "b.py"],
    )

    impl = (ws.artifacts_dir / "implement.md").read_text(encoding="utf-8")
    assert "passed" in impl
    assert "branch: mill/x" in impl
    assert "the summary" in impl

    ref = json.loads((ws.artifacts_dir / "reference_files.json").read_text())
    assert ref == [{"path": "a.py"}, {"path": "b.py"}]

    assert (ws.artifacts_dir / "implement_summary.md").read_text() == "the summary"

    # has_changes True → commit_all with the non-WIP message.
    assert fake.commits == [(str(repo_dir), f"mill: {t.title} ({t.id})")]


def test_finalize_blocked_header_and_no_changes(ctx_factory, tmp_path, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    fake = _FakeGitOps(changed=set())  # nothing changed
    monkeypatch.setattr(pc, "git_ops", fake)

    ImplementStage._finalize(
        ctx, t, repo_dir, "mill/y", "sum", ok=False, reference_files=None
    )

    impl = (ws.artifacts_dir / "implement.md").read_text(encoding="utf-8")
    assert "BLOCKED — resumable" in impl

    # reference_files None → empty list.
    ref = json.loads((ws.artifacts_dir / "reference_files.json").read_text())
    assert ref == []

    # has_changes False → commit_all NOT called.
    assert fake.commits == []


def test_finalize_extra_roots_writes_touched_repos(ctx_factory, tmp_path, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)
    primary = tmp_path / "primary"
    primary.mkdir()
    extra = tmp_path / "extra"
    extra.mkdir()
    fake = _FakeGitOps(changed={primary, extra})
    monkeypatch.setattr(pc, "git_ops", fake)

    ImplementStage._finalize(
        ctx,
        t,
        primary,
        "mill/z",
        "sum",
        ok=True,
        reference_files=None,
        extra_roots=[primary, extra],
    )

    tr = json.loads((ws.artifacts_dir / "touched_repos.json").read_text())
    ids = {e["repo_id"] for e in tr}
    assert ids == {"primary", "extra"}
    for entry in tr:
        assert entry["branch"] == "mill/z"
    # Both repos with changes were committed.
    assert {c[0] for c in fake.commits} == {str(primary), str(extra)}


# --- 5a. _finalize: towncrier fragment generation -------------------------


def test_finalize_generates_towncrier_fragment_when_configured(
    ctx_factory, tmp_path, monkeypatch
):
    """When ``pyproject.toml`` has ``[tool.towncrier]`` with
    ``directory = "changes"``, _finalize creates
    ``changes/<ticket_id>.misc.md`` containing the ticket title."""
    ctx = ctx_factory()
    t = _ticket(ctx, title="Implement foo bar baz")
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "pyproject.toml").write_text(
        '[tool.towncrier]\ndirectory = "changes"\n',
        encoding="utf-8",
    )
    fake = _FakeGitOps(changed={repo_dir})
    monkeypatch.setattr(pc, "git_ops", fake)

    ImplementStage._finalize(
        ctx, t, repo_dir, "mill/x", "summary", ok=True, reference_files=None
    )

    fragment = repo_dir / "changes" / f"{t.id}.misc.md"
    assert fragment.is_file()
    assert fragment.read_text(encoding="utf-8") == "Implement foo bar baz"


def test_finalize_skips_towncrier_when_not_configured(
    ctx_factory, tmp_path, monkeypatch
):
    """When ``pyproject.toml`` exists but has no ``[tool.towncrier]``,
    no fragment file is created."""
    ctx = ctx_factory()
    t = _ticket(ctx, title="Some change")
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "pyproject.toml").write_text(
        '[project]\nname = "example"\n',
        encoding="utf-8",
    )
    fake = _FakeGitOps(changed={repo_dir})
    monkeypatch.setattr(pc, "git_ops", fake)

    ImplementStage._finalize(
        ctx, t, repo_dir, "mill/x", "summary", ok=True, reference_files=None
    )

    # Default directory would be "changes".
    assert not (repo_dir / "changes").exists()
    # Commit still happened — just no fragment.
    assert len(fake.commits) == 1


def test_finalize_skips_towncrier_when_no_pyproject(ctx_factory, tmp_path, monkeypatch):
    """When no ``pyproject.toml`` exists, no fragment file is created
    and no error is raised."""
    ctx = ctx_factory()
    t = _ticket(ctx, title="Some change")
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    # No pyproject.toml at all.
    fake = _FakeGitOps(changed={repo_dir})
    monkeypatch.setattr(pc, "git_ops", fake)

    # Must not raise.
    ImplementStage._finalize(
        ctx, t, repo_dir, "mill/x", "summary", ok=True, reference_files=None
    )

    assert not (repo_dir / "changes").exists()
    assert len(fake.commits) == 1


def test_finalize_towncrier_respects_custom_directory(
    ctx_factory, tmp_path, monkeypatch
):
    """When ``[tool.towncrier]`` sets ``directory = "news"``, the fragment
    is created at ``news/<ticket_id>.misc.md``."""
    ctx = ctx_factory()
    t = _ticket(ctx, title="Custom dir test")
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "pyproject.toml").write_text(
        '[tool.towncrier]\ndirectory = "news"\n',
        encoding="utf-8",
    )
    fake = _FakeGitOps(changed={repo_dir})
    monkeypatch.setattr(pc, "git_ops", fake)

    ImplementStage._finalize(
        ctx, t, repo_dir, "mill/x", "summary", ok=True, reference_files=None
    )

    fragment = repo_dir / "news" / f"{t.id}.misc.md"
    assert fragment.is_file()
    assert fragment.read_text(encoding="utf-8") == "Custom dir test"
    # Default directory must NOT exist.
    assert not (repo_dir / "changes").exists()


def test_finalize_skips_towncrier_when_no_changes(ctx_factory, tmp_path, monkeypatch):
    """When the repo has no changes (``has_changes`` is False),
    ``commit_all`` is NOT called AND no fragment file is created."""
    ctx = ctx_factory()
    t = _ticket(ctx, title="No-op change")
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "pyproject.toml").write_text(
        '[tool.towncrier]\ndirectory = "changes"\n',
        encoding="utf-8",
    )
    fake = _FakeGitOps(changed=set())  # no repo has changes
    monkeypatch.setattr(pc, "git_ops", fake)

    ImplementStage._finalize(
        ctx, t, repo_dir, "mill/x", "summary", ok=False, reference_files=None
    )

    # commit_all must NOT have been called.
    assert fake.commits == []
    # Fragment must NOT exist (gated on has_changes).
    assert not (repo_dir / "changes").exists()


def test_finalize_skips_towncrier_when_fragment_already_exists(
    ctx_factory, tmp_path, monkeypatch
):
    """When a towncrier fragment (e.g. ``<id>.feature.md``) already exists
    before ``_finalize`` (the LLM agent wrote it), the auto-generated
    ``.misc.md`` is silently skipped."""
    ctx = ctx_factory()
    t = _ticket(ctx, title="Add new feature to the system")
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "pyproject.toml").write_text(
        '[tool.towncrier]\ndirectory = "changes"\n',
        encoding="utf-8",
    )
    # Simulate the LLM agent having written a .feature.md fragment.
    changes_dir = repo_dir / "changes"
    changes_dir.mkdir()
    feature_fragment = changes_dir / f"{t.id}.feature.md"
    feature_content = "Add new feature to the system"
    feature_fragment.write_text(feature_content, encoding="utf-8")

    fake = _FakeGitOps(changed={repo_dir})
    monkeypatch.setattr(pc, "git_ops", fake)

    ImplementStage._finalize(
        ctx, t, repo_dir, "mill/x", "summary", ok=True, reference_files=None
    )

    # .misc.md must NOT exist (skipped because .feature.md already exists).
    assert not (changes_dir / f"{t.id}.misc.md").exists()

    # .feature.md must be unchanged.
    assert feature_fragment.read_text(encoding="utf-8") == feature_content

    # Exactly one commit happened (the agent's other changes still get committed).
    assert len(fake.commits) == 1


# --- convergence backstop with cross_repo_target ------------------------


def _write_file_map(ctx, ticket, *files):
    """Write a minimal file_map.json for *ticket* listing *files*."""
    ws = ctx.service.workspace(ticket)
    (ws.artifacts_dir / "file_map.json").write_text(
        json.dumps([{"file": f, "note": "test"} for f in files]),
        encoding="utf-8",
    )


def _make_bare_repo_on_branch(tmp_path: Path, branch: str) -> str:
    """Create a bare repo whose default branch is *branch* (not main)."""
    seed = tmp_path / "seed"
    seed.mkdir()
    subprocess.run(
        ["git", "-C", str(seed), "init", "-q", f"--initial-branch={branch}"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(seed), "config", "user.email", "t@t"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(seed), "config", "user.name", "t"],
        check=True,
        capture_output=True,
    )
    (seed / "README.md").write_text("seed\n")
    subprocess.run(
        ["git", "-C", str(seed), "add", "-A"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(seed), "commit", "-q", "-m", "init"],
        check=True,
        capture_output=True,
    )
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", "-q", str(seed), str(bare)],
        check=True,
        capture_output=True,
    )
    return f"file://{bare}"


def test_convergence_backstop_uses_cross_repo_base_branch(
    ctx_factory, tmp_path, monkeypatch
):
    """When cross_repo_target.base_branch is "develop", the convergence
    backstop compares against origin/develop instead of origin/main."""
    from robotsix_mill.config import CrossRepoTarget

    remote = _make_bare_repo_on_branch(tmp_path, "develop")

    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="true",
        max_implement_review_cycles="10",
    )
    ctx.repo_config.cross_repo_target = CrossRepoTarget(
        upstream_remote_url=remote,
        fork_remote_url=remote,
        base_branch="develop",
    )

    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    # Bypass gates that require a real sandbox / API key.
    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    # Run implement once so the branch exists.
    def _run_once(*, repo_dir, **_kwargs):
        (Path(repo_dir) / "feature.txt").write_text("implemented")
        return ("done", ["feature.txt"], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run_once)
    out1 = ImplementStage().run(t, ctx)
    assert out1.next_state is State.CODE_REVIEW

    # Simulate returning from review: set review_rounds > 0 and RESET
    # the branch so it has no commits beyond origin/develop.
    t = ctx.service.get(t.id)
    ctx.service.set_review_rounds(t.id, 1)
    ws = ctx.service.workspace(t)
    repo_dir = ws.dir / "repo"
    branch = f"{ctx.settings.branch_prefix}{t.id}"
    subprocess.run(
        ["git", "-C", str(repo_dir), "reset", "--hard", "origin/develop"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "checkout", "-B", branch],
        check=True,
        capture_output=True,
    )

    t = ctx.service.get(t.id)
    assert t.review_rounds == 1

    # Second implement run: resuming=True, review_rounds>0, branch has
    # no commits ahead of origin/develop → genuine no-op → terminate DONE
    # (already satisfied) instead of looping in BLOCKED.
    out2 = ImplementStage().run(t, ctx)
    assert out2.next_state is State.DONE
    assert "already satisfied" in out2.note.lower()
    assert "empty diff" in out2.note.lower()
    # The note must reference the correct base branch.
    assert "origin/develop" in out2.note.lower()
