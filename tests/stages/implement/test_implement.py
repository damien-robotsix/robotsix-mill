import json
import subprocess
from pathlib import Path

import pytest

from robotsix_mill.agents import coding
from robotsix_mill.agents.fs_tools import build_fs_tools
from robotsix_mill.core.states import State
from robotsix_mill.scm.vcs import git_ops
from robotsix_mill.stages.implement import ImplementStage
from tests.stages.implement.conftest import (
    _fake_agent,
    _ticket,
    _write_file_map,
    make_bare_repo,
)

# --- fs_tools sandbox ---------------------------------------------------


def test_fs_tools_roundtrip_and_sandbox(tmp_path, fake_sandbox):
    from robotsix_mill.config import Settings

    s = Settings(data_dir=str(tmp_path))
    (
        read_file,
        write_file,
        _edit_file,
        _delete_file,
        list_dir,
        run_command,
        _parallel,
    ) = build_fs_tools(tmp_path, s)
    assert "wrote" in write_file("a/b.txt", "hi")
    assert read_file(path="a/b.txt") == "hi"
    assert "a/" in list_dir(".")
    assert "exit=0" in run_command("echo ok")
    # errors come back as strings (so the model can self-correct), and
    # the path-escape guard still refuses the op
    esc = read_file(path="../escape.txt")
    assert esc.startswith("error:")
    assert "escapes" in esc
    assert read_file(path="nope.txt").startswith("error:")  # missing file


def test_write_file_unchanged(tmp_path, fake_sandbox):
    """Existing write_file roundtrip still works identically."""
    from robotsix_mill.config import Settings

    s = Settings(data_dir=str(tmp_path))
    read_file, write_file, *_ = build_fs_tools(tmp_path, s)
    assert "wrote" in write_file("x.txt", "hello world")
    assert read_file(path="x.txt") == "hello world"


def test_edit_file_replaces_unique_substring_preserves_rest(tmp_path, fake_sandbox):
    from robotsix_mill.config import Settings

    s = Settings(data_dir=str(tmp_path))
    _, _, edit_file, _, _, _, _ = build_fs_tools(tmp_path, s)
    original = "line1\nline2\nline3\nline4\n"
    (tmp_path / "f.txt").write_text(original)
    result = edit_file("f.txt", "line2", "REPLACED")
    assert "replaced 1 occurrence" in result
    new = (tmp_path / "f.txt").read_text()
    assert "REPLACED" in new
    assert "line2" not in new
    # surrounding lines byte-identical
    assert new == "line1\nREPLACED\nline3\nline4\n"


def test_edit_file_old_string_absent_returns_error_file_unchanged(
    tmp_path, fake_sandbox
):
    from robotsix_mill.config import Settings

    s = Settings(data_dir=str(tmp_path))
    _, _, edit_file, _, _, _, _ = build_fs_tools(tmp_path, s)
    original = "line1\nline2\n"
    (tmp_path / "f.txt").write_text(original)
    result = edit_file("f.txt", "nonexistent", "X")
    assert "not found" in result
    assert (tmp_path / "f.txt").read_text() == original


def test_edit_file_old_string_appears_multiple_returns_error_file_unchanged(
    tmp_path,
    fake_sandbox,
):
    from robotsix_mill.config import Settings

    s = Settings(data_dir=str(tmp_path))
    _, _, edit_file, _, _, _, _ = build_fs_tools(tmp_path, s)
    original = "dup\nmiddle\ndup\n"
    (tmp_path / "f.txt").write_text(original)
    result = edit_file("f.txt", "dup", "X")
    assert "appears 2 times" in result
    assert (tmp_path / "f.txt").read_text() == original


def test_edit_file_path_escape_rejected(tmp_path, fake_sandbox):
    from robotsix_mill.config import Settings

    s = Settings(data_dir=str(tmp_path))
    _, _, edit_file, _, _, _, _ = build_fs_tools(tmp_path, s)
    result = edit_file("../outside.txt", "x", "y")
    assert "escapes" in result


