import contextlib
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from robotsix_mill.agents import coding
from robotsix_mill.agents.fs_tools import build_fs_tools
from robotsix_mill.core.models import TicketKind
from robotsix_mill.core.states import State
from robotsix_mill.stages.implement import ImplementStage
from robotsix_mill.vcs import git_ops
from tests.stages.implement.conftest import _ticket, _write_file_map


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def make_bare_repo(tmp_path: Path) -> str:
    """A throwaway local remote (file://) with a `main` branch — lets us
    exercise clone/branch/commit fully offline, no forge."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q")
    _git(seed, "config", "user.email", "t@t")
    _git(seed, "config", "user.name", "t")
    (seed / "README.md").write_text("seed\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "init")
    _git(seed, "branch", "-M", "main")
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", "-q", str(seed), str(bare)],
        check=True,
        capture_output=True,
    )
    return f"file://{bare}"


def _fake_agent(write: dict | None):
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
            previous_attempt_summary,
        )  # signature must match the seam
        if write:
            for name, content in write.items():
                (Path(repo_dir) / name).write_text(content)
        return (
            "did the thing",
            list(write.keys()) if write else [],
            "",
            None,
            None,
            False,
            "",
        )

    return _run


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


# --- unconditional rebase (fresh clone + resume) -----------------------


def _add_commit_to_bare_remote(bare_url: str, tmp_path: Path) -> str:
    """Add a commit to a bare remote (file:// URL) and return the file name.

    Clones the bare repo into a temp working dir, adds a file, commits,
    and pushes back to the bare remote. Returns the filename created.
    """
    import uuid

    wd = tmp_path / f"push-tmp-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["git", "clone", "-q", bare_url, str(wd)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(wd, "config", "user.email", "op@t")
    _git(wd, "config", "user.name", "operator")
    fname = "operator_edit.txt"
    (wd / fname).write_text("operator change on main\n")
    _git(wd, "add", "-A")
    _git(wd, "commit", "-q", "-m", "operator edit")
    _git(wd, "push", "origin", "main")
    return fname


def _conflicting_edit_on_remote(bare_url: str, tmp_path: Path) -> None:
    """Push a conflicting edit to README.md on the bare remote."""
    import uuid

    wd = tmp_path / f"conflict-tmp-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["git", "clone", "-q", bare_url, str(wd)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(wd, "config", "user.email", "op@t")
    _git(wd, "config", "user.name", "operator")
    (wd / "README.md").write_text("conflicting edit from remote\n")
    _git(wd, "add", "-A")
    _git(wd, "commit", "-q", "-m", "conflicting remote edit")
    _git(wd, "push", "origin", "main")


def test_fresh_clone_rebases_onto_new_remote_commit(ctx_factory, tmp_path, monkeypatch):
    """When a fresh clone materialises and origin/<target> has advanced
    since the clone (simulated by pushing *after* an initial clone that
    we discard), the rebase step picks up the new commit before the
    agent runs."""
    remote = make_bare_repo(tmp_path)

    # Push a second commit to the remote so it has README.md + operator_edit.txt.
    fname = _add_commit_to_bare_remote(remote, tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )

    seen_files: list[str] = []

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
        # Record what the agent can see in the working tree.
        for p in sorted(Path(repo_dir).iterdir()):
            if p.name != ".git":
                seen_files.append(p.name)
        (Path(repo_dir) / "agent_out.txt").write_text("done")
        return ("done", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)
    t = _ticket(ctx)
    _write_file_map(ctx, t, "agent_out.txt")

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.DOCUMENTING
    # The agent must see the operator's edit that landed on the remote
    # before the clone — proving the rebase brought it in (even though
    # in this case the clone also got it; the rebase is a no-op when the
    # clone already has the latest).
    assert fname in seen_files


def test_resume_rebases_onto_new_remote_commit(ctx_factory, tmp_path, monkeypatch):
    """Resume path: after a budget-cap BLOCKED run, a new commit lands
    on origin/main.  On resume the rebase picks it up and the agent
    sees the new file."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )
    n = {"i": 0}

    seen_files: list[list[str]] = [[], []]

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
        idx = n["i"]
        n["i"] += 1
        if idx == 0:
            (Path(repo_dir) / "first.txt").write_text("1")
            raise coding.AgentBudgetError("cap", [])
        # idx == 1: resume
        for p in sorted(Path(repo_dir).iterdir()):
            if p.name != ".git":
                seen_files[1].append(p.name)
        (Path(repo_dir) / "second.txt").write_text("2")
        return ("finished on resume", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)
    t = _ticket(ctx)
    _write_file_map(ctx, t, "first.txt", "second.txt")

    first = ImplementStage().run(t, ctx)
    assert first.next_state is State.BLOCKED

    # Simulate an operator edit landing on the remote while the ticket
    # is BLOCKED.
    fname = _add_commit_to_bare_remote(remote, tmp_path)

    ctx.service.transition(t.id, first.next_state, first.note)
    ctx.service.transition(t.id, State.READY, "retry")
    second = ImplementStage().run(ctx.service.get(t.id), ctx)

    assert second.next_state is State.DOCUMENTING
    assert n["i"] == 2
    # The agent must see the operator's edit in its working tree on resume.
    assert fname in seen_files[1]


def test_rebase_conflict_blocks_on_resume(ctx_factory, tmp_path, monkeypatch):
    """When a WIP commit on the ticket branch conflicts with a newer
    remote commit, the resume rebase fails → REBASING with a note about
    rebase failure. The workspace is left intact for operator inspection."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
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
        if n["i"] == 1:
            # Edit README.md to create a conflicting WIP commit.
            (Path(repo_dir) / "README.md").write_text("WIP edit to README\n")
            (Path(repo_dir) / "wip.txt").write_text("partial work")
            raise coding.AgentBudgetError("cap", [])
        # Should never reach here — the rebase should fail before the agent runs.
        raise AssertionError("agent should not run on resume when rebase fails")

    monkeypatch.setattr(coding, "run_implement_agent", _run)
    t = _ticket(ctx)
    _write_file_map(ctx, t, "README.md", "wip.txt")

    first = ImplementStage().run(t, ctx)
    assert first.next_state is State.BLOCKED

    # Push a conflicting edit to README.md on the remote.
    _conflicting_edit_on_remote(remote, tmp_path)

    ctx.service.transition(t.id, first.next_state, first.note)
    ctx.service.transition(t.id, State.READY, "retry")

    second = ImplementStage().run(ctx.service.get(t.id), ctx)

    assert second.next_state is State.REBASING
    assert "rebase" in second.note.lower()
    assert n["i"] == 1  # agent only ran once (first pass); resume blocked before agent

    # Workspace left intact.
    ws = ctx.service.workspace(t)
    repo = ws.dir / "repo"
    assert (repo / ".git").exists()
    assert (repo / "wip.txt").exists()


def test_rebase_failure_on_fresh_clone_blocks(ctx_factory, tmp_path, monkeypatch):
    """When try_rebase_onto fails on a fresh clone (e.g. fetch error),
    the stage returns REBASING with a note about rebase failure."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )

    # Force try_rebase_onto to fail on the very first call (fresh clone path).
    orig_rebase = git_ops.try_rebase_onto
    call_count = [0]

    def _failing_rebase(repo, target, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return False
        return orig_rebase(repo, target, **kwargs)

    monkeypatch.setattr(git_ops, "try_rebase_onto", _failing_rebase)

    agent_called = []

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
        agent_called.append(1)
        return ("done", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)
    t = _ticket(ctx)

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.REBASING
    assert "rebase" in out.note.lower()
    assert len(agent_called) == 0  # agent never invoked


# --- dependency gating -------------------------------------------------


def test_unmet_dep_noops_at_ready(ctx_factory, tmp_path, monkeypatch):
    """Implement stage returns READY (no-op) when deps are unmet."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote, test_command="true", review_enabled="false"
    )

    # Create the dependency ticket (in DRAFT — not terminal)
    dep = ctx.service.create("Dep ticket")
    assert dep.state is State.DRAFT

    # Create the depender ticket
    t = ctx.service.create("Depender", depends_on=f'["{dep.id}"]')
    ctx.service.transition(t.id, State.READY)
    t = ctx.service.get(t.id)

    agent_called = []

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
        agent_called.append(1)
        (Path(repo_dir) / "out.txt").write_text("done")
        return ("done", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.READY  # same-state no-op
    assert len(agent_called) == 0  # agent NOT called
    assert out.note is None  # no note for no-op


def test_dep_satisfied_implement_proceeds(ctx_factory, tmp_path, monkeypatch):
    """Implement stage proceeds to DELIVERABLE when dep is CLOSED."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote, test_command="true", review_enabled="false"
    )

    # Create and close the dependency
    dep = ctx.service.create("Dep ticket")
    ctx.service.transition(dep.id, State.READY)
    ctx.service.transition(dep.id, State.DELIVERABLE)
    ctx.service.transition(dep.id, State.IMPLEMENT_COMPLETE)
    ctx.service.transition(dep.id, State.HUMAN_MR_APPROVAL)
    ctx.service.transition(dep.id, State.DONE)
    ctx.service.transition(dep.id, State.CLOSED)

    t = ctx.service.create("Depender", "Add feature.txt", depends_on=f'["{dep.id}"]')
    ctx.service.transition(t.id, State.READY)
    t = ctx.service.get(t.id)
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(
        coding,
        "run_implement_agent",
        _fake_agent({"feature.txt": "done"}),
    )

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.DOCUMENTING


def test_missing_dep_id_implement_proceeds(ctx_factory, tmp_path, monkeypatch):
    """Implement stage proceeds when a dep ID doesn't exist (treated satisfied)."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote, test_command="true", review_enabled="false"
    )

    t = ctx.service.create(
        "Depender", "Add feature.txt", depends_on='["nonexistent-12345"]'
    )
    ctx.service.transition(t.id, State.READY)
    t = ctx.service.get(t.id)
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(
        coding,
        "run_implement_agent",
        _fake_agent({"feature.txt": "done"}),
    )

    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING


def test_no_deps_implement_proceeds_normally(ctx_factory, tmp_path, monkeypatch):
    """Tickets without depends_on have zero behavioral change."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote, test_command="true", review_enabled="false"
    )

    t = _ticket(ctx)  # creates ticket without depends_on
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(
        coding,
        "run_implement_agent",
        _fake_agent({"feature.txt": "done"}),
    )

    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING


def test_success_to_code_review_when_review_enabled(ctx_factory, tmp_path, monkeypatch):
    """Pipeline flip: implement routes to CODE_REVIEW when review_enabled is true."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="true",
    )
    monkeypatch.setattr(
        coding, "run_implement_agent", _fake_agent({"feature.txt": "x"})
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.CODE_REVIEW


# --- epic context -------------------------------------------------------


def test_epic_context_prepended_to_spec(ctx_factory, tmp_path, monkeypatch):
    """When a ticket has an epic parent, the spec passed to
    run_implement_agent starts with the epic context wrapper."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",  # this test asserts the no-review path
    )

    # Create an epic with rich global context
    epic = ctx.service.create(
        "Global Epic", "High-level goal: unify UX", kind=TicketKind.EPIC
    )
    # Create a child ticket under this epic
    child = ctx.service.create(
        "Add dark mode",
        "Please add dark mode toggle",
        parent_id=epic.id,
    )
    ctx.service.transition(child.id, State.READY)
    child = ctx.service.get(child.id)
    _write_file_map(ctx, child, "feature.txt")

    seen_spec: list[str] = []

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
            feedback,
            reference_files,
            message_history,
            memory,
            epic_workspace_path,
        )
        seen_spec.append(spec)
        (Path(repo_dir) / "feature.txt").write_text("done")
        return ("done", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    out = ImplementStage().run(child, ctx)
    assert out.next_state is State.DOCUMENTING
    assert len(seen_spec) == 1
    expected = (
        "````epic-context\nHigh-level goal: unify UX\n````\n<!-- /epic-context -->"
    )
    assert seen_spec[0].startswith(expected)


def test_epic_context_not_injected_without_epic_parent(
    ctx_factory, tmp_path, monkeypatch
):
    """Ticket without a parent: no epic context in spec."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote, test_command="true", review_enabled="false"
    )

    t = _ticket(ctx, title="Standalone", body="Just a task")
    _write_file_map(ctx, t, "feature.txt")
    seen_spec: list[str] = []

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
            feedback,
            reference_files,
            message_history,
            memory,
            epic_workspace_path,
        )
        seen_spec.append(spec)
        (Path(repo_dir) / "feature.txt").write_text("done")
        return ("done", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    ImplementStage().run(t, ctx)
    assert len(seen_spec) == 1
    assert "````epic-context" not in seen_spec[0]


def test_epic_context_not_injected_for_non_epic_parent(
    ctx_factory, tmp_path, monkeypatch
):
    """Ticket with a parent that is NOT an epic: no epic context."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote, test_command="true", review_enabled="false"
    )

    # Create a regular task parent (kind=TicketKind.TASK)
    parent = ctx.service.create("Parent task", "Ordinary task", kind=TicketKind.TASK)
    child = ctx.service.create(
        "Child of task",
        "Do a sub-thing",
        parent_id=parent.id,
    )
    ctx.service.transition(child.id, State.READY)
    child = ctx.service.get(child.id)
    _write_file_map(ctx, child, "feature.txt")

    seen_spec: list[str] = []

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
            feedback,
            reference_files,
            message_history,
            memory,
            epic_workspace_path,
        )
        seen_spec.append(spec)
        (Path(repo_dir) / "feature.txt").write_text("done")
        return ("done", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    ImplementStage().run(child, ctx)
    assert len(seen_spec) == 1
    assert "````epic-context" not in seen_spec[0]


def test_epic_context_not_injected_for_empty_epic_description(
    ctx_factory, tmp_path, monkeypatch
):
    """Epic with empty description: no injection."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote, test_command="true", review_enabled="false"
    )

    epic = ctx.service.create("Empty Epic", "", kind=TicketKind.EPIC)
    child = ctx.service.create(
        "Child of empty epic",
        "Do a thing",
        parent_id=epic.id,
    )
    ctx.service.transition(child.id, State.READY)
    child = ctx.service.get(child.id)
    _write_file_map(ctx, child, "feature.txt")

    seen_spec: list[str] = []

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
            feedback,
            reference_files,
            message_history,
            memory,
            epic_workspace_path,
        )
        seen_spec.append(spec)
        (Path(repo_dir) / "feature.txt").write_text("done")
        return ("done", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    ImplementStage().run(child, ctx)
    assert len(seen_spec) == 1
    assert "````epic-context" not in seen_spec[0]


# --- post-edit reference_files persistence ------------------------------


def test_post_edit_reference_files_persisted(ctx_factory, tmp_path, monkeypatch):
    """After a successful agent pass, reference_files.json (paths-only,
    sourced from agent-curated list) and implement_summary.md are written
    to artifacts_dir."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )

    agent_called = []

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
            previous_attempt_summary,
        )
        agent_called.append(1)
        # Agent edits a file AND curates a list that includes an
        # additional file it didn't touch on disk — curated, not
        # git-derived.
        (Path(repo_dir) / "wip.txt").write_text("post-edit content here")
        return (
            "agent summary text",
            ["wip.txt", "base_class.py"],
            "",
            None,
            None,
            False,
            "",
        )

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    t = _ticket(ctx)
    _write_file_map(ctx, t, "wip.txt", "base_class.py")

    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING
    assert len(agent_called) == 1

    artifacts = ctx.service.workspace(t).artifacts_dir

    # reference_files.json exists, paths-only, with agent-curated list.
    ref_path = artifacts / "reference_files.json"
    assert ref_path.exists(), "reference_files.json should exist"
    ref_data = json.loads(ref_path.read_text(encoding="utf-8"))
    assert len(ref_data) == 2
    assert ref_data[0] == {"path": "wip.txt"}
    assert ref_data[1] == {"path": "base_class.py"}

    # implement_summary.md exists with the agent's summary.
    summary_path = artifacts / "implement_summary.md"
    assert summary_path.exists(), "implement_summary.md should exist"
    assert summary_path.read_text(encoding="utf-8") == "agent summary text"


def test_reference_files_reloaded_on_retry(ctx_factory, tmp_path, monkeypatch):
    """On a retry iteration, the reference_files passed to
    run_implement_agent contain the prior pass's agent-curated paths
    (paths-only, reloaded from disk)."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="false",
        review_enabled="false",
        max_fix_iterations="2",
    )

    captured_refs: list[list[dict] | None] = []

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
            message_history,
            memory,
            epic_workspace_path,
            previous_attempt_summary,
        )
        captured_refs.append(reference_files)
        (Path(repo_dir) / "wip.txt").write_text("post-edit pass content")
        return ("agent summary", ["wip.txt"], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    t = _ticket(ctx)
    _write_file_map(ctx, t, "wip.txt")

    out = ImplementStage().run(t, ctx)
    # Test gate always fails → should escalate after 2 iterations.
    assert out.next_state is State.BLOCKED
    assert len(captured_refs) == 2, "agent should be called twice"

    # Second call's reference_files should contain paths-only from
    # the prior pass's agent-curated list.
    refs2 = captured_refs[1]
    assert refs2 is not None, "second call should receive reference_files"
    assert len(refs2) == 1
    assert refs2[0] == {"path": "wip.txt"}


def test_summary_included_in_retry_feedback(ctx_factory, tmp_path, monkeypatch):
    """On a retry iteration, the previous_attempt_summary is threaded to
    the agent alongside the test failure diagnosis as feedback."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="false",
        review_enabled="false",
        max_fix_iterations="2",
    )

    captured_feedback: list[str | None] = []
    captured_prev_summaries: list[str | None] = []

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
        )
        captured_feedback.append(feedback)
        captured_prev_summaries.append(previous_attempt_summary)
        (Path(repo_dir) / "wip.txt").write_text("edited")
        return ("pass-1-summary-abc", ["wip.txt"], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    t = _ticket(ctx)
    _write_file_map(ctx, t, "wip.txt")

    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert len(captured_feedback) == 2

    # First call: feedback should be None, no previous_attempt_summary.
    assert captured_feedback[0] is None
    assert captured_prev_summaries[0] is None

    # Second call: feedback should be the test-failure diagnosis.
    fb = captured_feedback[1]
    assert fb is not None
    # The diag is from the test agent (sandbox unavailable or test failure)
    assert "sandbox unavailable" in fb.lower() or "fail" in fb.lower()

    # previous_attempt_summary is threaded from implement_summary.md
    assert captured_prev_summaries[1] is not None
    assert "pass-1-summary-abc" in captured_prev_summaries[1]


def test_persistence_without_file_map_still_writes(ctx_factory, tmp_path, monkeypatch):
    """When file_map.json is absent, agent-curated reference_files and
    summary are still persisted — no crash."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )

    agent_called = []

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
        agent_called.append(1)
        (Path(repo_dir) / "out.txt").write_text("done")
        return ("summary", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    t = _ticket(ctx)
    # Deliberately do NOT write file_map.json.

    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING
    assert len(agent_called) == 1

    artifacts = ctx.service.workspace(t).artifacts_dir
    # Agent-curated artifacts are still written even without file_map.
    ref_path = artifacts / "reference_files.json"
    assert ref_path.exists(), (
        "reference_files.json should exist from agent-curated list"
    )

    summary_path = artifacts / "implement_summary.md"
    assert summary_path.exists(), "implement_summary.md should exist from agent summary"


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
        from robotsix_mill.vcs import git_ops

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


# --- test-baseline check -------------------------------------------------


def test_baseline_check_blocks_on_failure(ctx_factory, tmp_path, monkeypatch):
    """AC1: pre-existing base-branch test failures block before the loop."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )

    agent_called = []

    def _fake_agent_run(*a, **kw):
        agent_called.append(1)
        return ("done", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _fake_agent_run)

    # Force the baseline check to fail.
    def _failing_test_agent(
        *, settings, repo_dir, repo_config=None, retry_on_failure=False
    ):
        return False, "tests failed (rc=1); pre-existing failure"

    monkeypatch.setattr(
        "robotsix_mill.stages.implement.run_test_agent", _failing_test_agent
    )

    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "pre-existing test failures" in out.note
    assert "main" in out.note  # forge_target_branch
    # The agent loop must never be entered.
    assert len(agent_called) == 0

    # implement.md artifact must exist.
    artifacts = ctx.service.workspace(t).artifacts_dir
    assert (artifacts / "implement.md").exists()
    content = (artifacts / "implement.md").read_text(encoding="utf-8")
    assert "BLOCKED" in content


def test_baseline_check_skipped_for_baseline_fix_ticket(
    ctx_factory, tmp_path, monkeypatch
):
    """Regression: a baseline-fix ticket (source=IMPLEMENT_BASELINE_DEPENDENCY)
    must NOT re-run the baseline gate.

    Such a ticket exists to repair the red base, so it has to implement
    AGAINST that still-red base. Re-running the gate on it would spawn yet
    another baseline fix, which dedups to the ticket itself
    ("Ticket cannot depend on itself" -> Fatal), deadlocking the ticket and
    every ticket parked behind it (board-wide deadlock).
    """
    from robotsix_mill.core.models import SourceKind

    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )

    # Spy: the baseline gate must never be entered for this source. If the
    # guard regresses, this records a call and the assertion below fails.
    baseline_calls: list[int] = []
    monkeypatch.setattr(
        ImplementStage,
        "_run_baseline_check",
        staticmethod(lambda *a, **kw: baseline_calls.append(1)),
    )

    agent_called: list[int] = []

    def _fake_agent_run(*, settings, repo_dir, **_kwargs):
        del settings
        agent_called.append(1)
        (Path(repo_dir) / "feature.txt").write_text("done")
        return ("done", ["feature.txt"], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _fake_agent_run)

    t = ctx.service.create(
        "baseline: pre-existing test failures — main abc1234",
        "Repair the red base.",
        source=SourceKind.IMPLEMENT_BASELINE_DEPENDENCY,
    )
    ctx.service.transition(t.id, State.READY)
    t = ctx.service.get(t.id)
    _write_file_map(ctx, t, "feature.txt")

    out = ImplementStage().run(t, ctx)

    # The baseline gate was skipped for the baseline-fix ticket ...
    assert baseline_calls == []
    # ... and the implement loop ran normally against the (red) base.
    assert len(agent_called) == 1
    assert out.next_state is State.DOCUMENTING