def test_delete_file_removes_existing_file(tmp_path, fake_sandbox):
    """delete_file returns success and the file no longer exists."""
    from robotsix_mill.config import Settings

    s = Settings(data_dir=str(tmp_path))
    _, _, _, delete_file, _, _, _ = build_fs_tools(tmp_path, s)
    (tmp_path / "foo.txt").write_text("hello")
    result = delete_file("foo.txt")
    assert "deleted" in result
    assert not (tmp_path / "foo.txt").exists()


def test_delete_file_missing_returns_error(tmp_path, fake_sandbox):
    """delete_file on a missing file returns an error string, not a crash."""
    from robotsix_mill.config import Settings

    s = Settings(data_dir=str(tmp_path))
    _, _, _, delete_file, _, _, _ = build_fs_tools(tmp_path, s)
    result = delete_file("nope.txt")
    assert result.startswith("error:")


def test_delete_file_on_directory_returns_error(tmp_path, fake_sandbox):
    """delete_file on a directory returns an error string, no deletion."""
    from robotsix_mill.config import Settings

    s = Settings(data_dir=str(tmp_path))
    _, _, _, delete_file, _, _, _ = build_fs_tools(tmp_path, s)
    d = tmp_path / "subdir"
    d.mkdir()
    result = delete_file("subdir")
    assert result.startswith("error:")
    assert d.exists()  # directory untouched


def test_delete_file_path_escape_rejected(tmp_path, fake_sandbox):
    """Path traversal is rejected by _safe."""
    from robotsix_mill.config import Settings

    s = Settings(data_dir=str(tmp_path))
    _, _, _, delete_file, _, _, _ = build_fs_tools(tmp_path, s)
    result = delete_file("../outside.txt")
    assert "escapes" in result


def test_fs_tools_non_existent_root_returns_clear_error(tmp_path, fake_sandbox):
    """Every tool returns a stable error string (not a raw exception)
    when the workspace repo directory hasn't been cloned yet."""
    from robotsix_mill.config import Settings

    fake_root = tmp_path / "does-not-exist"
    s = Settings(data_dir=str(tmp_path))
    read_file, write_file, edit_file, delete_file, list_dir, run_command, _parallel = (
        build_fs_tools(fake_root, s)
    )
    msg = "workspace repo directory does not exist"

    assert msg in read_file(path="anything.txt")
    assert msg in write_file("x.txt", "content")
    assert msg in edit_file("x.txt", "a", "b")
    assert msg in delete_file("x.txt")
    assert msg in list_dir(".")
    # run_command does NOT go through _safe — it calls sandbox.run()
    # directly. When the repo_dir doesn't exist, _repo_mount rejects it.
    assert "repo" in run_command("true").lower()


# --- implement stage ----------------------------------------------------


def test_blocked_without_remote(ctx_factory):
    ctx = ctx_factory(test_command="true")
    out = ImplementStage().run(_ticket(ctx), ctx)
    assert out.next_state is State.BLOCKED
    assert "forge_remote_url" in out.note


def test_meta_ticket_builds_multi_repo_workspace(ctx_factory, tmp_path, monkeypatch):
    """A meta-board ticket runs the repo-triage + multi-repo workspace
    build, threads ``extra_roots`` to ``run_implement_agent``, and keys
    its memory ledger on the meta board (not crash on an empty board_id).
    """
    import robotsix_mill.meta.triage as mt
    import robotsix_mill.meta.workspace as mw

    ctx = ctx_factory(test_command="true", review_enabled="false")
    ctx.repo_config = None  # meta board is not a registered repo
    t = _ticket(ctx)
    t.board_id = "meta"
    _write_file_map(ctx, t, "feature.txt")

    # Build a real, on-disk clone so the implement loop's git_ops calls
    # (branch_exists, create_branch, checkout, …) work end-to-end.
    remote = make_bare_repo(tmp_path)
    primary = tmp_path / "meta-clones" / "robotsix-mill"
    primary.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "-q", remote, str(primary)],
        check=True,
        capture_output=True,
    )
    # Match git_ops.clone's identity setup so commits work.
    subprocess.run(
        ["git", "-C", str(primary), "config", "user.email", "mill@robotsix.local"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(primary), "config", "user.name", "robotsix-mill"],
        check=True,
    )
    extra = [primary]

    monkeypatch.setattr(
        mt,
        "required_repos_for",
        lambda *, settings, spec: ["robotsix-mill"],
    )
    monkeypatch.setattr(
        mw,
        "build_meta_workspace",
        lambda settings, ws, repo_ids: (primary, extra),
    )

    captured: dict = {}

    def _capture(*, settings, repo_dir, spec, **kw):
        captured["extra_roots"] = kw.get("extra_roots")
        captured["board_id"] = kw.get("board_id")
        (Path(repo_dir) / "feature.txt").write_text("x")
        return ("done", ["feature.txt"], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _capture)
    # Bypass the baseline check — fakes don't need to pass a real suite.
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.DOCUMENTING
    assert captured["extra_roots"] == extra
    # Memory ledger keyed on the ticket's own board ("meta"), not "".
    assert captured["board_id"] == "meta"
    # memory_file_for("implement", "meta") must resolve without raising
    # — call it directly to prove the fallback works.
    assert ctx.settings.memory_file_for("implement", "meta")


def test_meta_ticket_blocks_when_no_repos_clonable(ctx_factory, monkeypatch):
    """If the triaged workspace yields no clone, implement BLOCKs the
    meta ticket with the same note refine uses."""
    import robotsix_mill.meta.triage as mt
    import robotsix_mill.meta.workspace as mw

    ctx = ctx_factory(test_command="true")
    ctx.repo_config = None
    t = _ticket(ctx)
    t.board_id = "meta"

    monkeypatch.setattr(mt, "required_repos_for", lambda *, settings, spec: [])
    monkeypatch.setattr(
        mw,
        "build_meta_workspace",
        lambda settings, ws, repo_ids: (None, []),
    )

    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "no repos could be cloned" in out.note


def test_meta_ticket_blocks_when_triage_fails(ctx_factory, monkeypatch):
    """If ``required_repos_for`` raises, implement BLOCKs the meta ticket
    with a clear "meta repo-triage failed" note."""
    import robotsix_mill.meta.triage as mt

    ctx = ctx_factory(test_command="true")
    ctx.repo_config = None
    t = _ticket(ctx)
    t.board_id = "meta"

    def _boom(*, settings, spec):
        raise RuntimeError("triage exploded")

    monkeypatch.setattr(mt, "required_repos_for", _boom)

    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "meta repo-triage failed" in out.note