def test_baseline_checks_out_remote_base_sha_not_local_branch(
    ctx_factory, tmp_path, monkeypatch
):
    """Regression: the baseline must check out the EXACT origin/<branch>
    commit (base_sha), not the clone's possibly-stale local branch ref.

    The old `checkout(repo, "main")` ran whatever the local main pointed at
    — often stale — while labelling the result with the fresh remote SHA, so
    a fix that already landed on main was reported as still-failing and
    poisoned the gate. Assert the baseline checks out a 40-hex SHA.
    """
    from robotsix_mill.vcs import git_ops

    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote, test_command="true", review_enabled="false"
    )

    calls: list[str] = []
    real_checkout = git_ops.checkout

    def _spy(repo, name):
        calls.append(name)
        real_checkout(repo, name)

    monkeypatch.setattr(git_ops, "checkout", _spy)
    # Fail the baseline so the run stops right after it (no real branch ops).
    monkeypatch.setattr(
        "robotsix_mill.stages.implement.run_test_agent",
        lambda **kw: (False, "pre-existing"),
    )
    monkeypatch.setattr(
        coding,
        "run_implement_agent",
        lambda *a, **kw: ("done", [], "", None, None, False, ""),
    )

    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")
    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    # A full SHA was checked out for the baseline — not the bare branch name.
    assert any(
        len(c) == 40 and all(ch in "0123456789abcdef" for ch in c) for c in calls
    ), calls


def test_baseline_check_proceeds_on_pass(ctx_factory, tmp_path, monkeypatch):
    """AC2: passing baseline → loop proceeds normally."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )

    agent_called = []

    def _fake_agent_run(
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
            previous_attempt_summary,
        )
        agent_called.append(1)
        (Path(repo_dir) / "feature.txt").write_text("done")
        return ("done", ["feature.txt"], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _fake_agent_run)

    # Baseline check passes via fake_sandbox (test_command="true" → rc=0).
    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.DOCUMENTING
    assert len(agent_called) == 1


def test_baseline_check_no_test_command(ctx_factory, tmp_path, monkeypatch):
    """AC3: no test_command → baseline passes trivially → loop proceeds."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="",  # empty → run_test_agent returns (True, ...)
        review_enabled="false",
    )

    agent_called = []

    def _fake_agent_run(
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
            previous_attempt_summary,
        )
        agent_called.append(1)
        (Path(repo_dir) / "feature.txt").write_text("done")
        return ("done", ["feature.txt"], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _fake_agent_run)

    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.DOCUMENTING
    assert len(agent_called) == 1

    # Cache must exist with passed=true.
    cache_path = ctx.service.workspace(t).artifacts_dir / "baseline_check.json"
    assert cache_path.exists()
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cache["passed"] is True
    assert "no test gate configured" in cache["diagnosis"]


def test_baseline_check_cached_on_retry(ctx_factory, tmp_path, monkeypatch):
    """AC4: cached baseline failure is reused on retry — no re-execution."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )

    call_count = [0]

    def _counted_test_agent(
        *, settings, repo_dir, repo_config=None, retry_on_failure=False
    ):
        call_count[0] += 1
        return False, "pre-existing failure"

    monkeypatch.setattr(
        "robotsix_mill.stages.implement.run_test_agent", _counted_test_agent
    )
    monkeypatch.setattr(coding, "run_implement_agent", _fake_agent(None))

    t = _ticket(ctx)

    # First run: baseline check runs, blocks.
    out1 = ImplementStage().run(t, ctx)
    assert out1.next_state is State.BLOCKED
    assert call_count[0] == 1

    # Second run (resume): cache hit, no re-execution.
    # The ticket is still BLOCKED; we simulate a resume by re-running
    # (the stage calls _clone_and_branch which will do a fresh clone,
    # but the cache is still on disk from the first run).
    out2 = ImplementStage().run(t, ctx)
    assert out2.next_state is State.BLOCKED
    assert call_count[0] == 1  # still 1 — no second invocation


def test_baseline_check_sha_invalidation(ctx_factory, tmp_path, monkeypatch):
    """AC5: cached failure with old SHA → re-runs when base advances."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )

    call_count = [0]
    # First call fails, second call passes (simulating operator fix).
    results = [(False, "old failure"), (True, "all passed")]

    def _counted_test_agent(
        *, settings, repo_dir, repo_config=None, retry_on_failure=False
    ):
        idx = min(call_count[0], len(results) - 1)
        passed, diag = results[idx]
        call_count[0] += 1
        return passed, diag

    monkeypatch.setattr(
        "robotsix_mill.stages.implement.run_test_agent", _counted_test_agent
    )

    t = _ticket(ctx)

    # First run: baseline check fails, caches result.
    out1 = ImplementStage().run(t, ctx)
    assert out1.next_state is State.BLOCKED
    assert call_count[0] == 1

    # Tamper with the cache: change the base_sha so it no longer
    # matches the current remote SHA.  This simulates the base
    # branch advancing.
    cache_path = ctx.service.workspace(t).artifacts_dir / "baseline_check.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    cache["base_sha"] = "0000000000000000000000000000000000000000"
    cache_path.write_text(json.dumps(cache), encoding="utf-8")

    # Now re-run: cache SHA mismatch → re-execute.
    # Also need to bypass the agent since this time the test passes.
    monkeypatch.setattr(
        coding,
        "run_implement_agent",
        _fake_agent({"feature.txt": "done"}),
    )

    out2 = ImplementStage().run(t, ctx)
    # The second call to _counted_test_agent returned (True, ...) → proceed.
    assert out2.next_state is State.DOCUMENTING
    # Baseline re-executed (call 2); per-iteration test gate may add more.
    assert call_count[0] >= 2  # re-executed

    # Cache updated with new result.
    cache2 = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cache2["passed"] is True