def test_success_to_deliverable(ctx_factory, tmp_path, monkeypatch):
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote, test_command="true", review_enabled="false"
    )
    monkeypatch.setattr(
        coding, "run_implement_agent", _fake_agent({"feature.txt": "x"})
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.DOCUMENTING
    repo = ctx.service.workspace(t).dir / "repo"
    assert (repo / "feature.txt").exists()
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head == f"mill/{t.id}"
    assert ctx.service.get(t.id).branch == f"mill/{t.id}"
    assert (ctx.service.workspace(t).artifacts_dir / "implement.md").exists()


def test_implement_writes_phase_timings_artifact(ctx_factory, tmp_path, monkeypatch):
    """After an implement run, ``artifacts/implement_timings.json`` exists with
    the wrapped per-phase durations, the ``passes`` counter, and a positive
    ``total_s`` (acceptance criterion 1)."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote, test_command="true", review_enabled="false"
    )
    monkeypatch.setattr(
        coding, "run_implement_agent", _fake_agent({"feature.txt": "x"})
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING

    timings_path = ctx.service.workspace(t).artifacts_dir / "implement_timings.json"
    assert timings_path.exists()
    data = json.loads(timings_path.read_text(encoding="utf-8"))
    # Sequential phases that always run on the LLM path.
    for key in ("clone_and_branch", "agent_pass", "test_gate", "finalize"):
        assert key in data, f"missing phase key {key!r} in {data!r}"
    assert data["passes"] >= 1
    assert data["total_s"] > 0


def test_phase_timings_artifact_written_even_when_phase_raises(
    ctx_factory, tmp_path, monkeypatch
):
    """A raising phase must not suppress the timing artifact: emission happens
    in ``ImplementStage.run``'s ``finally`` regardless of outcome (acceptance
    criterion 5 — instrumentation is exception-safe end-to-end)."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote, test_command="true", review_enabled="false"
    )

    def _boom(*_a, **_kw):
        raise RuntimeError("baseline exploded")

    monkeypatch.setattr(ImplementStage, "_run_baseline_check", _boom)
    monkeypatch.setattr(
        coding, "run_implement_agent", _fake_agent({"feature.txt": "x"})
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    with pytest.raises(RuntimeError, match="baseline exploded"):
        ImplementStage().run(t, ctx)

    timings_path = ctx.service.workspace(t).artifacts_dir / "implement_timings.json"
    assert timings_path.exists()
    data = json.loads(timings_path.read_text(encoding="utf-8"))
    # The phases that ran before the raise are captured; total is positive.
    assert "clone_and_branch" in data
    assert "baseline_check" in data
    assert data["total_s"] > 0


def test_no_changes_terminates_done_when_already_satisfied(
    ctx_factory, tmp_path, monkeypatch
):
    """A fresh run whose test gate passes and that produces an empty diff
    with NO edit-tool calls and NO gitignored writes is a genuine no-op:
    the spec is already satisfied. Terminate DONE instead of looping in
    BLOCKED (ticket 0976)."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote, test_command="true", review_enabled="false"
    )
    monkeypatch.setattr(coding, "run_implement_agent", _fake_agent(None))
    t = _ticket(ctx)
    _write_file_map(ctx, t, "dummy.txt")
    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.DONE
    assert "already satisfied" in out.note.lower()


def test_spec_mandate_blocks_empty_diff_done(ctx_factory, tmp_path, monkeypatch):
    """An empty diff on a fresh run must NOT terminate DONE when the spec
    explicitly mandates a non-empty diff — BLOCK for inspection instead
    (regression for the empty-draft fast-path false positive)."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote, test_command="true", review_enabled="false"
    )
    monkeypatch.setattr(coding, "run_implement_agent", _fake_agent(None))
    t = _ticket(
        ctx,
        "Wire real tinyauth mobile token exchange",
        "## Acceptance criteria\n\n"
        "- The implementation must produce a non-empty diff.\n"
        "- The ticket must not be closed without changes.",
    )
    _write_file_map(ctx, t, "dummy.txt")
    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "spec demands code change" in out.note


def test_failing_gate_blocks_resumable(ctx_factory, tmp_path, monkeypatch):
    """The stage owns a bounded fix loop: it re-invokes the coordinator
    on each test-gate failure, feeding the diagnosis back, and escalates
    to BLOCKED-resumable once max_fix_iterations is exhausted — WIP
    committed."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="false",  # gate always fails
        max_fix_iterations="2",  # keep the loop short
    )
    calls = []

    def _run(
        *,
        settings,
        repo_dir,
        spec,
        feedback=None,
        reference_files=None,
        message_history=None,
        memory="",
        epic_workspace_path=None,
        previous_attempt_summary=None,
        **_kwargs,
    ):
        del (
            settings,
            spec,
            reference_files,
            message_history,
            memory,
            epic_workspace_path,
        )  # seam signature
        calls.append(feedback)
        (Path(repo_dir) / "wip.txt").write_text("did work")
        return ("tried", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)
    # Bypass the baseline check — this test exercises the per-iteration
    # test gate, not the pre-flight baseline.
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)
    t = _ticket(ctx)
    _write_file_map(ctx, t, "wip.txt")

    out = ImplementStage().run(t, ctx)  # test_failing_gate_blocks_resumable

    assert out.next_state is State.BLOCKED
    assert "still failing" in out.note
    assert "resumable" in out.note
    # The stage re-invokes the coordinator once per iteration.
    assert len(calls) == 2
    assert calls[0] is None  # first pass: no feedback
    assert calls[1] is not None  # retry: prior diagnosis fed back
    repo = ctx.service.workspace(t).dir / "repo"
    log = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--pretty=%s"],
        capture_output=True,
        text=True,
    ).stdout
    assert "WIP" in log  # WIP committed so a human can pick it up


def test_smoke_gate_runs_after_tests_pass_when_paths_match(
    ctx_factory, tmp_path, monkeypatch
):
    """A board-touching ticket (empty smoke_paths ⇒ unconditional) runs the
    smoke gate after the unit gate passes, and a smoke failure routes
    exactly like a unit-test failure (escalate → BLOCKED-resumable)."""
    from robotsix_mill.stages import implement as impl_mod

    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",  # unit gate passes
        smoke_command="scripts/smoke.sh",  # smoke gate enabled
        review_enabled="false",
        max_fix_iterations="1",  # escalate on the first failure
    )
    monkeypatch.setattr(
        coding, "run_implement_agent", _fake_agent({"feature.txt": "x"})
    )
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    smoke_calls = []

    def _fake_smoke(**kwargs):
        smoke_calls.append(kwargs)
        return (False, "smoke failed: board did not render")

    monkeypatch.setattr(impl_mod, "run_smoke_agent", _fake_smoke)

    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    out = ImplementStage().run(t, ctx)

    assert smoke_calls, "smoke gate must run after the unit gate passes"
    assert out.next_state is State.BLOCKED
    assert "still failing" in out.note
    assert "resumable" in out.note


def test_smoke_gate_skipped_when_paths_do_not_match(ctx_factory, tmp_path, monkeypatch):
    """A pure-backend ticket whose introduced files match no smoke_paths
    glob does NOT invoke the smoke gate — the ticket proceeds normally."""
    from robotsix_mill.stages import implement as impl_mod

    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        smoke_command="scripts/smoke.sh",
        review_enabled="false",
    )
    monkeypatch.setattr(coding, "run_implement_agent", _fake_agent({"backend.py": "x"}))
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)
    # Non-empty, board-scoped globs; the changed file (backend.py) matches none.
    monkeypatch.setattr(
        impl_mod,
        "load_repo_smoke_paths",
        lambda repo_dir: ["src/robotsix_mill/runtime/**"],
    )

    smoke_calls = []
    monkeypatch.setattr(
        impl_mod,
        "run_smoke_agent",
        lambda **kw: smoke_calls.append(kw) or (True, "smoke passed"),
    )

    t = _ticket(ctx)
    _write_file_map(ctx, t, "backend.py")

    out = ImplementStage().run(t, ctx)

    assert not smoke_calls, "smoke gate must NOT run for a non-matching diff"
    assert out.next_state is State.DOCUMENTING


def test_smoke_gate_lifts_board_screenshot_into_artifacts(
    ctx_factory, tmp_path, monkeypatch
):
    """When the board smoke writes its screenshot to <clone>/artifacts/board.png
    (BOARD_SMOKE_SCREENSHOT, cwd = the clone), the implement gate lifts it
    into the workspace artifacts dir where the review stage reads it."""
    from robotsix_mill.stages import implement as impl_mod

    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        smoke_command="scripts/smoke.sh",  # smoke gate enabled (paths empty ⇒ runs)
        review_enabled="false",
    )
    monkeypatch.setattr(
        coding, "run_implement_agent", _fake_agent({"feature.txt": "x"})
    )
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    def _fake_smoke(*, settings, repo_dir, repo_config=None, **_kw):
        del settings, repo_config
        # Mirror board_browser_check.py honoring BOARD_SMOKE_SCREENSHOT.
        png = Path(repo_dir) / "artifacts" / "board.png"
        png.parent.mkdir(parents=True, exist_ok=True)
        png.write_bytes(b"\x89PNG\r\n\x1a\n")
        return (True, "smoke passed")

    monkeypatch.setattr(impl_mod, "run_smoke_agent", _fake_smoke)

    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.DOCUMENTING
    ws = ctx.service.workspace(t)
    lifted = ws.artifacts_dir / "board.png"
    assert lifted.exists(), "gate must lift board.png into the workspace artifacts dir"
    assert lifted.read_bytes().startswith(b"\x89PNG")

    # The screenshot must be MOVED, not copied: the clone working tree is
    # clean afterwards so _finalize's ``git add -A`` cannot stage it, and
    # the resulting commit must not carry the stray binary.
    clone = ws.dir / "repo"
    assert not (clone / "artifacts" / "board.png").exists(), (
        "screenshot must be moved out of the clone, not left for git add -A"
    )
    tracked = subprocess.run(
        ["git", "ls-files", "artifacts/board.png"],
        cwd=clone,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert tracked == "", "board.png must not be committed into the feature branch"


def test_env_error_short_circuits_within_two_cycles(ctx_factory, tmp_path, monkeypatch):
    """An ENV-ERROR diagnosis (missing binary) repeated identically caps
    the fix loop at ≤2 cycles — instead of burning max_fix_iterations —
    and BLOCKS with a note naming the missing binary."""
    from robotsix_mill.agents.testing import ENV_ERROR_PREFIX
    from robotsix_mill.stages import implement as impl_mod

    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="yamllint --strict .",
        max_fix_iterations="8",  # high → prove the breaker fires early
    )
    calls = []

    def _run(*, settings, repo_dir, spec, feedback=None, **_kwargs):
        del settings, spec  # seam signature
        calls.append(feedback)
        (Path(repo_dir) / "feature.txt").write_text("work")
        return ("tried", ["feature.txt"], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)
    env_diag = (
        f"{ENV_ERROR_PREFIX} command not found in sandbox: 'yamllint' (rc=127). "
        "This binary is not installed/on PATH; declare it via "
        "extra_sandbox_packages in .robotsix-mill/config.yaml (pip:<name> or "
        "apt:<name>) — not fixable by editing code."
    )
    monkeypatch.setattr(impl_mod, "run_test_agent", lambda **kw: (False, env_diag))
    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "environment failure" in out.note
    assert "yamllint" in out.note  # missing binary surfaced
    assert len(calls) == 2  # short-circuited at the 2nd identical env-error


def test_identical_diagnosis_three_cycles_short_circuits(
    ctx_factory, tmp_path, monkeypatch
):
    """A NON-env failure yielding the identical distilled diagnosis 3
    consecutive cycles is short-circuited to BLOCKED (the general
    repeated-identical-diagnosis guard), not run to max_fix_iterations."""
    from robotsix_mill.stages import implement as impl_mod

    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="false",
        max_fix_iterations="8",
    )
    calls = []

    def _run(*, settings, repo_dir, spec, feedback=None, **_kwargs):
        del settings, spec
        calls.append(feedback)
        (Path(repo_dir) / "feature.txt").write_text("work")
        return ("tried", ["feature.txt"], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)
    diag = "test_foo assertion failed: expected 1 got 2"
    monkeypatch.setattr(impl_mod, "run_test_agent", lambda **kw: (False, diag))
    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert diag in out.note
    assert len(calls) == 3  # short-circuited after 3 identical diagnoses


def _commits(repo):
    return subprocess.run(
        ["git", "-C", str(repo), "log", "--pretty=%s"],
        capture_output=True,
        text=True,
    ).stdout.splitlines()


def test_budget_error_blocks_resumable_with_wip(ctx_factory, tmp_path, monkeypatch):
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote, test_command="true", review_enabled="false"
    )

    def _run(
        *,
        settings,
        repo_dir,
        spec,
        feedback=None,
        reference_files=None,
        message_history=None,
        memory="",
        epic_workspace_path=None,
        previous_attempt_summary=None,
        **_kwargs,
    ):
        del (
            settings,
            spec,
            feedback,
            reference_files,
            message_history,
            memory,
            epic_workspace_path,
        )
        (Path(repo_dir) / "partial.txt").write_text("half done")
        raise coding.AgentBudgetError("request_limit of 50", [])

    monkeypatch.setattr(coding, "run_implement_agent", _run)
    t = _ticket(ctx)
    _write_file_map(ctx, t, "partial.txt")

    out = ImplementStage().run(t, ctx)  # test_budget_error_blocks_resumable_with_wip

    assert out.next_state is State.BLOCKED
    assert "resumable" in out.note
    assert "budget" in out.note
    # WIP committed so a human can resume (no transcript now — a resume
    # re-runs the coordinator fresh).
    ws = ctx.service.workspace(t)
    assert "WIP" in _commits(ws.dir / "repo")[0]
    # Artifacts written even on BLOCKED-as-resumable path.
    assert (ws.artifacts_dir / "reference_files.json").exists()
    assert (ws.artifacts_dir / "implement_summary.md").exists()


def test_resume_reruns_coordinator_without_reclone(ctx_factory, tmp_path, monkeypatch):
    """Resume = run the coordinator FRESH (no transcript replay), and
    crucially do NOT re-clone — the prior WIP branch is reused."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote, test_command="true", review_enabled="false"
    )
    n = {"i": 0}

    def _run(
        *,
        settings,
        repo_dir,
        spec,
        feedback=None,
        reference_files=None,
        message_history=None,
        memory="",
        epic_workspace_path=None,
        previous_attempt_summary=None,
        **_kwargs,
    ):
        del (
            settings,
            spec,
            feedback,
            reference_files,
            message_history,
            memory,
            epic_workspace_path,
        )
        n["i"] += 1
        if n["i"] == 1:  # first pass: partial work, hit the cap
            (Path(repo_dir) / "first.txt").write_text("1")
            raise coding.AgentBudgetError("cap", [])
        (Path(repo_dir) / "second.txt").write_text("2")
        return ("finished on resume", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)
    t = _ticket(ctx)
    _write_file_map(ctx, t, "first.txt", "second.txt")

    first = ImplementStage().run(t, ctx)
    assert first.next_state is State.BLOCKED
    repo = ctx.service.workspace(t).dir / "repo"
    git_inode = (repo / ".git").stat().st_ino  # detect a re-clone

    # worker applies the Outcome; operator moves it back to READY
    ctx.service.transition(t.id, first.next_state, first.note)
    ctx.service.transition(t.id, State.READY, "retry")
    second = ImplementStage().run(ctx.service.get(t.id), ctx)

    assert second.next_state is State.DOCUMENTING
    assert n["i"] == 2  # coordinator re-run
    assert (repo / ".git").stat().st_ino == git_inode  # NOT re-cloned
    assert (repo / "first.txt").exists()  # prior WIP kept
    assert (repo / "second.txt").exists()
    msgs = _commits(repo)
    assert any("WIP" in m for m in msgs)
    assert len(msgs) >= 2