def test_baseline_check_sandbox_unavailable(ctx_factory, tmp_path, monkeypatch):
    """AC6: sandbox unavailable → BLOCKED with diagnostic."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )

    def _sandbox_error(*, settings, repo_dir, repo_config=None, retry_on_failure=False):
        return False, "sandbox unavailable: Docker daemon not running"

    monkeypatch.setattr("robotsix_mill.stages.implement.run_test_agent", _sandbox_error)

    t = _ticket(ctx)

    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "sandbox unavailable" in out.note

    # Result must be cached so retries don't re-attempt.
    cache_path = ctx.service.workspace(t).artifacts_dir / "baseline_check.json"
    assert cache_path.exists()
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cache["passed"] is False
    assert "sandbox unavailable" in cache["diagnosis"]


def test_baseline_gate_proceeds_when_dependency_fix_done(
    ctx_factory, tmp_path, monkeypatch
):
    """Idempotency: a ticket whose baseline-fix dependency has reached DONE
    for THIS base_sha must NOT re-spawn a duplicate fix — it proceeds.

    Without the guard, on re-entry origin/main is unchanged (the fix lives on
    its own unmerged branch) → same base_sha → cached/fresh FAILING result →
    a brand-new baseline-fix is spawned (the prior DONE one is invisible to
    the open-only dedup), wedging the ticket in an operator-only re-spawn
    cycle.
    """
    from robotsix_mill.core.models import SourceKind

    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )

    # Force a deterministic resolved base SHA so the title is predictable.
    base_sha = "a1b2c3d4" + "0" * 32
    monkeypatch.setattr(git_ops, "remote_branch_sha", lambda *a, **kw: base_sha)
    # The baseline test agent reports FAILING — without the guard this would
    # re-spawn a fix.
    monkeypatch.setattr(
        "robotsix_mill.stages.implement.run_test_agent",
        lambda **kw: (False, "pre-existing failure"),
    )

    fix_title = ImplementStage._baseline_fix_title(
        ctx.settings, base_sha, ctx.settings.forge_target_branch
    )
    fix = ctx.service.create(
        fix_title,
        "Repair the red base.",
        source=SourceKind.IMPLEMENT_BASELINE_DEPENDENCY,
    )
    ctx.service.transition(fix.id, State.DONE)

    t = ctx.service.create("Add feature", "Please add feature.txt")
    ctx.service.set_depends_on(t.id, [fix.id])
    ctx.service.transition(t.id, State.READY)
    t = ctx.service.get(t.id)

    before = len(
        ctx.service.recent_proposals_for(SourceKind.IMPLEMENT_BASELINE_DEPENDENCY)
    )

    out = ImplementStage._run_baseline_check(
        ctx, t, tmp_path, f"mill/{t.id}", False, ctx.settings
    )

    # Proceeds (no short-circuit Outcome) ...
    assert out is None
    # ... and no NEW baseline-fix ticket was spawned.
    after = len(
        ctx.service.recent_proposals_for(SourceKind.IMPLEMENT_BASELINE_DEPENDENCY)
    )
    assert after == before


def test_baseline_gate_spawns_when_dependency_fix_for_different_sha(
    ctx_factory, tmp_path, monkeypatch
):
    """The guard must NOT fire when the depended-on baseline-fix is for a
    DIFFERENT base_sha (different title) — normal gating still spawns/parks.
    """
    from robotsix_mill.core.models import SourceKind

    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )

    monkeypatch.setattr(
        "robotsix_mill.stages.implement.run_test_agent",
        lambda **kw: (False, "pre-existing failure"),
    )
    monkeypatch.setattr(
        coding,
        "run_implement_agent",
        lambda *a, **kw: ("done", [], "", None, None, False, ""),
    )

    # A DONE baseline-fix for an unrelated base_sha — its title differs from
    # the title computed for the real base, so the guard does not fire.
    other_sha = "deadbeef" + "0" * 32
    fix = ctx.service.create(
        ImplementStage._baseline_fix_title(
            ctx.settings, other_sha, ctx.settings.forge_target_branch
        ),
        "Repair some other red base.",
        source=SourceKind.IMPLEMENT_BASELINE_DEPENDENCY,
    )
    ctx.service.transition(fix.id, State.DONE)

    t = ctx.service.create("Add feature", "Please add feature.txt")
    ctx.service.set_depends_on(t.id, [fix.id])
    ctx.service.transition(t.id, State.READY)
    t = ctx.service.get(t.id)
    _write_file_map(ctx, t, "feature.txt")

    before = len(
        ctx.service.recent_proposals_for(SourceKind.IMPLEMENT_BASELINE_DEPENDENCY)
    )

    out = ImplementStage().run(t, ctx)

    # Normal gating: pre-existing failure parks the ticket BLOCKED ...
    assert out.next_state is State.BLOCKED
    assert "pre-existing test failures" in out.note
    # ... and a NEW baseline-fix was spawned (guard did not suppress it).
    after = len(
        ctx.service.recent_proposals_for(SourceKind.IMPLEMENT_BASELINE_DEPENDENCY)
    )
    assert after == before + 1


# --- misc helper --------------------------------------------------------


def _clone_repo_to(ctx, remote_url, repo_dir):
    """Clone to *repo_dir* without the full stage machinery."""
    from robotsix_mill.forge.auth import github_token
    from robotsix_mill.vcs import git_ops

    if repo_dir.exists():
        import shutil

        shutil.rmtree(repo_dir)
    token = None
    with contextlib.suppress(RuntimeError):
        token = github_token(ctx.settings, repo_config=ctx.repo_config)
    git_ops.clone(remote_url, repo_dir, ctx.settings.forge_target_branch, token)


# --- prerequisite gate --------------------------------------------------


def _no_prereq_block_spec():
    return "## Problem\nDo a thing.\n## Acceptance criteria\n- works\n"


def test_prereq_gate_disabled_never_checks(ctx_factory, tmp_path, monkeypatch):
    """Gate explicitly disabled: run_prerequisite_check is never called and
    behaviour is unchanged — the stage proceeds to the agent."""
    from robotsix_mill.agents import prerequisite

    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        prerequisite_gate_enabled="false",
    )
    assert ctx.settings.prerequisite_gate_enabled is False

    called = {"n": 0}

    def _spy(*a, **kw):
        called["n"] += 1
        return {"unmet": [], "reason": "x"}

    monkeypatch.setattr(prerequisite, "run_prerequisite_check", _spy)
    monkeypatch.setattr(
        coding, "run_implement_agent", _fake_agent({"feature.txt": "x"})
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING
    assert called["n"] == 0


def test_prereq_gate_unmet_blocks_without_agent(ctx_factory, tmp_path, monkeypatch):
    """Gate enabled + an unmet prerequisite → BLOCKED, naming the
    directive, WITHOUT invoking run_implement_agent."""
    from robotsix_mill.agents import prerequisite

    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        prerequisite_gate_enabled="true",
    )

    monkeypatch.setattr(
        prerequisite,
        "run_prerequisite_check",
        lambda *a, **kw: {
            "unmet": ["symbol CostLogSource from robotsix_llmio"],
            "reason": "unmet",
        },
    )

    def _boom(*a, **kw):
        raise AssertionError("run_implement_agent must NOT be called")

    monkeypatch.setattr(coding, "run_implement_agent", _boom)
    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "CostLogSource" in out.note
    assert "prerequisite" in out.note.lower()


def test_prereq_gate_default_activation_blocks_without_agent(
    ctx_factory, tmp_path, monkeypatch
):
    """Flag left at its NEW default (not set explicitly) + an unmet
    prerequisite → BLOCKED, WITHOUT invoking run_implement_agent. Proves
    the default activation works end-to-end."""
    from robotsix_mill.agents import prerequisite

    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )
    # The flag is on by default now — the test must NOT set it.
    assert ctx.settings.prerequisite_gate_enabled is True

    monkeypatch.setattr(
        prerequisite,
        "run_prerequisite_check",
        lambda *a, **kw: {
            "unmet": ["symbol CostLogSource from robotsix_llmio"],
            "reason": "unmet",
        },
    )

    def _boom(*a, **kw):
        raise AssertionError("run_implement_agent must NOT be called")

    monkeypatch.setattr(coding, "run_implement_agent", _boom)
    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "CostLogSource" in out.note
    assert "prerequisite" in out.note.lower()


def test_prereq_gate_met_proceeds(ctx_factory, tmp_path, monkeypatch):
    """Gate enabled + all prerequisites met → stage proceeds to the
    agent exactly as before."""
    from robotsix_mill.agents import prerequisite

    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        prerequisite_gate_enabled="true",
    )
    monkeypatch.setattr(
        prerequisite,
        "run_prerequisite_check",
        lambda *a, **kw: {"unmet": [], "reason": "ok"},
    )
    monkeypatch.setattr(
        coding, "run_implement_agent", _fake_agent({"feature.txt": "x"})
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING


def test_prereq_gate_best_effort_on_error(ctx_factory, tmp_path, monkeypatch):
    """Gate enabled but run_prerequisite_check raises → stage logs a
    warning and proceeds (best-effort), rather than blocking."""
    from robotsix_mill.agents import prerequisite

    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        prerequisite_gate_enabled="true",
    )

    def _boom(*a, **kw):
        raise RuntimeError("checker exploded")

    monkeypatch.setattr(prerequisite, "run_prerequisite_check", _boom)
    monkeypatch.setattr(
        coding, "run_implement_agent", _fake_agent({"feature.txt": "x"})
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING


# ---------------------------------------------------------------------------
# prepare hook integration tests
# ---------------------------------------------------------------------------


def test_prepare_hook_failure_blocks_before_prerequisite_gate(
    ctx_factory,
    tmp_path,
    monkeypatch,
):
    """When ``run_prepare_hook`` returns an error, implement short-circuits
    to BLOCKED with that error BEFORE the prerequisite gate runs."""
    from robotsix_mill.agents import prerequisite

    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        prerequisite_gate_enabled="true",
    )

    prereq_called = []

    def _spy_prereq(*args, **kwargs):
        prereq_called.append(1)

    monkeypatch.setattr(
        prerequisite,
        "run_prerequisite_check",
        _spy_prereq,
    )
    monkeypatch.setattr(
        ImplementStage,
        "_run_prerequisite_gate",
        _spy_prereq,
    )

    from robotsix_mill.stages import hooks as hooks_mod

    monkeypatch.setattr(
        hooks_mod,
        "run_prepare_hook",
        lambda repo_dir, ticket_id, workspace_dir: (
            "prepare hook exited 2: setup failed"
        ),
    )

    t = _ticket(ctx)
    _write_file_map(ctx, t, "dummy.txt")

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "prepare hook exited 2" in out.note
    assert "setup failed" in out.note
    # Prerequisite gate must NOT have been called — the hook blocked first.
    assert len(prereq_called) == 0


# ── gitignored-edit detection (manifest boards: writes git can't see) ──


def test_claimed_gitignored_edits_detects_invisible_writes(tmp_path):
    """Edit tool-calls that landed in a gitignored sub-tree (the
    robotsix-mill-ros2 ``/src/*`` manifest layout) are named, so the
    'no changes produced' block tells the operator WHAT happened."""
    import json as _json

    remote = make_bare_repo(tmp_path)
    repo_dir = tmp_path / "clone"
    git_ops.clone(remote, repo_dir, "main")
    (repo_dir / ".gitignore").write_text("/src/*\n")
    git_ops.commit_all(repo_dir, "ignore vendored sources")
    target = repo_dir / "src" / "pkg" / "msg"
    target.mkdir(parents=True)
    (target / "Status.msg").write_text("int32 code\n")
    (repo_dir / "tracked.txt").write_text("visible\n")

    msgs = _json.dumps(
        [
            {
                "parts": [
                    {
                        "part_kind": "tool-call",
                        "tool_name": "write_file",
                        "args": {"path": "src/pkg/msg/Status.msg"},
                        "tool_call_id": "c1",
                    },
                    {
                        "part_kind": "tool-call",
                        "tool_name": "write_file",
                        "args": {"path": "tracked.txt"},
                        "tool_call_id": "c2",
                    },
                    {
                        "part_kind": "tool-call",
                        "tool_name": "Write",
                        # absolute path INSIDE the clone (Claude SDK style)
                        "args": {"file_path": str(target / "Status.msg")},
                        "tool_call_id": "c3",
                    },
                    {
                        "part_kind": "tool-call",
                        "tool_name": "Write",
                        # absolute path OUTSIDE the clone → skipped
                        "args": {"file_path": "/etc/hosts"},
                        "tool_call_id": "c4",
                    },
                ]
            }
        ]
    ).encode()

    hits = ImplementStage._claimed_gitignored_edits(repo_dir, msgs)
    assert hits == ["src/pkg/msg/Status.msg"]


def test_claimed_gitignored_edits_fail_open(tmp_path):
    """Malformed input never raises — the detector only enriches notes."""
    assert ImplementStage._claimed_gitignored_edits(tmp_path, b"{bad") == []
    assert ImplementStage._claimed_gitignored_edits(tmp_path, None) == []


def test_scope_triage_new_file_summary_shows_content(
    ctx_factory, tmp_path, monkeypatch
):
    """NEW (untracked) out-of-scope files have an empty ``git diff`` vs the
    base; the triage agent then sees no content and ESCALATEs blindly (live
    case: the worker.py package refactor cb63 — every new submodule
    summarized empty). The summary must fall back to the file head."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        max_fix_iterations="3",
    )
    t = _ticket(ctx)

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "file_map.json").write_text(
        '[{"file": "wip.txt", "note": "only this file"}]',
        encoding="utf-8",
    )

    def _run(*, settings, repo_dir, spec, **_kwargs):
        del settings, spec
        (Path(repo_dir) / "wip.txt").write_text("in scope")
        # Brand-new file, never tracked → empty `git diff origin/main -- f`.
        (Path(repo_dir) / "brand_new_module.py").write_text(
            "def shiny_new_helper():\n    return 42\n"
        )
        return ("edit done", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    import robotsix_mill.agents.scope_triage as scope_triage_mod
    from robotsix_mill.agents.scope_triage import ScopeTriageVerdict

    captured: dict = {}

    def _fake_triage(
        *, settings, ticket_spec, file_map, out_of_scope_files, diff_summaries
    ):
        captured["summaries"] = dict(diff_summaries)
        return ScopeTriageVerdict(
            action="ESCALATE",
            justification="capture only",
            expand_files=[],
        )

    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _fake_triage)

    ImplementStage().run(t, ctx)

    summary = captured["summaries"]["brand_new_module.py"]
    assert "NEW FILE" in summary
    assert "shiny_new_helper" in summary


# ------------------------------------------------------------------


def test_convergence_backstop_halts_at_cycle_cap(ctx_factory, tmp_path, monkeypatch):
    """The preflight gate escalates to BLOCKED when
    ``implement_cycles`` reaches ``max_implement_review_cycles``.
    """
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        max_implement_review_cycles="2",
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    # Seed the counter at the cap so preflight trips it.
    ctx.service.set_implement_cycles(t.id, 2)
    # Reload so ticket.implement_cycles reflects the set value.
    t = ctx.service.get(t.id)
    assert t.implement_cycles == 2

    out = ImplementStage().preflight(t, ctx)

    assert out is not None
    assert out.next_state is State.BLOCKED
    assert "cycle limit reached" in out.note.lower()
    assert "2/2" in out.note


def test_convergence_empty_diff_after_review_terminates_done(
    ctx_factory, tmp_path, monkeypatch
):
    """When a ticket returns from review (review_rounds > 0) and the
    branch has no commits beyond origin/main, there is genuinely nothing
    to merge — implement terminates DONE (already satisfied) instead of
    looping in BLOCKED (ticket 0976).
    """
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="true",
        max_implement_review_cycles="10",
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    # Bypass gates that require a real sandbox / API key.
    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    # Run implement once so the branch exists (creating the clone).
    # The agent produces a simple change that gets committed.
    def _run_once(*, repo_dir, **_kwargs):
        (Path(repo_dir) / "feature.txt").write_text("implemented")
        return ("done", ["feature.txt"], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run_once)
    out1 = ImplementStage().run(t, ctx)
    # With review_enabled=True, the first pass should proceed to CODE_REVIEW.
    assert out1.next_state is State.CODE_REVIEW

    # Now simulate returning from review: set review_rounds > 0 and
    # RESET the branch so it has no commits beyond origin/main.
    t = ctx.service.get(t.id)
    ctx.service.set_review_rounds(t.id, 1)
    ws = ctx.service.workspace(t)
    repo_dir = ws.dir / "repo"
    branch = f"{ctx.settings.branch_prefix}{t.id}"
    target = "main"
    subprocess.run(
        ["git", "-C", str(repo_dir), "reset", "--hard", f"origin/{target}"],
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

    # Second implement run: resuming=True, review_rounds>0, branch has no
    # commits ahead → genuine no-op → terminate DONE (already satisfied).
    out2 = ImplementStage().run(t, ctx)
    assert out2.next_state is State.DONE
    assert "already satisfied" in out2.note.lower()
    assert "empty diff" in out2.note.lower()


# --- spec emptiness precondition ----------------------------------------


def test_empty_spec_blocks_before_agent(ctx_factory, tmp_path, monkeypatch):
    """When the ticket spec is empty, implement blocks BEFORE invoking
    the coordinator agent — no paid re-spawn, no $0.00 trace."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Empty spec ticket", body="")
    _write_file_map(ctx, t, "feature.txt")

    # Track whether the agent was ever invoked.
    agent_called = False

    def _track(*, repo_dir, spec, **kwargs):
        nonlocal agent_called
        agent_called = True
        return ("done", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _track)

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "spec is empty" in out.note.lower()
    assert not agent_called, "agent must not be invoked for empty spec"


def test_whitespace_only_spec_blocks_before_agent(ctx_factory, tmp_path, monkeypatch):
    """A spec that is only whitespace is treated as empty and blocks."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Whitespace spec", body="\n  \n\t\n")
    _write_file_map(ctx, t, "feature.txt")

    agent_called = False

    def _track(*, repo_dir, spec, **kwargs):
        nonlocal agent_called
        agent_called = True
        return ("done", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _track)

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "spec is empty" in out.note.lower()
    assert not agent_called


def test_non_empty_spec_proceeds_normally(ctx_factory, tmp_path, monkeypatch):
    """A non-empty spec must still reach the agent (no regression)."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Normal ticket", body="Add a feature.txt file")
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    agent_called = False

    def _agent(*, repo_dir, spec, **kwargs):
        nonlocal agent_called
        agent_called = True
        (Path(repo_dir) / "feature.txt").write_text("done")
        return ("did the thing", ["feature.txt"], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _agent)

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.DOCUMENTING
    assert agent_called, "agent must be invoked for non-empty spec"


# --- implement spawn counter --------------------------------------------


def test_spawn_counter_blocks_after_limit(ctx_factory, tmp_path, monkeypatch):
    """After ``implement_max_spawns_per_ticket`` entries, the preflight
    gate blocks BEFORE a trace opens or the agent is invoked."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        implement_max_spawns_per_ticket="1",
    )
    t = _ticket(ctx, title="Spawn cap ticket", body="Add a feature.txt file")
    _write_file_map(ctx, t, "feature.txt")

    # Seed the counter at the limit — next preflight trips the gate.
    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "implement_spawn_count").write_text("1", encoding="utf-8")
    # Also seed a stale conversation state — the block should clear it.
    conv_state = ws.artifacts_dir / "implement_conversation_state.json"
    conv_state.write_text('{"messages":[]}')

    out = ImplementStage().preflight(t, ctx)

    assert out is not None
    assert out.next_state is State.BLOCKED
    assert "spawn limit reached" in out.note.lower()
    # Conversation state must be cleared so a resume starts fresh.
    assert not conv_state.exists()


def test_spawn_counter_increments_each_run(ctx_factory, tmp_path, monkeypatch):
    """Each preflight call increments the spawn counter file only when
    retry_attempt == 0 (genuine re-spawn). Transient retries must not
    burn spawn slots."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        implement_max_spawns_per_ticket="5",
    )
    t = _ticket(ctx, title="Counter ticket", body="Add a feature.txt file")
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    _call_count = 0

    def _agent(*, repo_dir, spec, **kwargs):
        nonlocal _call_count
        _call_count += 1
        (Path(repo_dir) / "feature.txt").write_text(f"done round {_call_count}")
        return (f"done round {_call_count}", ["feature.txt"], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _agent)

    # First invocation: preflight increments counter from 0 to 1
    # (retry_attempt == 0 → genuine re-spawn).
    pre1 = ImplementStage().preflight(t, ctx)
    assert pre1 is None, "preflight should proceed"

    out1 = ImplementStage().run(t, ctx)
    assert out1.next_state is State.DOCUMENTING

    ws = ctx.service.workspace(t)
    counter_path = ws.artifacts_dir / "implement_spawn_count"
    assert counter_path.exists()
    assert counter_path.read_text(encoding="utf-8").strip() == "1"

    # Reset the ticket to READY for a second invocation.
    ctx.service.transition(t.id, State.BLOCKED, "test reset")
    ctx.service.transition(t.id, State.READY, "test reset")
    t = ctx.service.get(t.id)

    # Second invocation: preflight increments counter from 1 to 2.
    pre2 = ImplementStage().preflight(t, ctx)
    assert pre2 is None, "preflight should proceed"

    out2 = ImplementStage().run(t, ctx)
    assert out2.next_state is State.DOCUMENTING

    assert counter_path.read_text(encoding="utf-8").strip() == "2"

    # --- transient retry: counter must NOT increment ---
    ctx.service.transition(t.id, State.BLOCKED, "test reset")
    ctx.service.transition(t.id, State.READY, "test reset")
    # Simulate a transient retry by setting retry_attempt > 0.
    ctx.service.set_retry_state(
        t.id,
        retry_attempt=2,
        last_transient_error="sandbox EOF",
        next_retry_at=None,
    )
    t = ctx.service.get(t.id)
    assert t.retry_attempt == 2

    pre3 = ImplementStage().preflight(t, ctx)
    assert pre3 is None, "preflight should still proceed on retry"

    # Counter must still be 2 — transient retry does NOT burn a spawn slot.
    assert counter_path.read_text(encoding="utf-8").strip() == "2"


def test_spawn_counter_disabled_when_set_to_zero(ctx_factory, tmp_path, monkeypatch):
    """When ``implement_max_spawns_per_ticket=0`` the counter gate is
    skipped entirely."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        implement_max_spawns_per_ticket="0",
    )
    t = _ticket(ctx, title="Unlimited spawns", body="Add a feature.txt file")
    _write_file_map(ctx, t, "feature.txt")

    # Seed a counter at 999 — should be ignored since limit is 0 (disabled).
    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "implement_spawn_count").write_text("999", encoding="utf-8")

    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    agent_called = False

    def _agent(*, repo_dir, spec, **kwargs):
        nonlocal agent_called
        agent_called = True
        (Path(repo_dir) / "feature.txt").write_text("done")
        return ("done", ["feature.txt"], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _agent)

    pre = ImplementStage().preflight(t, ctx)
    assert pre is None, "preflight should proceed when counter is disabled"

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.DOCUMENTING
    assert agent_called, "agent must be invoked when counter is disabled"


def test_spawn_counter_blocked_note_includes_summary_tail(
    ctx_factory, tmp_path, monkeypatch
):
    """When the spawn limit is reached, the BLOCKED outcome note
    includes the tail of artifacts/implement_summary.md so the operator
    sees the genuine failure cause."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        implement_max_spawns_per_ticket="1",
    )
    t = _ticket(ctx, title="Spawn cap with summary", body="Add a feature.txt file")
    _write_file_map(ctx, t, "feature.txt")

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "implement_spawn_count").write_text("1", encoding="utf-8")
    summary = (
        "## Implement result\n\n"
        "The agent failed because of a level-3 Claude SDK error:\n"
        "RunContext tools are not available in this environment.\n"
        "Try upgrading the SDK or using a different model tier.\n"
    )
    (ws.artifacts_dir / "implement_summary.md").write_text(summary, encoding="utf-8")
    # Seed a stale conversation state — spawn-limit block must clear it.
    conv_state = ws.artifacts_dir / "implement_conversation_state.json"
    conv_state.write_text('{"messages":[]}')

    out = ImplementStage().preflight(t, ctx)

    assert out is not None
    assert out.next_state is State.BLOCKED
    assert "spawn limit reached" in out.note.lower()
    assert "Last attempt summary tail:" in out.note
    assert "RunContext tools are not available" in out.note
    assert not conv_state.exists()


def test_spawn_counter_blocked_note_no_summary_when_file_missing(ctx_factory, tmp_path):
    """When implement_summary.md does not exist, the BLOCKED note
    does not include a summary tail section."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        implement_max_spawns_per_ticket="1",
    )
    t = _ticket(ctx, title="Spawn cap no summary", body="Add a feature.txt file")
    _write_file_map(ctx, t, "feature.txt")

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "implement_spawn_count").write_text("1", encoding="utf-8")
    # No implement_summary.md written.

    out = ImplementStage().preflight(t, ctx)

    assert out is not None
    assert out.next_state is State.BLOCKED
    assert "spawn limit reached" in out.note.lower()
    assert "Last attempt summary tail:" not in out.note


def test_spawn_limit_exhausted_emits_diagnostic_event(
    ctx_factory, tmp_path, monkeypatch
):
    """When the spawn limit is reached, a SPAWN_LIMIT_EXHAUSTED diagnostic
    event is emitted so agents can discover the exhaustion programmatically."""
    from robotsix_mill.agents.runners.diagnostic_events import list_diagnostic_events

    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        implement_max_spawns_per_ticket="1",
    )
    t = _ticket(ctx, title="Spawn cap event", body="Add a feature.txt file")
    _write_file_map(ctx, t, "feature.txt")

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "implement_spawn_count").write_text("1", encoding="utf-8")
    (ws.artifacts_dir / "implement_summary.md").write_text(
        "## Implement result\n\nFailed.\n", encoding="utf-8"
    )

    out = ImplementStage().preflight(t, ctx)

    assert out is not None
    assert out.next_state is State.BLOCKED
    assert "spawn limit reached" in out.note.lower()

    events = list_diagnostic_events(ctx.settings, "test-board")
    spawn_events = [e for e in events if e.category == "SPAWN_LIMIT_EXHAUSTED"]
    assert len(spawn_events) == 1
    assert spawn_events[0].ticket_id == t.id
    assert "spawn limit reached" in spawn_events[0].reason.lower()
    assert spawn_events[0].normalized_key.startswith("spawn_limit_exhausted:")


def test_recurring_spawn_exhaustion_emits_recurring_event(
    ctx_factory, tmp_path, monkeypatch
):
    """When the spawn limit is reached for the second consecutive time
    with an UNCHANGED spec, a RECURRING_SPAWN_EXHAUSTION diagnostic
    event is emitted instead of the plain SPAWN_LIMIT_EXHAUSTED."""
    from robotsix_mill.agents.runners.diagnostic_events import list_diagnostic_events
    from robotsix_mill.core.workspace import record_spawn_exhaustion_marker

    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        implement_max_spawns_per_ticket="1",
    )
    t = _ticket(ctx, title="Recurring spawn", body="Add a feature.txt file")
    _write_file_map(ctx, t, "feature.txt")

    ws = ctx.service.workspace(t)
    # Counter at limit.
    (ws.artifacts_dir / "implement_spawn_count").write_text("1", encoding="utf-8")
    # Pre-seed the marker as if the ticket already exhausted once
    # on the same spec fingerprint.  The effective fingerprint for
    # a non-epic ticket is sha256(description)[:16].
    effective_desc = "Add a feature.txt file"
    fp = hashlib.sha256(effective_desc.encode("utf-8")).hexdigest()[:16]
    record_spawn_exhaustion_marker(ws, fp, 1)

    out = ImplementStage().preflight(t, ctx)

    events = list_diagnostic_events(ctx.settings, "test-board")
    recurring_events = [e for e in events if e.category == "RECURRING_SPAWN_EXHAUSTION"]
    assert len(recurring_events) == 1
    assert recurring_events[0].ticket_id == t.id
    assert "unchanged spec" in recurring_events[0].reason.lower()
    assert recurring_events[0].normalized_key.startswith("spawn_limit_exhausted:")
    # Verify no plain event also emitted.
    spawn_events = [e for e in events if e.category == "SPAWN_LIMIT_EXHAUSTED"]
    assert len(spawn_events) == 0

    assert out is not None
    assert out.next_state is State.BLOCKED
    assert "counter will not be auto-reset" in out.note.lower()


def test_first_spawn_exhaustion_emits_plain_event(ctx_factory, tmp_path, monkeypatch):
    """When the spawn limit is reached for the first time, a plain
    SPAWN_LIMIT_EXHAUSTED event is emitted (not RECURRING) and the
    block note still mentions auto-reset."""
    from robotsix_mill.agents.runners.diagnostic_events import list_diagnostic_events

    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        implement_max_spawns_per_ticket="1",
    )
    t = _ticket(ctx, title="First spawn", body="Add a feature.txt file")
    _write_file_map(ctx, t, "feature.txt")

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "implement_spawn_count").write_text("1", encoding="utf-8")
    # No marker pre-seeded — first exhaustion.

    out = ImplementStage().preflight(t, ctx)

    events = list_diagnostic_events(ctx.settings, "test-board")
    spawn_events = [e for e in events if e.category == "SPAWN_LIMIT_EXHAUSTED"]
    assert len(spawn_events) == 1
    recurring_events = [e for e in events if e.category == "RECURRING_SPAWN_EXHAUSTION"]
    assert len(recurring_events) == 0

    assert out is not None
    assert out.next_state is State.BLOCKED
    assert "resume-blocked to retry" in out.note.lower()
    assert "clears the counter automatically" in out.note.lower()


def test_spawn_exhaustion_captures_tool_outputs_before_clearing_state(
    ctx_factory, tmp_path, monkeypatch
):
    """When the spawn limit is reached and a conversation state exists
    with tool-return parts, the tool outputs are captured to a durable
    artifact BEFORE the conversation state is cleared, and the block
    note includes a tail of those outputs."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        implement_max_spawns_per_ticket="1",
    )
    t = _ticket(ctx, title="Tool capture", body="Add a feature.txt file")
    _write_file_map(ctx, t, "feature.txt")

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "implement_spawn_count").write_text("1", encoding="utf-8")

    # Write a conversation state JSON with tool-return parts.
    conv_state = ws.artifacts_dir / "implement_conversation_state.json"
    messages = [
        {
            "parts": [
                {
                    "part_kind": "tool-return",
                    "tool_call_id": "call_1",
                    "content": "exit=1\nruff failed: E501 line too long",
                },
                {
                    "part_kind": "tool-return",
                    "tool_call_id": "call_2",
                    "content": "module-registration: 2 files unclassified",
                },
                {
                    "part_kind": "tool-return",
                    "tool_call_id": "call_3",
                    "content": "No changes detected in working tree.",
                },
            ]
        }
    ]
    conv_state.write_text(json.dumps(messages), encoding="utf-8")

    out = ImplementStage().preflight(t, ctx)

    assert out is not None
    assert out.next_state is State.BLOCKED

    # The conversation state must be cleared (file deleted).
    assert not conv_state.exists(), "conversation state should be cleared"

    # The durable tool-output artifact must exist.
    artifact = ws.artifacts_dir / "implement_tool_outputs.md"
    assert artifact.exists(), "tool outputs artifact should exist"
    artifact_text = artifact.read_text(encoding="utf-8")
    assert "ruff failed" in artifact_text
    assert "module-registration" in artifact_text
    assert "No changes detected" in artifact_text

    # The block note must reference the tool outputs.
    assert "Last attempt tool outputs:" in out.note
    assert "ruff failed" in out.note or "module-registration" in out.note


def test_spawn_exhaustion_no_conversation_state_is_harmless(
    ctx_factory, tmp_path, monkeypatch
):
    """When the spawn limit is reached but no conversation state file
    exists, the exhaustion path proceeds without error and no tool
    output artifact is created."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        implement_max_spawns_per_ticket="1",
    )
    t = _ticket(ctx, title="No conv state", body="Add a feature.txt file")
    _write_file_map(ctx, t, "feature.txt")

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "implement_spawn_count").write_text("1", encoding="utf-8")
    # No conversation state file — simulate a case where it was
    # already cleared or never created.

    out = ImplementStage().preflight(t, ctx)

    assert out is not None
    assert out.next_state is State.BLOCKED
    assert "spawn limit reached" in out.note.lower()
    # No tool output artifact should be created.
    artifact = ws.artifacts_dir / "implement_tool_outputs.md"
    assert not artifact.exists()


# --- pre-LLM spawn abort visibility (process-death kill recovery) --------


def test_killed_inflight_spawn_is_absorbed_not_counted(
    ctx_factory, tmp_path, monkeypatch
):
    """A stale in-flight marker (previous attempt killed by process
    shutdown) is absorbed before the limit check: the killed attempt
    does NOT burn a spawn slot, so a counter at the limit proceeds."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        implement_max_spawns_per_ticket="1",
    )
    t = _ticket(ctx, title="Killed spawn", body="Add a feature.txt file")
    _write_file_map(ctx, t, "feature.txt")

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "implement_spawn_count").write_text("1", encoding="utf-8")
    # Stale marker: attempt #1 was killed mid-flight.
    (ws.artifacts_dir / "implement_spawn_state.json").write_text(
        '{"state": "in_flight", "started_at": "2026-08-16T20:42:00+00:00", '
        '"spawn_count": 1, "counted": true}',
        encoding="utf-8",
    )

    out = ImplementStage().preflight(t, ctx)

    # The killed attempt was absorbed: counter went 1 -> 0, so the
    # limit check passes and preflight proceeds (no block).
    assert out is None, f"killed spawn should not consume a slot: {out}"

    counter = (
        (ws.artifacts_dir / "implement_spawn_count").read_text(encoding="utf-8").strip()
    )
    # Proceeding preflight re-increments: absorbed kill (-1) then new
    # attempt (+1) → back to 1, but the BLOCK did not fire.
    assert counter == "1"

    # The abort was recorded durably.
    aborts = (ws.artifacts_dir / "implement_spawn_aborts.jsonl").read_text(
        encoding="utf-8"
    )
    assert "2026-08-16T20:42:00+00:00" in aborts
    assert '"counted": true' in aborts


def test_killed_inflight_spawn_block_note_carries_evidence(
    ctx_factory, tmp_path, monkeypatch
):
    """When the limit fires despite kill absorption (counter still at
    limit after decrement), the block note carries the kill evidence —
    spawn-limit blocks are never evidence-free."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        implement_max_spawns_per_ticket="1",
    )
    t = _ticket(ctx, title="Killed spawn block", body="Add a feature.txt file")
    _write_file_map(ctx, t, "feature.txt")

    ws = ctx.service.workspace(t)
    # Two counted attempts, one of which was killed mid-flight.
    (ws.artifacts_dir / "implement_spawn_count").write_text("2", encoding="utf-8")
    (ws.artifacts_dir / "implement_spawn_state.json").write_text(
        '{"state": "in_flight", "started_at": "2026-08-16T20:42:00+00:00", '
        '"spawn_count": 2, "counted": true}',
        encoding="utf-8",
    )

    out = ImplementStage().preflight(t, ctx)

    assert out is not None
    assert out.next_state is State.BLOCKED
    assert "spawn limit reached" in out.note.lower()
    # The block note must include the kill evidence.
    assert "killed by process shutdown" in out.note
    assert "2026-08-16T20:42:00+00:00" in out.note


def test_uncounted_killed_spawn_does_not_decrement_counter(
    ctx_factory, tmp_path, monkeypatch
):
    """A killed in-flight marker for an UNCCOUNTED retry (retry_attempt
    > 0) logs the abort but must NOT decrement the spawn counter."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        implement_max_spawns_per_ticket="3",
    )
    t = _ticket(ctx, title="Uncounted kill", body="Add a feature.txt file")
    _write_file_map(ctx, t, "feature.txt")

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "implement_spawn_count").write_text("1", encoding="utf-8")
    (ws.artifacts_dir / "implement_spawn_state.json").write_text(
        '{"state": "in_flight", "started_at": "2026-08-16T20:42:00+00:00", '
        '"spawn_count": 1, "counted": false}',
        encoding="utf-8",
    )
    # Simulate a transient retry dispatch (retry_attempt > 0): preflight
    # must not increment the counter on this pass.
    ctx.service.set_retry_state(
        t.id,
        retry_attempt=1,
        last_transient_error="sandbox EOF",
        next_retry_at=None,
    )
    t = ctx.service.get(t.id)

    out = ImplementStage().preflight(t, ctx)

    assert out is None, f"counter below limit should proceed: {out}"
    # Counter untouched — the killed retry was not counted.
    assert (ws.artifacts_dir / "implement_spawn_count").read_text(
        encoding="utf-8"
    ).strip() == "1"
    # But the abort is still logged.
    aborts = (ws.artifacts_dir / "implement_spawn_aborts.jsonl").read_text(
        encoding="utf-8"
    )
    assert '"counted": false' in aborts


def test_inflight_marker_cleared_after_run_completes(
    ctx_factory, tmp_path, monkeypatch
):
    """After a successful implement run, the in-flight spawn marker is
    cleared so the next preflight doesn't misread a completed attempt
    as a process-death kill."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        implement_max_spawns_per_ticket="3",
    )
    t = _ticket(ctx, title="Marker cleared", body="Add a feature.txt file")
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    def _agent(*, repo_dir, spec, **kwargs):
        (Path(repo_dir) / "feature.txt").write_text("done")
        return ("done", ["feature.txt"], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _agent)

    ws = ctx.service.workspace(t)

    pre = ImplementStage().preflight(t, ctx)
    assert pre is None
    # Marker written by preflight.
    assert (ws.artifacts_dir / "implement_spawn_state.json").exists()

    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING

    # Marker cleared by the run() completion hook.
    assert not (ws.artifacts_dir / "implement_spawn_state.json").exists()


def test_inflight_marker_cleared_after_run_raises(ctx_factory, tmp_path, monkeypatch):
    """When implement run raises (worker records the error durably),
    the marker is still cleared — the attempt reached a recorded
    terminal state and must not be misread as a silent kill."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        implement_max_spawns_per_ticket="3",
    )
    t = _ticket(ctx, title="Marker cleared on raise", body="Add a feature.txt file")
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    ws = ctx.service.workspace(t)

    pre = ImplementStage().preflight(t, ctx)
    assert pre is None
    assert (ws.artifacts_dir / "implement_spawn_state.json").exists()

    def _boom(*a, **kw):
        raise RuntimeError("agent exploded")

    monkeypatch.setattr(coding, "run_implement_agent", _boom)

    with pytest.raises(RuntimeError, match="agent exploded"):
        ImplementStage().run(t, ctx)

    # Marker cleared even on raise — the worker's error handler records
    # the failure, so the attempt is not silent.
    assert not (ws.artifacts_dir / "implement_spawn_state.json").exists()


def test_resume_blocked_clears_spawn_state_and_aborts(
    ctx_factory, tmp_path, monkeypatch
):
    """resume-blocked at the spawn limit clears the spawn counter AND
    the spawn-state ledger (in-flight marker + abort log) so the
    operator's intervention grants a clean budget."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
        implement_max_spawns_per_ticket="1",
    )
    t = _ticket(ctx, title="Resume clears ledger", body="Add a feature.txt file")
    _write_file_map(ctx, t, "feature.txt")

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "implement_spawn_count").write_text("1", encoding="utf-8")
    (ws.artifacts_dir / "implement_spawn_state.json").write_text(
        '{"state": "in_flight", "started_at": "2026-08-16T20:42:00+00:00", '
        '"spawn_count": 1, "counted": true}',
        encoding="utf-8",
    )
    (ws.artifacts_dir / "implement_spawn_aborts.jsonl").write_text(
        '{"started_at": "2026-08-16T20:42:00+00:00", "spawn_count": 1, '
        '"detected_at": "2026-08-16T20:43:00+00:00", "counted": true}\n',
        encoding="utf-8",
    )

    # Block the ticket from READY (spawn limit) then resume it.
    ctx.service.transition(t.id, State.BLOCKED, "implement spawn limit reached")
    resumed = ctx.service.resume_blocked(t.id, note="operator retry")
    assert resumed.state is State.READY

    assert not (ws.artifacts_dir / "implement_spawn_count").exists()
    assert not (ws.artifacts_dir / "implement_spawn_state.json").exists()
    assert not (ws.artifacts_dir / "implement_spawn_aborts.jsonl").exists()


# --- epic context in preflight spec check --------------------------------


def test_preflight_epic_context_allows_empty_direct_spec(
    ctx_factory, tmp_path, monkeypatch
):
    """An epic child with an empty direct body but non-empty parent epic
    must pass the preflight spec gate (epic context inherited)."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )
    # Parent epic with real content.
    epic = ctx.service.create(
        "Epic parent", "Build the login system", kind=TicketKind.EPIC
    )
    # Child with empty body — spec inherited from epic.
    child = ctx.service.create("Epic child", "", parent_id=epic.id)
    _write_file_map(ctx, child, "feature.txt")

    # preflight should NOT block — epic context provides the spec.
    out = ImplementStage().preflight(child, ctx)
    assert out is None, f"epic context should satisfy spec gate, got: {out}"


def test_preflight_blocks_when_both_spec_and_epic_empty(
    ctx_factory, tmp_path, monkeypatch
):
    """When BOTH the direct spec AND the epic context are empty,
    preflight must block."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )
    # Parent epic with empty body.
    epic = ctx.service.create("Empty epic", "", kind=TicketKind.EPIC)
    # Child with empty body — no spec from either source.
    child = ctx.service.create("Empty child", "", parent_id=epic.id)
    _write_file_map(ctx, child, "feature.txt")

    out = ImplementStage().preflight(child, ctx)
    assert out is not None
    assert out.next_state is State.BLOCKED
    assert "empty or missing specification" in out.note.lower()


# --- stale re-spawn guard (spec fingerprint) ----------------------------


def test_stale_respawn_guard_blocks_unchanged_spec(ctx_factory, tmp_path, monkeypatch):
    """When implement.md records a BLOCKED outcome and the spec hasn't
    changed, preflight must block BEFORE a trace opens."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Stale spec", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    # First implement run: agent produces NO changes → BLOCKED.
    # This writes implement.md with "BLOCKED — resumable" + spec-fingerprint.
    def _agent_noop(*, repo_dir, spec, **kwargs):
        return ("did nothing", [], "", None, None, True, "nothing to do")

    monkeypatch.setattr(coding, "run_implement_agent", _agent_noop)

    out1 = ImplementStage().run(t, ctx)
    assert out1.next_state is State.DONE  # no_change_needed → DONE

    # Reset ticket to READY to simulate a re-spawn.
    ctx.service.transition(t.id, State.BLOCKED, "test reset")
    ctx.service.transition(t.id, State.READY, "test reset")
    t = ctx.service.get(t.id)

    # Write implement.md simulating a prior BLOCKED outcome.
    ws = ctx.service.workspace(t)
    # The BLOCKED→READY transition above persists a spec-fingerprint
    # override (durable suppress).  Clear it here so this test
    # exercises the non-override (guard-fires) path.
    override = ws.artifacts_dir / "implement_spec_override"
    with contextlib.suppress(FileNotFoundError):
        override.unlink()
    import hashlib

    body = ws.read_description() or ""
    fp = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    (ws.artifacts_dir / "implement.md").write_text(
        "# Implement (BLOCKED — resumable)\n"
        "branch: test-branch\n"
        f"spec-fingerprint: {fp}\n"
        "\nno changes produced\n",
        encoding="utf-8",
    )

    # Preflight should block — spec unchanged, last outcome BLOCKED.
    out = ImplementStage().preflight(t, ctx)
    assert out is not None
    assert out.next_state is State.BLOCKED
    assert "spec unchanged" in out.note.lower()
    assert fp in out.note


def test_stale_respawn_guard_allows_changed_spec(ctx_factory, tmp_path, monkeypatch):
    """When the spec has changed since the last BLOCKED implement,
    preflight must allow the re-spawn."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Changed spec", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    # Write implement.md with fingerprint of the OLD spec.
    ws = ctx.service.workspace(t)
    import hashlib

    old_body = "Old spec content"
    old_fp = hashlib.sha256(old_body.encode("utf-8")).hexdigest()[:16]
    (ws.artifacts_dir / "implement.md").write_text(
        "# Implement (BLOCKED — resumable)\n"
        "branch: test-branch\n"
        f"spec-fingerprint: {old_fp}\n"
        "\nno changes produced\n",
        encoding="utf-8",
    )

    # Preflight should allow — current spec differs from stored fingerprint.
    out = ImplementStage().preflight(t, ctx)
    assert out is None, f"preflight must allow when spec changed, got: {out}"


def test_stale_respawn_guard_skips_when_passed(ctx_factory, tmp_path, monkeypatch):
    """When implement.md records a 'passed' outcome, preflight must NOT
    block regardless of fingerprint match."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Passed ticket", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    # Write implement.md with "passed" header — even with matching
    # fingerprint, preflight should not block.
    ws = ctx.service.workspace(t)
    import hashlib

    body = ws.read_description() or ""
    fp = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    (ws.artifacts_dir / "implement.md").write_text(
        "# Implement (passed)\n"
        "branch: test-branch\n"
        f"spec-fingerprint: {fp}\n"
        "\ncompleted successfully\n",
        encoding="utf-8",
    )

    out = ImplementStage().preflight(t, ctx)
    assert out is None, f"preflight must allow when last outcome passed, got: {out}"


def test_stale_respawn_guard_skips_without_implement_md(
    ctx_factory, tmp_path, monkeypatch
):
    """On first implement run (no implement.md), preflight must proceed
    normally — no false positive block."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Fresh ticket", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    # No implement.md exists — preflight should proceed.
    out = ImplementStage().preflight(t, ctx)
    assert out is None, f"preflight must allow on first run, got: {out}"


# --- transient fingerprint guard -------------------------------------------


def test_transient_header_skips_fingerprint_guard(ctx_factory, tmp_path, monkeypatch):
    """When implement.md records a TRANSIENT outcome, preflight must NOT
    block — the transient abort was environmental, not spec-determined."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Transient ticket", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    # Write implement.md with "TRANSIENT — retryable" header and NO
    # spec-fingerprint line — simulating an env-error short-circuit.
    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "implement.md").write_text(
        "# Implement (TRANSIENT — retryable)\n"
        "branch: test-branch\n"
        "\nenvironment failure not fixable by code edits\n",
        encoding="utf-8",
    )

    out = ImplementStage().preflight(t, ctx)
    assert out is None, (
        f"preflight must allow when last outcome was TRANSIENT, got: {out}"
    )


def test_spec_determined_blocked_persists_fingerprint_and_guards(
    ctx_factory, tmp_path, monkeypatch
):
    """A spec-determined BLOCKED writes the fingerprint, and an unchanged-
    spec re-spawn is correctly guarded."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Blocked spec-determined", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    # Run an implement pass that produces NO changes → _finalize writes
    # implement.md with "BLOCKED — resumable" + spec-fingerprint.
    def _agent_noop(*, repo_dir, spec, **kwargs):
        return ("did nothing", [], "", None, None, True, "nothing to do")

    monkeypatch.setattr(coding, "run_implement_agent", _agent_noop)

    out1 = ImplementStage().run(t, ctx)
    assert out1.next_state is State.DONE  # no_change_needed → DONE

    # Reset ticket to READY to simulate a re-spawn.
    ctx.service.transition(t.id, State.BLOCKED, "test reset")
    ctx.service.transition(t.id, State.READY, "test reset")
    t = ctx.service.get(t.id)

    # Write implement.md with "BLOCKED — resumable" + matching fingerprint.
    ws = ctx.service.workspace(t)
    # The BLOCKED→READY transition above persists a spec-fingerprint
    # override (durable suppress).  Clear it here so this test
    # exercises the non-override (guard-fires) path.
    override = ws.artifacts_dir / "implement_spec_override"
    with contextlib.suppress(FileNotFoundError):
        override.unlink()
    import hashlib

    body = ws.read_description() or ""
    fp = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    (ws.artifacts_dir / "implement.md").write_text(
        "# Implement (BLOCKED — resumable)\n"
        "branch: test-branch\n"
        f"spec-fingerprint: {fp}\n"
        "\nno changes produced\n",
        encoding="utf-8",
    )

    # Preflight should block — spec-determined, fingerprint matches.
    out = ImplementStage().preflight(t, ctx)
    assert out is not None
    assert out.next_state is State.BLOCKED
    assert "spec-determined" in out.note.lower()
    assert fp in out.note


# --- fingerprint collision + operator force-retry integration ------------


def test_resume_blocked_with_note_allows_implement_after_fingerprint_match(
    ctx_factory, tmp_path, monkeypatch
):
    """A blocked ticket with an unchanged spec, resumed with an operator
    justification note, must proceed to a fresh implement cycle, and
    the guard stays suppressed for that spec fingerprint across
    subsequent cycles — only re-blocking when the spec actually changes."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Force-retry ticket", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    # --- Phase 1: write a stale implement.md simulating a prior BLOCKED ---
    # outcome with the current spec's fingerprint.
    ws = ctx.service.workspace(t)
    import hashlib

    body = ws.read_description() or ""
    fp = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    (ws.artifacts_dir / "implement.md").write_text(
        "# Implement (BLOCKED — resumable)\n"
        "branch: test-branch\n"
        f"spec-fingerprint: {fp}\n"
        "\nno changes produced\n",
        encoding="utf-8",
    )

    # Preflight should block — fingerprint matches unchanged spec.
    out = ImplementStage().preflight(t, ctx)
    assert out is not None
    assert out.next_state is State.BLOCKED
    assert "spec unchanged" in out.note.lower()
    assert fp in out.note

    # Transition the ticket to BLOCKED (simulating what the worker would do).
    ctx.service.transition(t.id, State.BLOCKED, out.note)
    t = ctx.service.get(t.id)
    assert t.state is State.BLOCKED

    # --- Phase 2: operator force-retry via resume_blocked with note ---
    # This should clear the stale implement guard.
    resumed = ctx.service.resume_blocked(t.id, note="operator force retry — flake")
    assert resumed.state is State.READY
    assert not (ws.artifacts_dir / "implement.md").exists(), (
        "resume_blocked with note must clear the stale implement guard"
    )

    # --- Phase 3: preflight must now ALLOW the re-spawn ---
    # (implement.md was deleted, so the guard has nothing to match against).
    t = ctx.service.get(t.id)
    out3 = ImplementStage().preflight(t, ctx)
    assert out3 is None, f"preflight must allow after force-retry, got: {out3}"

    # --- Phase 4: after ONE implement cycle (which fails again), ---
    # the guard re-arms — the next automatic retry should be blocked.
    def _agent_noop(*, repo_dir, spec, **kwargs):
        return ("did nothing", [], "", None, None, True, "nothing to do")

    monkeypatch.setattr(coding, "run_implement_agent", _agent_noop)

    out4 = ImplementStage().run(t, ctx)
    assert out4.next_state is State.DONE  # no_change_needed → DONE

    # Write a fresh implement.md with matching fingerprint (simulating a
    # new BLOCKED outcome from a subsequent re-spawn).
    ctx.service.transition(t.id, State.BLOCKED, "test re-block")
    ctx.service.transition(t.id, State.READY, "test re-block")
    t = ctx.service.get(t.id)
    (ws.artifacts_dir / "implement.md").write_text(
        "# Implement (BLOCKED — resumable)\n"
        "branch: test-branch\n"
        f"spec-fingerprint: {fp}\n"
        "\nno changes produced\n",
        encoding="utf-8",
    )

    # Preflight should NOT block — the durable override persists across
    # cycles for the same spec fingerprint (operator already overrode).
    out5 = ImplementStage().preflight(t, ctx)
    assert out5 is None, (
        f"durable override must suppress re-block for same fingerprint, got: {out5}"
    )


def test_resume_blocked_without_note_does_not_clear_fingerprint_guard(
    ctx_factory, tmp_path, monkeypatch
):
    """resume_blocked without a note leaves the fingerprint guard intact —
    the next preflight must still block on an unchanged spec."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="No-note retry", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    # Write implement.md with matching fingerprint and block the ticket.
    ws = ctx.service.workspace(t)
    import hashlib

    body = ws.read_description() or ""
    fp = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    (ws.artifacts_dir / "implement.md").write_text(
        "# Implement (BLOCKED — resumable)\n"
        "branch: test-branch\n"
        f"spec-fingerprint: {fp}\n"
        "\nno changes produced\n",
        encoding="utf-8",
    )

    ctx.service.transition(t.id, State.BLOCKED, "fingerprint match")
    t = ctx.service.get(t.id)
    assert t.state is State.BLOCKED

    # Resume WITHOUT a note — the guard should NOT be cleared.
    resumed = ctx.service.resume_blocked(t.id)
    assert resumed.state is State.READY
    assert (ws.artifacts_dir / "implement.md").exists(), (
        "resume_blocked without note must NOT clear the implement guard"
    )

    # Preflight should still block.
    t = ctx.service.get(t.id)
    out = ImplementStage().preflight(t, ctx)
    assert out is not None
    assert out.next_state is State.BLOCKED


def test_transition_blocked_to_ready_with_note_clears_fingerprint_guard(
    ctx_factory, tmp_path, monkeypatch
):
    """An operator-forced BLOCKED→READY transition with a justification
    note also clears the fingerprint guard (equivalent to resume_blocked)."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Transition retry", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    ws = ctx.service.workspace(t)
    import hashlib

    body = ws.read_description() or ""
    fp = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    (ws.artifacts_dir / "implement.md").write_text(
        "# Implement (BLOCKED — resumable)\n"
        "branch: test-branch\n"
        f"spec-fingerprint: {fp}\n"
        "\nno changes produced\n",
        encoding="utf-8",
    )

    ctx.service.transition(t.id, State.BLOCKED, "fingerprint match")
    t = ctx.service.get(t.id)
    assert t.state is State.BLOCKED

    # Force-retry via direct transition with a note (not resume_blocked).
    ctx.service.transition(
        t.id, State.READY, note="operator force retry — environmental flake"
    )
    t = ctx.service.get(t.id)
    assert t.state is State.READY
    assert not (ws.artifacts_dir / "implement.md").exists(), (
        "BLOCKED→READY transition with note must clear the implement guard"
    )

    # Preflight must now allow the re-spawn.
    out = ImplementStage().preflight(t, ctx)
    assert out is None, f"preflight must allow after transition with note, got: {out}"


def test_answer_pending_question_clears_fingerprint_guard(
    ctx_factory, tmp_path, monkeypatch
):
    """When an AWAITING_USER_REPLY ticket with a stale implement fingerprint
    is answered via answer_pending_question, the fingerprint guard must be
    cleared — the operator's answer is the new input, and re-blocking on
    the unchanged spec alone wastes the spawn budget."""
    from robotsix_mill.core import db
    from robotsix_mill.core.models import Comment, Ticket

    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Answer-clears-guard", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    ws = ctx.service.workspace(t)
    import hashlib

    body = ws.read_description() or ""
    fp = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    (ws.artifacts_dir / "implement.md").write_text(
        "# Implement (BLOCKED — resumable)\n"
        "branch: test-branch\n"
        f"spec-fingerprint: {fp}\n"
        "\nno changes produced\n",
        encoding="utf-8",
    )

    # Put the ticket into AWAITING_USER_REPLY with an open ask_user thread.
    with db.session(ctx.settings, ctx.service.board_id) as s:
        row = s.get(Ticket, t.id)
        row.state = State.AWAITING_USER_REPLY
        row.paused_from = State.READY.value
        s.add(row)
        s.flush()
        # Add an open [ASK_USER] thread.
        c = Comment(
            ticket_id=t.id,
            body="[ASK_USER] operator clarification needed",
            author="system",
            parent_id=None,
        )
        s.add(c)
        s.commit()

    t = ctx.service.get(t.id)
    assert t.state is State.AWAITING_USER_REPLY

    # Operator answers the question.
    reply = ctx.service.answer_pending_question(
        t.id,
        "Here is the clarification.",
        author="operator",
    )
    assert reply is not None

    # Ticket must resume to READY (its paused_from).
    t = ctx.service.get(t.id)
    assert t.state is State.READY

    # The spec-fingerprint override must be persisted.
    override_path = ws.artifacts_dir / "implement_spec_override"
    assert override_path.exists(), (
        "answer_pending_question must persist the spec-fingerprint override"
    )
    assert override_path.read_text(encoding="utf-8").strip() == fp, (
        "override fingerprint must match the current spec fingerprint"
    )

    # Preflight must now ALLOW the re-spawn.
    out = ImplementStage().preflight(t, ctx)
    assert out is None, (
        f"preflight must allow after answer_pending_question, got: {out}"
    )


def test_automatic_fingerprint_refusal_message_includes_force_retry_remedy(
    ctx_factory, tmp_path, monkeypatch
):
    """When the fingerprint guard blocks an automatic retry, the diagnostic
    message must name resume-blocked as an operator remedy."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        forge_remote_url=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Message check", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    ws = ctx.service.workspace(t)
    import hashlib

    body = ws.read_description() or ""
    fp = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    (ws.artifacts_dir / "implement.md").write_text(
        "# Implement (BLOCKED — resumable)\n"
        "branch: test-branch\n"
        f"spec-fingerprint: {fp}\n"
        "\nno changes produced\n",
        encoding="utf-8",
    )

    out = ImplementStage().preflight(t, ctx)
    assert out is not None
    assert out.next_state is State.BLOCKED
    # The message must mention both the old remedies AND the new one.
    assert "resume-blocked" in out.note, (
        f"diagnostic must mention resume-blocked as a remedy, got: {out.note}"
    )
    assert "fingerprint" in out.note
    assert "spec unchanged" in out.note.lower()


# ---------------------------------------------------------------------------
# resume-with-ahead edit-claim guard: a branch that already carries committed
# work is not "lost work", even when one claimed edit is missing from its diff.
#
# Measured 2026-08-13: four of the five tickets blocked on this path had 2-4
# commits and 2-6 changed files sitting in their workspace, stranded
# (robotsix-chat a78d/a801/3f3e, robotsix-mill 8b2c). The fifth (959e) had an
# empty branch — the shape the guard is actually for.
# ---------------------------------------------------------------------------


def _edit_msgs(*paths: str) -> bytes:
    """A new_messages payload claiming an ``edit_file`` call per path."""
    import json as _json

    parts: list[dict] = []
    for p in paths:
        parts.append(
            {
                "part_kind": "tool-call",
                "tool_name": "edit_file",
                "args": {"path": p},
                "tool_call_id": f"call_{p}",
            }
        )
    return _json.dumps([{"parts": parts}]).encode()


def test_resume_with_ahead_missing_claimed_edit_does_not_block(
    ctx_factory, tmp_path, monkeypatch
):
    """Branch has real commits; one claimed edit is absent → proceed, not BLOCK.

    This is the stranded-work shape: prior passes committed the
    implementation, the resume pass re-touched files and also claimed an
    edit that is not in the branch diff (a changelog fragment renamed away,
    a file written then reverted). Blocking discards committed work and
    needs a human; the branch is intact, so it must flow on to review.
    """
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote, test_command="true", review_enabled="true"
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    def _run_first(*, repo_dir, **_kwargs):
        (Path(repo_dir) / "feature.txt").write_text("implemented")
        return ("done", ["feature.txt"], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run_first)
    assert ImplementStage().run(t, ctx).next_state is State.CODE_REVIEW

    t = ctx.service.get(t.id)
    ctx.service.set_review_rounds(t.id, 1)

    # Resume: no new working-tree changes, but the agent claims an edit to a
    # file that never made it onto the branch.
    def _run_resume(*, repo_dir, **_kwargs):
        del repo_dir
        return (
            "Re-applied the change; also wrote a changelog fragment.",
            [],
            "",
            None,
            _edit_msgs("never_landed.md"),
            False,
            "",
        )

    monkeypatch.setattr(coding, "run_implement_agent", _run_resume)
    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.CODE_REVIEW
    assert "edit-claim contradiction" not in (out.note or "")


def test_resume_empty_branch_missing_claimed_edit_still_blocks(
    ctx_factory, tmp_path, monkeypatch
):
    """No commits on the branch + a claimed edit that vanished → still BLOCK.

    Keeps the guard's teeth where the evidence says it belongs: nothing was
    committed, so an edit call that left no trace really is lost work.
    """
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        forge_remote_url=remote, test_command="true", review_enabled="true"
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    # First pass commits nothing — the branch stays level with origin/main.
    def _run_noop(*, repo_dir, **_kwargs):
        del repo_dir
        return ("nothing yet", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run_noop)
    ImplementStage().run(t, ctx)

    t = ctx.service.get(t.id)
    ctx.service.set_review_rounds(t.id, 1)

    def _run_resume(*, repo_dir, **_kwargs):
        del repo_dir
        return (
            "Edited the file.",
            [],
            "",
            None,
            _edit_msgs("vanished.py"),
            False,
            "",
        )

    monkeypatch.setattr(coding, "run_implement_agent", _run_resume)
    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