# --- no-change-needed → DONE bypass -------------------------------------


def test_no_change_needed_with_rationale_transitions_to_done(
    ctx_factory, tmp_path, monkeypatch
):
    """When the implement agent signals ``no_change_needed=True`` with
    a non-empty rationale AND produces no git diff, the stage routes
    the ticket DRAFT→DONE with the rationale as the note — instead of
    BLOCKING with the generic "no changes produced" error. This is the
    bypass for tickets where the work was already landed by a sibling
    (e.g. bc-check dead-code cleanups)."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(forge_remote_url=remote, test_command="true")

    def _run(*, repo_dir, **_kwargs):
        # Touch nothing; the codebase is already correct.
        del repo_dir
        return (
            (
                "Inspected — the `hasattr` guard was already removed by "
                "20260528T070000Z-cleanup-hasattr-guards-1234."
            ),
            [],
            "",
            None,
            None,
            True,
            (
                "The `hasattr` guard at routes.py:127 referenced in the "
                "spec was already removed by ticket 1234 on 2026-05-28. "
                "Current repo state matches the spec's desired end state."
            ),
        )

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    t = _ticket(ctx)
    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.DONE
    assert "no change needed" in out.note.lower()
    assert "1234" in out.note  # rationale carried into the note


def test_no_change_needed_empty_rationale_terminates_done(
    ctx_factory, tmp_path, monkeypatch
):
    """``no_change_needed=True`` with an empty rationale falls through
    the rationale-gated bypass to the general empty-diff handler. With
    no edit-tool calls and no gitignored writes it is a genuine no-op
    (empty diff vs base), so it now terminates DONE (already satisfied)
    rather than looping in BLOCKED (ticket 0976). Nothing was produced,
    so no real work can be lost."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(forge_remote_url=remote, test_command="true")

    def _run(*, repo_dir, **_kwargs):
        del repo_dir
        return (
            "nothing to do",
            [],
            "",
            None,
            None,
            True,
            "   ",
        )  # whitespace rationale

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    t = _ticket(ctx)
    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.DONE
    assert "already satisfied" in out.note.lower()


def test_no_change_needed_ignored_when_branch_ahead_of_main(
    ctx_factory, tmp_path, monkeypatch
):
    """Regression: if the workspace branch already carries commits
    ahead of ``origin/main`` (the agent's previous iterations produced
    the diff), the ``no_change_needed`` bypass must NOT fire — routing
    to DONE here strands the work in the workspace forever. Proceed
    normally so deliver picks up the existing commits."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(forge_remote_url=remote, test_command="true")

    def _run(*, repo_dir, **_kwargs):
        # Pre-commit a "previous-iteration" change on the workspace
        # branch so it is ahead of origin/main, but no further changes
        # in this iteration. The agent (wrongly) reports
        # no_change_needed.
        from robotsix_mill.scm.vcs import git_ops

        (repo_dir / "prior_iteration.txt").write_text("from a prior pass")
        git_ops.commit_all(repo_dir, "prior pass content")
        return (
            "Looked around; spec already satisfied by prior commits.",
            [],
            "",
            None,
            None,
            True,
            (
                "(False positive: ignoring this rationale because the "
                "branch has commits ahead of origin/main that haven't "
                "been delivered yet.)"
            ),
        )

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    t = _ticket(ctx)
    out = ImplementStage().run(t, ctx)

    # MUST NOT be DONE — the prior commits still need to be delivered.
    assert out.next_state is not State.DONE


def test_no_change_needed_on_resume_still_routes_to_done(
    ctx_factory, tmp_path, monkeypatch
):
    """Regression: the ``no_change_needed`` → DONE bypass must fire
    on a resume too (the bc-check "remove dead X" case where the
    operator unblocks expecting the agent to confirm a sibling
    ticket already did the work). The original check was gated on
    ``not resuming`` and silently skipped that path, so the empty
    branch leaked downstream to deliver and got re-BLOCKED there."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(forge_remote_url=remote, test_command="true")

    def _run(*, repo_dir, **_kwargs):
        del repo_dir
        return (
            "Confirmed the dead guard was already removed by ticket 5678.",
            [],
            "",
            None,
            None,
            True,
            (
                "The hasattr guard the spec asks us to remove was deleted "
                "by ticket 5678. Verified by reading pass_runner.py — the "
                "symbol is no longer present."
            ),
        )

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    t = _ticket(ctx)
    # Simulate a resume: pre-create the per-ticket clone so the
    # implement stage takes the resume path (skipping re-clone) and
    # sets ``resuming=True`` inside ``_run_single_implement_pass``.
    ws = ctx.service.workspace(t)
    repo_dir = ws.dir / "repo"
    git_ops.clone(remote, repo_dir, "main", None)
    branch = f"mill/{t.id}"
    git_ops.create_branch(repo_dir, branch)
    ctx.service.set_branch(t.id, branch)
    t = ctx.service.get(t.id)

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.DONE
    assert "no change needed" in out.note.lower()
    assert "5678" in out.note


def test_resume_with_ahead_branch_and_clean_tree_proceeds_to_review(
    ctx_factory, tmp_path, monkeypatch
):
    """When the workspace branch already carries prior commits (from
    earlier implement passes) and the current resume pass produces zero
    new working-tree changes, the ticket must proceed to CODE_REVIEW →
    deliver instead of short-circuiting to DONE.  Routing to DONE strands
    the WIP commits — they never reach deliver, no PR is opened, and the
    gap regrows.  Proceeding to CODE_REVIEW lets the deliver stage detect
    the ahead-of-target commits, push, and create the PR.

    A convergence backstop (``implement_cycles``) guards against a true
    review→implement loop.
    """
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="true",
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    # Bypass preflight gates.
    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    # First pass: agent creates a commit so the branch is ahead.
    def _run_first(*, repo_dir, **_kwargs):
        (Path(repo_dir) / "feature.txt").write_text("implemented")
        return ("done", ["feature.txt"], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run_first)
    out1 = ImplementStage().run(t, ctx)
    assert out1.next_state is State.CODE_REVIEW

    # Second pass (resume): branch already has the implementation.
    # Agent produces zero new changes — no edit tools called, clean
    # working tree.  Should route to DONE, not CODE_REVIEW.
    t = ctx.service.get(t.id)
    ctx.service.set_review_rounds(t.id, 1)

    def _run_resume(*, repo_dir, **_kwargs):
        del repo_dir
        return (
            (
                "The spec is already implemented — feature.txt was modified "
                "in a prior pass and no further changes are needed."
            ),
            [],
            "",
            None,
            None,
            False,
            "",
        )

    monkeypatch.setattr(coding, "run_implement_agent", _run_resume)
    out2 = ImplementStage().run(t, ctx)
    assert out2.next_state is State.CODE_REVIEW
