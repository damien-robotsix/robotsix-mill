import json
import subprocess
from pathlib import Path

import pytest

from robotsix_mill.agents import coding
from robotsix_mill.agents.fs_tools import build_fs_tools
from robotsix_mill.core import db
from robotsix_mill.core.models import TicketKind
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.implement import ImplementStage
from robotsix_mill.vcs import git_ops


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


@pytest.fixture
def ctx_factory(tmp_path, fake_sandbox):
    from robotsix_mill.config import Settings

    created = []

    def make(**env):
        db.reset_engine()
        # fake_sandbox replaces the (always-containerized) seam; no
        # Docker, no host execution.
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
                board_id="test-board",
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


def _write_file_map(ctx, ticket, *files):
    """Write a minimal file_map.json for *ticket* listing *files*."""
    import json as _json

    ws = ctx.service.workspace(ticket)
    (ws.artifacts_dir / "file_map.json").write_text(
        _json.dumps([{"file": f, "note": "test"} for f in files]),
        encoding="utf-8",
    )


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
    read_file, write_file, edit_file, delete_file, list_dir, run_command = (
        build_fs_tools(tmp_path, s)
    )
    assert "wrote" in write_file("a/b.txt", "hi")
    assert read_file(path="a/b.txt") == "hi"
    assert "a/" in list_dir(".")
    assert "exit=0" in run_command("echo ok")
    # errors come back as strings (so the model can self-correct), and
    # the path-escape guard still refuses the op
    esc = read_file(path="../escape.txt")
    assert esc.startswith("error:") and "escapes" in esc
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
    _, _, edit_file, _, _, _ = build_fs_tools(tmp_path, s)
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
    _, _, edit_file, _, _, _ = build_fs_tools(tmp_path, s)
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
    _, _, edit_file, _, _, _ = build_fs_tools(tmp_path, s)
    original = "dup\nmiddle\ndup\n"
    (tmp_path / "f.txt").write_text(original)
    result = edit_file("f.txt", "dup", "X")
    assert "appears 2 times" in result
    assert (tmp_path / "f.txt").read_text() == original


def test_edit_file_path_escape_rejected(tmp_path, fake_sandbox):
    from robotsix_mill.config import Settings

    s = Settings(data_dir=str(tmp_path))
    _, _, edit_file, _, _, _ = build_fs_tools(tmp_path, s)
    result = edit_file("../outside.txt", "x", "y")
    assert "escapes" in result


def test_delete_file_removes_existing_file(tmp_path, fake_sandbox):
    """delete_file returns success and the file no longer exists."""
    from robotsix_mill.config import Settings

    s = Settings(data_dir=str(tmp_path))
    _, _, _, delete_file, _, _ = build_fs_tools(tmp_path, s)
    (tmp_path / "foo.txt").write_text("hello")
    result = delete_file("foo.txt")
    assert "deleted" in result
    assert not (tmp_path / "foo.txt").exists()


def test_delete_file_missing_returns_error(tmp_path, fake_sandbox):
    """delete_file on a missing file returns an error string, not a crash."""
    from robotsix_mill.config import Settings

    s = Settings(data_dir=str(tmp_path))
    _, _, _, delete_file, _, _ = build_fs_tools(tmp_path, s)
    result = delete_file("nope.txt")
    assert result.startswith("error:")


def test_delete_file_on_directory_returns_error(tmp_path, fake_sandbox):
    """delete_file on a directory returns an error string, no deletion."""
    from robotsix_mill.config import Settings

    s = Settings(data_dir=str(tmp_path))
    _, _, _, delete_file, _, _ = build_fs_tools(tmp_path, s)
    d = tmp_path / "subdir"
    d.mkdir()
    result = delete_file("subdir")
    assert result.startswith("error:")
    assert d.exists()  # directory untouched


def test_delete_file_path_escape_rejected(tmp_path, fake_sandbox):
    """Path traversal is rejected by _safe."""
    from robotsix_mill.config import Settings

    s = Settings(data_dir=str(tmp_path))
    _, _, _, delete_file, _, _ = build_fs_tools(tmp_path, s)
    result = delete_file("../outside.txt")
    assert "escapes" in result


def test_fs_tools_non_existent_root_returns_clear_error(tmp_path, fake_sandbox):
    """Every tool returns a stable error string (not a raw exception)
    when the workspace repo directory hasn't been cloned yet."""
    from robotsix_mill.config import Settings

    fake_root = tmp_path / "does-not-exist"
    s = Settings(data_dir=str(tmp_path))
    read_file, write_file, edit_file, delete_file, list_dir, run_command = (
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
    assert "FORGE_REMOTE_URL" in out.note


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
        FORGE_REMOTE_URL=remote, test_command="true", review_enabled="false"
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
        FORGE_REMOTE_URL=remote, test_command="true", review_enabled="false"
    )
    monkeypatch.setattr(coding, "run_implement_agent", _fake_agent(None))
    t = _ticket(ctx)
    _write_file_map(ctx, t, "dummy.txt")
    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.DONE
    assert "already satisfied" in out.note.lower()


def test_failing_gate_blocks_resumable(ctx_factory, tmp_path, monkeypatch):
    """The stage owns a bounded fix loop: it re-invokes the coordinator
    on each test-gate failure, feeding the diagnosis back, and escalates
    to BLOCKED-resumable once max_fix_iterations is exhausted — WIP
    committed."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
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
    assert "still failing" in out.note and "resumable" in out.note
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
        FORGE_REMOTE_URL=remote,
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
    assert "still failing" in out.note and "resumable" in out.note


def test_smoke_gate_skipped_when_paths_do_not_match(ctx_factory, tmp_path, monkeypatch):
    """A pure-backend ticket whose introduced files match no smoke_paths
    glob does NOT invoke the smoke gate — the ticket proceeds normally."""
    from robotsix_mill.stages import implement as impl_mod

    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
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
        FORGE_REMOTE_URL=remote,
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
    from robotsix_mill.stages import implement as impl_mod
    from robotsix_mill.agents.testing import ENV_ERROR_PREFIX

    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
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
        FORGE_REMOTE_URL=remote,
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
        FORGE_REMOTE_URL=remote, test_command="true", review_enabled="false"
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
    assert "resumable" in out.note and "budget" in out.note
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
        FORGE_REMOTE_URL=remote, test_command="true", review_enabled="false"
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
    assert any("WIP" in m for m in msgs) and len(msgs) >= 2


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
        FORGE_REMOTE_URL=remote,
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
        FORGE_REMOTE_URL=remote,
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
        FORGE_REMOTE_URL=remote,
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
        FORGE_REMOTE_URL=remote,
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
        FORGE_REMOTE_URL=remote, test_command="true", review_enabled="false"
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
        FORGE_REMOTE_URL=remote, test_command="true", review_enabled="false"
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
        FORGE_REMOTE_URL=remote, test_command="true", review_enabled="false"
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
        FORGE_REMOTE_URL=remote, test_command="true", review_enabled="false"
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
        FORGE_REMOTE_URL=remote,
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
        FORGE_REMOTE_URL=remote,
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
        FORGE_REMOTE_URL=remote, test_command="true", review_enabled="false"
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
        FORGE_REMOTE_URL=remote, test_command="true", review_enabled="false"
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
        FORGE_REMOTE_URL=remote, test_command="true", review_enabled="false"
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


# --- scope guardrail ----------------------------------------------------


def test_scope_violation_blocks_ticket(ctx_factory, tmp_path, monkeypatch):
    """When the agent modifies a tracked file not in file_map, the scope
    check catches it and immediately blocks the ticket — no retry.
    The test gate is never reached on the violating iteration."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
        max_fix_iterations="3",
        scope_triage_enabled="false",
    )
    t = _ticket(ctx)

    # file_map only allows wip.txt — README.md is out of scope.
    ws = ctx.service.workspace(t)
    file_map_path = ws.artifacts_dir / "file_map.json"
    file_map_path.write_text(
        '[{"file": "wip.txt", "note": "only this file"}]',
        encoding="utf-8",
    )

    call_count = {"n": 0}

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
        call_count["n"] += 1
        # Write wip.txt (in-scope) AND modify README.md (out-of-scope)
        (Path(repo_dir) / "wip.txt").write_text("in scope")
        (Path(repo_dir) / "README.md").write_text("out of scope edit")
        return ("edit done", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "scope violation" in out.note
    assert call_count["n"] == 1, "agent must not be retried"
    assert (ctx.service.workspace(t).artifacts_dir / "implement.md").exists()


def test_scope_check_passes_when_all_in_scope(
    ctx_factory, tmp_path, monkeypatch, caplog
):
    """When every changed file is in file_map, the scope check passes,
    logs an info message, and the loop proceeds to the test gate."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx)

    # file_map includes wip.txt → scope check should pass.
    ws = ctx.service.workspace(t)
    file_map_path = ws.artifacts_dir / "file_map.json"
    file_map_path.write_text(
        '[{"file": "wip.txt", "note": "the change"}]',
        encoding="utf-8",
    )

    monkeypatch.setattr(
        coding,
        "run_implement_agent",
        _fake_agent({"wip.txt": "done"}),
    )
    import logging

    with caplog.at_level(logging.INFO, logger="robotsix_mill.stages.implement"):
        out = ImplementStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING
    assert any(
        "scope check passed" in m and "1 file(s) changed" in m for m in caplog.messages
    ), f"expected scope-passed info log, got: {caplog.messages}"


def test_scope_check_directory_entry_prefix_matches(
    ctx_factory, tmp_path, monkeypatch, caplog
):
    """A file_map entry ending in "/" covers every file under that
    directory — regression for auto-mail 6624, where a declared
    ".deps/" removal flooded the scope check with 118 "out-of-scope"
    files that were the ticket's own deliverable."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx)

    ws = ctx.service.workspace(t)
    file_map_path = ws.artifacts_dir / "file_map.json"
    file_map_path.write_text(
        '[{"file": "vendored/", "note": "delete the vendored tree"}]',
        encoding="utf-8",
    )

    inner = _fake_agent({"vendored/pkg/data.txt": "x", "vendored/other.txt": "y"})

    def _agent_with_dirs(*, repo_dir, **kwargs):
        (Path(repo_dir) / "vendored" / "pkg").mkdir(parents=True, exist_ok=True)
        return inner(repo_dir=repo_dir, **kwargs)

    monkeypatch.setattr(coding, "run_implement_agent", _agent_with_dirs)
    import logging

    with caplog.at_level(logging.INFO, logger="robotsix_mill.stages.implement"):
        out = ImplementStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING
    assert any("scope check passed" in m for m in caplog.messages), (
        f"expected scope-passed info log, got: {caplog.messages}"
    )


def test_scope_check_skipped_when_no_file_map(
    ctx_factory, tmp_path, monkeypatch, caplog
):
    """When file_map.json is absent, the stage logs a warning and
    proceeds — scope enforcement is skipped, not blocked."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx)
    # No file_map.json written → stage should warn and proceed.

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

    import logging

    with caplog.at_level(logging.WARNING, logger="robotsix_mill.stages.implement"):
        out = ImplementStage().run(t, ctx)

    assert out.next_state is State.DOCUMENTING, (
        f"expected DOCUMENTING, got {out.next_state}"
    )
    assert len(agent_called) == 1, "agent must be called when file_map is missing"
    assert any("skipping scope enforcement" in m for m in caplog.messages), (
        f"expected scope-skip warning, got: {caplog.messages}"
    )


def test_scope_check_skipped_when_file_map_empty(
    ctx_factory, tmp_path, monkeypatch, caplog
):
    """When file_map.json exists but is an empty array, the stage logs
    a warning and proceeds — same as a missing file_map."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx)

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "file_map.json").write_text("[]", encoding="utf-8")

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

    import logging

    with caplog.at_level(logging.WARNING, logger="robotsix_mill.stages.implement"):
        out = ImplementStage().run(t, ctx)

    assert out.next_state is State.DOCUMENTING, (
        f"expected DOCUMENTING, got {out.next_state}"
    )
    assert len(agent_called) == 1, "agent must be called when file_map is empty"
    assert any("skipping scope enforcement" in m for m in caplog.messages), (
        f"expected scope-skip warning, got: {caplog.messages}"
    )


# --- scope-triage integration tests -------------------------------------


def test_scope_triage_expand_continues_loop(ctx_factory, tmp_path, monkeypatch):
    """EXPAND verdict: file_map is updated in-memory, the loop continues,
    and a comment is posted.  When at least one expand-file has *not*
    been modified yet, the agent is re-run (no retroactive short-circuit)."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
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

    call_count = {"n": 0}

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
        call_count["n"] += 1
        (Path(repo_dir) / "wip.txt").write_text("in scope")
        (Path(repo_dir) / "README.md").write_text("out of scope edit")
        return ("edit done", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    import robotsix_mill.agents.scope_triage as scope_triage_mod
    from robotsix_mill.agents.scope_triage import ScopeTriageVerdict

    def _fake_triage(
        *, settings, ticket_spec, file_map, out_of_scope_files, diff_summaries
    ):
        return ScopeTriageVerdict(
            action="EXPAND",
            justification="Minor dependency edit is a legitimate consequence",
            # CHANGELOG.md is *not* in the diff, so there is genuinely
            # new work to do → loop MUST continue.
            expand_files=["README.md", "CHANGELOG.md"],
        )

    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _fake_triage)

    out = ImplementStage().run(t, ctx)

    # With at least one expand-file not yet modified, the loop must continue
    # (agent called at least twice).
    assert call_count["n"] >= 2, "EXPAND with unmodified file should continue the loop"
    assert out.next_state is not State.BLOCKED
    # The EXPAND decision lands in history, not comments (v1).
    history = ctx.service.history(t.id)
    assert any((ev.note or "").startswith("scope-triage EXPAND") for ev in history)
    comments = ctx.service.list_comments(t.id)
    assert not any("[scope-triage]" in (c.body or "") for c in comments), (
        "scope-triage no longer emits comments — it uses add_step_event"
    )


def test_scope_triage_expand_retroactive_short_circuit(
    ctx_factory, tmp_path, monkeypatch
):
    """When scope-triage EXPANDs files that are *all* already in the
    current diff, the retroactive short-circuit fires: the agent is NOT
    re-run, the loop falls through to the test gate, and (with tests
    passing) the ticket finalizes without wasting an iteration."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx)

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "file_map.json").write_text(
        '[{"file": "wip.txt", "note": "only this file"}]',
        encoding="utf-8",
    )

    call_count = {"n": 0}

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
        call_count["n"] += 1
        (Path(repo_dir) / "wip.txt").write_text("in scope")
        (Path(repo_dir) / "README.md").write_text("out of scope edit")
        return ("agent summary text", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    import robotsix_mill.agents.scope_triage as scope_triage_mod
    from robotsix_mill.agents.scope_triage import ScopeTriageVerdict

    def _fake_triage(
        *, settings, ticket_spec, file_map, out_of_scope_files, diff_summaries
    ):
        return ScopeTriageVerdict(
            action="EXPAND",
            justification="README.md is a natural side-effect edit",
            expand_files=["README.md"],
        )

    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _fake_triage)

    out = ImplementStage().run(t, ctx)

    # Agent must be called exactly once (no wasted re-run).
    assert call_count["n"] == 1, "retroactive short-circuit should skip agent re-run"
    assert out.next_state is State.DOCUMENTING, (
        f"expected DOCUMENTING, got {out.next_state}"
    )
    # The EXPAND decision lands in history (v1 — no more comments).
    # The agent summary also lands in history (as a same-state
    # `implement:` step event, post the implement-step-event change).
    # The transition note is now a short stage-name marker, not the
    # summary.
    history = ctx.service.history(t.id)
    assert any((ev.note or "").startswith("scope-triage EXPAND") for ev in history)
    assert any(
        (ev.note or "").startswith("implement:")
        and "agent summary text" in (ev.note or "")
        for ev in history
    )


def test_scope_triage_reject_to_ready(ctx_factory, tmp_path, monkeypatch):
    """REJECT verdict: ticket goes back to READY with a comment naming
    the rogue files."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
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
        (Path(repo_dir) / "wip.txt").write_text("in scope")
        (Path(repo_dir) / "README.md").write_text("out of scope edit")
        return ("edit done", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    import robotsix_mill.agents.scope_triage as scope_triage_mod
    from robotsix_mill.agents.scope_triage import ScopeTriageVerdict

    def _fake_triage(
        *, settings, ticket_spec, file_map, out_of_scope_files, diff_summaries
    ):
        return ScopeTriageVerdict(
            action="REJECT",
            justification="Unrelated module — scope creep",
            expand_files=[],
        )

    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _fake_triage)

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.READY
    assert "REJECT" in out.note
    # The REJECT details live in the transition note (history) — not
    # in comments. Files are quoted in backticks so the dedup loop
    # can scan history events.
    assert "README.md" in (out.note or "")
    comments = ctx.service.list_comments(t.id)
    assert not any("scope-triage" in (c.body or "") for c in comments)


def test_scope_triage_escalate_to_blocked(ctx_factory, tmp_path, monkeypatch):
    """ESCALATE verdict: ticket goes to BLOCKED with triage reasoning."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
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
        (Path(repo_dir) / "wip.txt").write_text("in scope")
        (Path(repo_dir) / "README.md").write_text("out of scope edit")
        return ("edit done", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    import robotsix_mill.agents.scope_triage as scope_triage_mod
    from robotsix_mill.agents.scope_triage import ScopeTriageVerdict

    def _fake_triage(
        *, settings, ticket_spec, file_map, out_of_scope_files, diff_summaries
    ):
        return ScopeTriageVerdict(
            action="ESCALATE",
            justification="Ambiguous spec — cannot classify",
            expand_files=[],
        )

    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _fake_triage)

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "ESCALATE" in out.note
    # ESCALATE reasoning + the out-of-scope file list now live in the
    # transition note rather than a comment.
    assert "README.md" in (out.note or "")
    comments = ctx.service.list_comments(t.id)
    assert not any("scope-triage" in (c.body or "") for c in comments)


def test_scope_triage_disabled_falls_through(ctx_factory, tmp_path, monkeypatch):
    """When scope_triage_enabled=False, existing BLOCKED behaviour is
    preserved exactly — no triage agent is called."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
        max_fix_iterations="3",
        scope_triage_enabled="false",
    )
    t = _ticket(ctx)

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "file_map.json").write_text(
        '[{"file": "wip.txt", "note": "only this file"}]',
        encoding="utf-8",
    )

    call_count = {"n": 0}

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
        call_count["n"] += 1
        (Path(repo_dir) / "wip.txt").write_text("in scope")
        (Path(repo_dir) / "README.md").write_text("out of scope edit")
        return ("edit done", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "scope violation" in out.note
    assert call_count["n"] == 1, "agent must not be retried"


def test_scope_triage_agent_error_escalates(ctx_factory, tmp_path, monkeypatch):
    """When the triage agent raises an exception, the ticket escalates
    to BLOCKED with an agent-error note."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
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
        (Path(repo_dir) / "wip.txt").write_text("in scope")
        (Path(repo_dir) / "README.md").write_text("out of scope edit")
        return ("edit done", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)

    import robotsix_mill.agents.scope_triage as scope_triage_mod

    def _failing_triage(
        *, settings, ticket_spec, file_map, out_of_scope_files, diff_summaries
    ):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _failing_triage)

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "agent error" in out.note
    # The note names the ACTUAL exception — a bare "agent error" reads
    # like a scope verdict and sends the operator log-hunting.
    assert "RuntimeError: model unavailable" in out.note
    # The "agent error" diagnostic lives in the transition note now
    # (v1 — scope-triage no longer comments).
    comments = ctx.service.list_comments(t.id)
    assert not any("scope-triage" in (c.body or "") for c in comments)


# --- post-edit reference_files persistence ------------------------------


def test_post_edit_reference_files_persisted(ctx_factory, tmp_path, monkeypatch):
    """After a successful agent pass, reference_files.json (paths-only,
    sourced from agent-curated list) and implement_summary.md are written
    to artifacts_dir."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
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
        FORGE_REMOTE_URL=remote,
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
        FORGE_REMOTE_URL=remote,
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
        FORGE_REMOTE_URL=remote,
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
    ctx = ctx_factory(FORGE_REMOTE_URL=remote, test_command="true")

    def _run(*, repo_dir, **_kwargs):
        # Touch nothing; the codebase is already correct.
        del repo_dir
        return (
            "Inspected — the `hasattr` guard was already removed by "
            "20260528T070000Z-cleanup-hasattr-guards-1234.",
            [],
            "",
            None,
            None,
            True,
            "The `hasattr` guard at routes.py:127 referenced in the "
            "spec was already removed by ticket 1234 on 2026-05-28. "
            "Current repo state matches the spec's desired end state.",
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
    ctx = ctx_factory(FORGE_REMOTE_URL=remote, test_command="true")

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
    ctx = ctx_factory(FORGE_REMOTE_URL=remote, test_command="true")

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
            "(False positive: ignoring this rationale because the "
            "branch has commits ahead of origin/main that haven't "
            "been delivered yet.)",
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
    ctx = ctx_factory(FORGE_REMOTE_URL=remote, test_command="true")

    def _run(*, repo_dir, **_kwargs):
        del repo_dir
        return (
            "Confirmed the dead guard was already removed by ticket 5678.",
            [],
            "",
            None,
            None,
            True,
            "The hasattr guard the spec asks us to remove was deleted "
            "by ticket 5678. Verified by reading pass_runner.py — the "
            "symbol is no longer present.",
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


# --- unit tests for _run_scope_guardrail --------------------------------


def test_run_scope_guardrail_triage_disabled_blocks(ctx_factory, tmp_path, monkeypatch):
    """scope_triage_enabled=False: any out-of-scope file → BLOCKED outcome."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        scope_triage_enabled="false",
    )
    t = _ticket(ctx)
    # file_map only allows "a.txt"
    _write_file_map(ctx, t, "a.txt")

    # Write out-of-scope file to the repo so git_ops.changed_files
    # sees it as a change from origin/main.
    repo = ctx.service.workspace(t).dir / "repo"
    _clone_repo_to(ctx, remote, repo)
    (repo / "b.txt").write_text("out of scope")
    # Commit so that changed_files detects it against origin/main
    # (changed_files uses diff between HEAD and origin/<target>).
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "wip")
    # Write file_map.json so the guardrail has a scope to enforce.
    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "file_map.json").write_text(
        '[{"file": "a.txt", "note": "only a.txt"}]',
        encoding="utf-8",
    )
    settings = ctx.settings

    result = ImplementStage._run_scope_guardrail(
        ctx,
        t,
        repo,
        f"mill/{t.id}",
        summary="agent summary",
        ref_files=None,
        file_map={"a.txt"},
        settings=settings,
        spec="add a.txt",
        current_feedback=None,
    )

    assert result.action == "return"
    assert result.outcome is not None
    assert result.outcome.next_state is State.BLOCKED
    assert "scope violation" in result.outcome.note
    assert "b.txt" in result.outcome.note


def test_run_scope_guardrail_dedup_guard_suppresses_duplicate_reject(
    ctx_factory,
    tmp_path,
    monkeypatch,
):
    """When all out-of-scope files were already REJECTed in prior history
    events, the dedup guard fires → skip_iteration WITHOUT shipping: the
    re-created files are cleaned from the tree and NOT added to file_map.
    v1: the source of truth for the REJECT seed is a step event, not
    a comment (scope-triage no longer comments)."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "a.txt")

    # Seed a prior scope-triage REJECT history event naming b.txt.
    ctx.service.add_step_event(
        t.id,
        "scope-triage REJECT: prior run — out-of-scope: `b.txt`",
    )

    repo = ctx.service.workspace(t).dir / "repo"
    _clone_repo_to(ctx, remote, repo)
    (repo / "b.txt").write_text("out of scope again")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "wip")
    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "file_map.json").write_text(
        '[{"file": "a.txt", "note": "only a.txt"}]',
        encoding="utf-8",
    )
    settings = ctx.settings

    # Mock the scope-triage agent to return REJECT (the dedup guard
    # should intercept before this matters, but the agent is called).
    import robotsix_mill.agents.scope_triage as scope_triage_mod
    from robotsix_mill.agents.scope_triage import ScopeTriageVerdict

    def _fake_triage(
        *, settings, ticket_spec, file_map, out_of_scope_files, diff_summaries
    ):
        return ScopeTriageVerdict(
            action="REJECT",
            justification="Still out of scope",
            expand_files=[],
        )

    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _fake_triage)

    result = ImplementStage._run_scope_guardrail(
        ctx,
        t,
        repo,
        f"mill/{t.id}",
        summary="agent summary",
        ref_files=None,
        file_map={"a.txt"},
        settings=settings,
        spec="add a.txt",
        current_feedback=None,
    )

    assert result.action == "skip_iteration"
    # The dedup guard must NOT ship the re-created file: b.txt stays out
    # of file_map and is cleaned back out of the working tree.
    assert result.file_map is not None
    assert "b.txt" not in result.file_map
    assert result.feedback is None
    assert "b.txt" not in git_ops.changed_files(repo, "main")
    assert not (repo / "b.txt").exists()


def _reject_triage(
    *, settings, ticket_spec, file_map, out_of_scope_files, diff_summaries
):
    from robotsix_mill.agents.scope_triage import ScopeTriageVerdict

    return ScopeTriageVerdict(
        action="REJECT",
        justification="Unrelated scope creep",
        expand_files=[],
    )


def test_run_scope_guardrail_reject_cleans_tracked_and_untracked(
    ctx_factory, tmp_path, monkeypatch
):
    """A first-time REJECT removes the out-of-scope changes from the tree
    before finalize commits: a tracked modification (restored to origin),
    a newly-added tracked file, and an untracked file are all absent from
    the diff vs origin afterwards, while the in-scope change survives."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(FORGE_REMOTE_URL=remote, test_command="true")
    t = _ticket(ctx)
    _write_file_map(ctx, t, "a.txt")

    repo = ctx.service.workspace(t).dir / "repo"
    _clone_repo_to(ctx, remote, repo)
    # In-scope change + out-of-scope (tracked-mod README.md, new vendored.py),
    # both WIP-committed; plus an untracked stray.txt.
    (repo / "a.txt").write_text("in scope")
    (repo / "README.md").write_text("out of scope edit")
    (repo / "vendored.py").write_text("vendored tree")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "wip")
    (repo / "stray.txt").write_text("untracked stray")

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "file_map.json").write_text(
        '[{"file": "a.txt", "note": "only a.txt"}]', encoding="utf-8"
    )

    import robotsix_mill.agents.scope_triage as scope_triage_mod

    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _reject_triage)

    result = ImplementStage._run_scope_guardrail(
        ctx,
        t,
        repo,
        f"mill/{t.id}",
        summary="agent summary",
        ref_files=None,
        file_map={"a.txt"},
        settings=ctx.settings,
        spec="add a.txt",
        current_feedback=None,
    )

    assert result.action == "return"
    assert result.outcome.next_state is State.READY
    changed = git_ops.changed_files(repo, "main")
    # Out-of-scope paths gone from the diff (unstaged + WIP-committed).
    assert "README.md" not in changed
    assert "vendored.py" not in changed
    assert "stray.txt" not in changed
    assert not (repo / "vendored.py").exists()
    assert not (repo / "stray.txt").exists()
    # In-scope work preserved.
    assert "a.txt" in changed


def test_run_scope_guardrail_reject_cleans_resumed_wip_history(
    ctx_factory, tmp_path, monkeypatch
):
    """Resumed-branch case: the rejected file is already in the branch's
    committed history (no unstaged edit). REJECT must scrub it from the
    committed diff vs origin/main, not just the working tree."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(FORGE_REMOTE_URL=remote, test_command="true")
    t = _ticket(ctx)
    _write_file_map(ctx, t, "a.txt")

    repo = ctx.service.workspace(t).dir / "repo"
    _clone_repo_to(ctx, remote, repo)
    branch = f"mill/{t.id}"
    git_ops.create_branch(repo, branch)
    # Simulate a prior polluted WIP commit (in-scope + vendored tree).
    (repo / "a.txt").write_text("in scope")
    (repo / "vendored.py").write_text("vendored tree")
    git_ops.commit_all(repo, "prior wip [WIP]")
    # Re-checkout to mimic a fresh resume off the committed branch.
    git_ops.checkout(repo, branch)
    assert "vendored.py" in git_ops.changed_files(repo, "main")

    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "file_map.json").write_text(
        '[{"file": "a.txt", "note": "only a.txt"}]', encoding="utf-8"
    )

    import robotsix_mill.agents.scope_triage as scope_triage_mod

    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _reject_triage)

    result = ImplementStage._run_scope_guardrail(
        ctx,
        t,
        repo,
        branch,
        summary="agent summary",
        ref_files=None,
        file_map={"a.txt"},
        settings=ctx.settings,
        spec="add a.txt",
        current_feedback=None,
    )

    assert result.action == "return"
    # finalize committed the cleaned tree → no net committed diff for the
    # rejected file vs origin/main.
    net = subprocess.run(
        ["git", "-C", str(repo), "diff", "origin/main...HEAD", "--name-only"],
        capture_output=True,
        text=True,
    ).stdout
    assert "vendored.py" not in net
    assert "a.txt" in net


# --- binary artifact auto-cleanup in scope guardrail ----------------------


def test_binary_artifact_auto_cleanup_skips_triage(ctx_factory, tmp_path, monkeypatch):
    """When all out-of-scope files are binary artifacts, the scope-triage
    LLM is NOT invoked, the binary files are auto-cleaned, and the result
    is skip_iteration (ticket continues to test gate)."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "a.txt")

    repo = ctx.service.workspace(t).dir / "repo"
    _clone_repo_to(ctx, remote, repo)

    # Create a binary artifact file (test.db) that is out-of-scope.
    db_path = repo / "test.db"
    db_path.write_bytes(b"\x00\x01\x02\x03SQLite format 3\0")
    _git(repo, "add", "test.db")
    _git(repo, "commit", "-q", "-m", "wip with binary")

    settings = ctx.settings

    # Mock scope-triage to verify it is NOT called.
    import robotsix_mill.agents.scope_triage as scope_triage_mod

    triage_called = []

    def _fake_triage(
        *, settings, ticket_spec, file_map, out_of_scope_files, diff_summaries
    ):
        triage_called.append(1)
        raise AssertionError("scope-triage should not be called for binary artifacts")

    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _fake_triage)

    result = ImplementStage._run_scope_guardrail(
        ctx,
        t,
        repo,
        f"mill/{t.id}",
        summary="agent summary",
        ref_files=None,
        file_map={"a.txt"},
        settings=settings,
        spec="add a.txt",
        current_feedback=None,
    )

    # a) scope-triage LLM NOT invoked
    assert len(triage_called) == 0, (
        "scope-triage agent should not be called for binary-only out-of-scope"
    )

    # b) binary file no longer exists on disk
    assert not db_path.exists(), "binary artifact should be removed from disk"

    # c) result is skip_iteration
    assert result.action == "skip_iteration"
    assert result.outcome is None

    # d) step event contains auto-REJECT with filename
    history = ctx.service.history(t.id)
    events = [ev.note for ev in history if ev.note]
    assert any(
        "scope-triage auto-REJECT (binary artifacts)" in note and "`test.db`" in note
        for note in events
    ), f"auto-REJECT step event missing; history events: {events}"


def test_binary_artifact_cleanup_with_text_files_still_calls_triage(
    ctx_factory, tmp_path, monkeypatch
):
    """When out-of-scope files include both a binary artifact AND a text
    file, the binary is auto-cleaned AND the text file is still passed to
    the scope-triage LLM (called exactly once, with only the text file)."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "a.txt")

    repo = ctx.service.workspace(t).dir / "repo"
    _clone_repo_to(ctx, remote, repo)

    # Create a binary artifact AND a text file, both out-of-scope.
    db_path = repo / "test.db"
    db_path.write_bytes(b"\x00\x01\x02\x03SQLite format 3\0")
    (repo / "README.md").write_text("out of scope text edit")
    _git(repo, "add", "test.db", "README.md")
    _git(repo, "commit", "-q", "-m", "wip with binary and text")

    settings = ctx.settings

    # Mock scope-triage to capture what files it receives.
    import robotsix_mill.agents.scope_triage as scope_triage_mod
    from robotsix_mill.agents.scope_triage import ScopeTriageVerdict

    triage_calls = []

    def _fake_triage(
        *, settings, ticket_spec, file_map, out_of_scope_files, diff_summaries
    ):
        triage_calls.append((out_of_scope_files, diff_summaries))
        return ScopeTriageVerdict(
            action="EXPAND",
            justification="README.md is a natural side-effect edit",
            expand_files=["README.md"],
        )

    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _fake_triage)

    result = ImplementStage._run_scope_guardrail(
        ctx,
        t,
        repo,
        f"mill/{t.id}",
        summary="agent summary",
        ref_files=None,
        file_map={"a.txt"},
        settings=settings,
        spec="add a.txt",
        current_feedback=None,
    )

    # Binary file is removed from disk.
    assert not db_path.exists(), "binary artifact should be removed from disk"

    # Triage agent is called exactly once.
    assert len(triage_calls) == 1, (
        "scope-triage should be called exactly once for mixed out-of-scope"
    )

    out_of_scope_files, diff_summaries = triage_calls[0]

    # Only the text file is passed to triage.
    assert out_of_scope_files == ["README.md"], (
        f"expected only README.md, got {out_of_scope_files}"
    )
    assert "README.md" in diff_summaries
    assert "test.db" not in diff_summaries

    # Auto-REJECT step event was emitted for the binary.
    history = ctx.service.history(t.id)
    events = [ev.note for ev in history if ev.note]
    assert any(
        "scope-triage auto-REJECT (binary artifacts)" in note and "`test.db`" in note
        for note in events
    )

    # Result should be EXPAND (continue or skip_iteration depending on
    # whether expand files need re-run). Since README.md may or may not
    # already be in changed, either is fine — but it should not be a
    # return (which would mean BLOCKED).
    assert result.action in ("continue", "skip_iteration")


def test_binary_artifact_git_numstat_fallback(ctx_factory, tmp_path, monkeypatch):
    """A file without a known binary extension but detected by git numstat
    as binary is still auto-cleaned."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "a.txt")

    repo = ctx.service.workspace(t).dir / "repo"
    _clone_repo_to(ctx, remote, repo)

    # Create a file with a non-binary extension but binary content
    # (git will treat it as binary).
    weird_path = repo / "datafile.dat"
    weird_path.write_bytes(b"\x00\x01\x02\x03\x04\x05\x06\x07\x08")
    _git(repo, "add", "datafile.dat")
    _git(repo, "commit", "-q", "-m", "wip with misnamed binary")

    settings = ctx.settings

    import robotsix_mill.agents.scope_triage as scope_triage_mod

    triage_called = []

    def _fake_triage(
        *, settings, ticket_spec, file_map, out_of_scope_files, diff_summaries
    ):
        triage_called.append(1)
        raise AssertionError("scope-triage should not be called")

    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _fake_triage)

    result = ImplementStage._run_scope_guardrail(
        ctx,
        t,
        repo,
        f"mill/{t.id}",
        summary="agent summary",
        ref_files=None,
        file_map={"a.txt"},
        settings=settings,
        spec="add a.txt",
        current_feedback=None,
    )

    assert len(triage_called) == 0
    assert not weird_path.exists(), "misnamed binary should be removed from disk"
    assert result.action == "skip_iteration"


def test_binary_artifact_untracked_file_cleanup(ctx_factory, tmp_path, monkeypatch):
    """An untracked binary file (created by agent runtime, never committed)
    is still detected and cleaned by os.unlink after git checkout is a no-op."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "a.txt")

    repo = ctx.service.workspace(t).dir / "repo"
    _clone_repo_to(ctx, remote, repo)

    # Create an untracked binary file — NOT committed.
    untracked_db = repo / "mail.db"
    untracked_db.write_bytes(b"\x00\x01\x02\x03SQLite format 3\0")

    # Also modify a tracked, in-scope file so that changed_files returns
    # something we can work with alongside the untracked binary.
    (repo / "a.txt").write_text("modified in-scope file")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "in-scope change")

    settings = ctx.settings

    import robotsix_mill.agents.scope_triage as scope_triage_mod

    triage_called = []

    def _fake_triage(
        *, settings, ticket_spec, file_map, out_of_scope_files, diff_summaries
    ):
        triage_called.append(1)
        raise AssertionError("scope-triage should not be called")

    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _fake_triage)

    result = ImplementStage._run_scope_guardrail(
        ctx,
        t,
        repo,
        f"mill/{t.id}",
        summary="agent summary",
        ref_files=None,
        file_map={"a.txt"},
        settings=settings,
        spec="add a.txt",
        current_feedback=None,
    )

    assert len(triage_called) == 0
    assert not untracked_db.exists(), (
        "untracked binary artifact should be removed from disk"
    )
    assert result.action == "skip_iteration"


# --- scope-triage flood guard ---------------------------------------------


def test_scope_triage_flood_guard_blocks(ctx_factory, tmp_path, monkeypatch):
    """When the out-of-scope TEXT file count exceeds
    scope_triage_max_files, the flood guard short-circuits: the
    scope-triage LLM is NEVER called, the result is BLOCKED, and a
    flood-guard step event with a truncated sample is recorded."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        scope_triage_max_files=5,
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "a.txt")

    repo = ctx.service.workspace(t).dir / "repo"
    _clone_repo_to(ctx, remote, repo)

    # Create more out-of-scope text files than the cap (5) and well
    # past _FLOOD_SAMPLE_SIZE-independent truncation logic.
    n_files = 12
    for i in range(n_files):
        (repo / f"flood_{i:02d}.txt").write_text(f"flood file {i}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "wip: artifact flood")

    settings = ctx.settings

    import robotsix_mill.agents.scope_triage as scope_triage_mod

    triage_called = []

    def _fake_triage(
        *, settings, ticket_spec, file_map, out_of_scope_files, diff_summaries
    ):
        triage_called.append(1)
        raise AssertionError("scope-triage should not be called for a flood")

    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _fake_triage)

    result = ImplementStage._run_scope_guardrail(
        ctx,
        t,
        repo,
        f"mill/{t.id}",
        summary="agent summary",
        ref_files=None,
        file_map={"a.txt"},
        settings=settings,
        spec="add a.txt",
        current_feedback=None,
    )

    assert len(triage_called) == 0, "LLM must not be called for a flood"
    assert result.action == "return"
    assert result.outcome is not None
    assert result.outcome.next_state is State.BLOCKED
    assert "flood guard" in result.outcome.note
    # 12 files > _FLOOD_SAMPLE_SIZE? No (20). But the message still
    # reports the count and cap.
    assert "12" in result.outcome.note
    assert "5" in result.outcome.note

    # A flood-guard step event was recorded.
    events = [ev.note for ev in ctx.service.history(t.id) if ev.note]
    assert any("scope-triage flood guard" in note for note in events)


def test_scope_triage_flood_guard_truncates_sample(ctx_factory, tmp_path, monkeypatch):
    """When the out-of-scope count exceeds _FLOOD_SAMPLE_SIZE, the
    operator-facing message truncates the sample with a '+N more' marker."""
    from robotsix_mill.stages.implement import _FLOOD_SAMPLE_SIZE

    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        scope_triage_max_files=5,
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "a.txt")

    repo = ctx.service.workspace(t).dir / "repo"
    _clone_repo_to(ctx, remote, repo)

    n_files = _FLOOD_SAMPLE_SIZE + 5
    for i in range(n_files):
        (repo / f"flood_{i:03d}.txt").write_text(f"flood file {i}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "wip: big artifact flood")

    settings = ctx.settings

    import robotsix_mill.agents.scope_triage as scope_triage_mod

    monkeypatch.setattr(
        scope_triage_mod,
        "run_scope_triage_agent",
        lambda **_k: (_ for _ in ()).throw(AssertionError("must not be called")),
    )

    result = ImplementStage._run_scope_guardrail(
        ctx,
        t,
        repo,
        f"mill/{t.id}",
        summary="agent summary",
        ref_files=None,
        file_map={"a.txt"},
        settings=settings,
        spec="add a.txt",
        current_feedback=None,
    )

    assert result.outcome is not None
    assert result.outcome.next_state is State.BLOCKED
    assert "flood guard" in result.outcome.note
    assert "more)" in result.outcome.note
    assert "+5 more" in result.outcome.note


def test_scope_triage_flood_guard_below_cap_calls_llm(
    ctx_factory, tmp_path, monkeypatch
):
    """Out-of-scope count <= cap: the guard does NOT trip — the
    scope-triage LLM is still invoked normally."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        scope_triage_max_files=5,
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "a.txt")

    repo = ctx.service.workspace(t).dir / "repo"
    _clone_repo_to(ctx, remote, repo)

    # Only 2 out-of-scope text files — well under the cap of 5.
    for i in range(2):
        (repo / f"small_{i}.txt").write_text(f"small file {i}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "wip: small out-of-scope set")

    settings = ctx.settings

    import robotsix_mill.agents.scope_triage as scope_triage_mod
    from robotsix_mill.agents.scope_triage import ScopeTriageVerdict

    triage_called = []

    def _fake_triage(
        *, settings, ticket_spec, file_map, out_of_scope_files, diff_summaries
    ):
        triage_called.append(out_of_scope_files)
        return ScopeTriageVerdict(
            action="ESCALATE",
            justification="ambiguous",
            expand_files=[],
        )

    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _fake_triage)

    result = ImplementStage._run_scope_guardrail(
        ctx,
        t,
        repo,
        f"mill/{t.id}",
        summary="agent summary",
        ref_files=None,
        file_map={"a.txt"},
        settings=settings,
        spec="add a.txt",
        current_feedback=None,
    )

    assert len(triage_called) == 1, "LLM must be called below the cap"
    # ESCALATE → BLOCKED via the normal scope-triage path, NOT the flood guard.
    assert result.action == "return"
    assert result.outcome is not None
    assert result.outcome.next_state is State.BLOCKED
    assert "flood guard" not in (result.outcome.note or "")


# --- modules.yaml auto-EXPAND in scope guardrail --------------------------


def test_modules_yaml_repath_in_scope_auto_expands(ctx_factory, tmp_path, monkeypatch):
    """AC1: a refactor that re-paths in-scope modules in docs/modules.yaml
    is auto-EXPANDed — no LLM invoked, file_map gains the file."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
    )
    t = _ticket(ctx)

    repo = ctx.service.workspace(t).dir / "repo"
    _clone_repo_to(ctx, remote, repo)

    # Seed the base with old.py TRACKED and docs/modules.yaml pointing to it.
    (repo / "src" / "robotsix_mill").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "robotsix_mill" / "old.py").write_text("# old module")
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "modules.yaml").write_text(
        "modules:\n  - id: my_module\n    paths:\n      - src/robotsix_mill/old.py\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed old module")
    _git(repo, "push", "origin", "main")

    # Create the mill branch and move the module with git mv.
    branch = f"mill/{t.id}"
    _git(repo, "checkout", "-q", "-b", branch)
    pkg = repo / "src" / "robotsix_mill" / "pkg"
    pkg.mkdir(parents=True)
    _git(repo, "mv", "src/robotsix_mill/old.py", "src/robotsix_mill/pkg/new.py")

    # Re-path docs/modules.yaml to the new location.
    (repo / "docs" / "modules.yaml").write_text(
        "modules:\n"
        "  - id: my_module\n"
        "    paths:\n"
        "      - src/robotsix_mill/pkg/new.py\n"
    )
    _git(repo, "add", "docs/modules.yaml")
    _git(repo, "commit", "-q", "-m", "wip: move module")

    # file_map contains only the new path (the moved file).
    _write_file_map(ctx, t, "src/robotsix_mill/pkg/new.py")

    # Mock scope-triage to prove it is NOT called for this file.
    import robotsix_mill.agents.scope_triage as scope_triage_mod

    def _fake_triage(
        *, settings, ticket_spec, file_map, out_of_scope_files, diff_summaries
    ):
        raise AssertionError(
            "LLM must NOT be called — docs/modules.yaml should be "
            "auto-EXPANDed deterministically"
        )

    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _fake_triage)

    result = ImplementStage._run_scope_guardrail(
        ctx,
        t,
        repo,
        branch,
        summary="agent summary",
        ref_files=None,
        file_map={"src/robotsix_mill/pkg/new.py"},
        settings=ctx.settings,
        spec="move old.py to pkg/new.py",
        current_feedback=None,
    )

    # AC1: auto-EXPAND → skip_iteration
    assert result.action == "skip_iteration"
    assert result.file_map is not None
    assert "docs/modules.yaml" in result.file_map

    # Step event recording the auto-EXPAND was emitted.
    history = ctx.service.history(t.id)
    events = [ev.note for ev in history if ev.note]
    assert any(
        "scope-triage auto-EXPAND" in note
        and "docs/modules.yaml" in note
        and "registry sync" in note
        for note in events
    ), f"auto-EXPAND step event missing; history events: {events}"


def test_modules_yaml_new_unrelated_module_still_flagged(
    ctx_factory, tmp_path, monkeypatch
):
    """AC2: registering a NEW module in docs/modules.yaml with paths NOT in
    file_map is NOT auto-EXPANDed — it stays in out_of_scope and reaches
    the LLM (or blocks when triage is disabled)."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
    )
    t = _ticket(ctx)

    repo = ctx.service.workspace(t).dir / "repo"
    _clone_repo_to(ctx, remote, repo)

    # Seed docs/modules.yaml with a paths: entry.
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "modules.yaml").write_text(
        "modules:\n  - id: my_module\n    paths:\n      - src/robotsix_mill/legit.py\n"
    )
    _git(repo, "add", "docs/modules.yaml")
    _git(repo, "commit", "-q", "-m", "seed modules.yaml")
    _git(repo, "push", "origin", "main")

    # Create mill branch with a legitimate in-scope change AND an
    # unrelated modules.yaml addition.
    branch = f"mill/{t.id}"
    _git(repo, "checkout", "-q", "-b", branch)

    # In-scope change: the legit file.
    (repo / "src" / "robotsix_mill").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "robotsix_mill" / "legit.py").write_text("# legit change")
    _git(repo, "add", "src/robotsix_mill/legit.py")

    # Unrelated: register a brand-new module entry in modules.yaml.
    (repo / "docs" / "modules.yaml").write_text(
        "modules:\n"
        "  - id: my_module\n"
        "    paths:\n"
        "      - src/robotsix_mill/legit.py\n"
        "  - id: unrelated_module\n"
        "    paths:\n"
        "      - src/robotsix_mill/unrelated.py\n"
    )
    _git(repo, "add", "docs/modules.yaml")
    _git(repo, "commit", "-q", "-m", "wip: legit + unrelated registry")

    # file_map contains only the legitimate file.
    _write_file_map(ctx, t, "src/robotsix_mill/legit.py")

    # Mock scope-triage to capture what out_of_scope_files it receives.
    import robotsix_mill.agents.scope_triage as scope_triage_mod
    from robotsix_mill.agents.scope_triage import ScopeTriageVerdict

    triage_calls = []

    def _fake_triage(
        *, settings, ticket_spec, file_map, out_of_scope_files, diff_summaries
    ):
        triage_calls.append((out_of_scope_files, diff_summaries))
        return ScopeTriageVerdict(
            action="REJECT",
            justification="Unrelated module registered",
            expand_files=[],
        )

    monkeypatch.setattr(scope_triage_mod, "run_scope_triage_agent", _fake_triage)

    result = ImplementStage._run_scope_guardrail(
        ctx,
        t,
        repo,
        branch,
        summary="agent summary",
        ref_files=None,
        file_map={"src/robotsix_mill/legit.py"},
        settings=ctx.settings,
        spec="update legit.py",
        current_feedback=None,
    )

    # The triage agent WAS called (docs/modules.yaml was NOT auto-EXPANDed).
    assert len(triage_calls) == 1, (
        "scope-triage should be called because unrelated module path is not in file_map"
    )
    out_of_scope_files, _ = triage_calls[0]
    assert "docs/modules.yaml" in out_of_scope_files, (
        "docs/modules.yaml should remain in out_of_scope_files"
    )
    # The guardrail returns because the LLM issued REJECT.
    assert result.action == "return"


def test_modules_yaml_added_paths_parses_diff(tmp_path):
    """Unit test: _modules_yaml_added_paths correctly extracts added path
    tokens from a git diff, ignoring removed lines, comments, and non-path
    YAML keys."""
    from robotsix_mill.stages.implement import _modules_yaml_added_paths

    # Build a minimal git repo with a base and a branch that modifies
    # docs/modules.yaml.
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")

    # Base commit: empty modules.yaml.
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "modules.yaml").write_text(
        "modules:\n"
        "  - id: existing\n"
        "    description: already present\n"
        "    paths:\n"
        "      - src/robotsix_mill/existing.py\n"
    )
    _git(repo, "add", "docs/modules.yaml")
    _git(repo, "commit", "-q", "-m", "base")
    # Create a fake remote ref so origin/main resolves.
    _git(repo, "branch", "-M", "main")

    # Modify: add new paths, a comment line, a description line, and
    # delete an old path. The helper must:
    # - pick up the added paths (renamed.py, brand_new.py)
    # - ignore the removed path (existing.py)
    # - ignore comment/description/id lines.
    (repo / "docs" / "modules.yaml").write_text(
        "modules:\n"
        "  - id: existing\n"
        "    description: already present (updated description)\n"
        "    paths:\n"
        "      - src/robotsix_mill/renamed.py\n"
        "      - src/robotsix_mill/brand_new.py\n"
        "  # comment line\n"
    )
    _git(repo, "add", "docs/modules.yaml")

    # We need origin/main to be the base commit. Since we can't easily
    # make a real remote, we use a trick: tag the base commit as a
    # substitute for origin/main in the git diff call. Actually, the
    # helper uses `origin/{target_branch}`, so we need a real remote.
    # Create a bare clone as the "remote":
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", "-q", str(repo), str(bare)],
        check=True,
        capture_output=True,
    )
    _git(repo, "remote", "add", "origin", f"file://{bare}")
    # Fetch so origin/main is known locally.
    _git(repo, "fetch", "-q", "origin")

    # Now the helper should diff HEAD (uncommitted) against origin/main.
    added = _modules_yaml_added_paths(repo, "main")

    assert "src/robotsix_mill/renamed.py" in added, (
        f"expected renamed.py in added paths, got {added}"
    )
    assert "src/robotsix_mill/brand_new.py" in added, (
        f"expected brand_new.py in added paths, got {added}"
    )
    # Removed path must NOT appear.
    assert "src/robotsix_mill/existing.py" not in added, (
        "removed path existing.py should not be in added paths"
    )
    # Non-path lines must NOT appear.
    for non_path in (
        "description:",
        "id:",
        "modules:",
        "# comment line",
    ):
        assert non_path not in added, (
            f"non-path token {non_path!r} should not be in added paths"
        )
    # Comment and description variants.
    assert "already present (updated description)" not in added


# --- test-baseline check -------------------------------------------------


def test_baseline_check_blocks_on_failure(ctx_factory, tmp_path, monkeypatch):
    """AC1: pre-existing base-branch test failures block before the loop."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
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
        FORGE_REMOTE_URL=remote,
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
        FORGE_REMOTE_URL=remote, test_command="true", review_enabled="false"
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
        FORGE_REMOTE_URL=remote,
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
        FORGE_REMOTE_URL=remote,
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
        FORGE_REMOTE_URL=remote,
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
        FORGE_REMOTE_URL=remote,
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
        FORGE_REMOTE_URL=remote,
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
        FORGE_REMOTE_URL=remote,
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
        FORGE_REMOTE_URL=remote,
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
    from robotsix_mill.vcs import git_ops
    from robotsix_mill.forge.auth import github_token

    if repo_dir.exists():
        import shutil

        shutil.rmtree(repo_dir)
    token = None
    try:
        token = github_token(ctx.settings, repo_config=ctx.repo_config)
    except RuntimeError:
        pass
    git_ops.clone(remote_url, repo_dir, ctx.settings.forge_target_branch, token)


# --- multi-repo implement (meta tickets, N ≥ 1) ------------------------


def _make_bare_repo_in(tmp_path: Path, name: str) -> str:
    """Create a bare repo in ``tmp_path / name``."""
    sub = tmp_path / name
    sub.mkdir(parents=True, exist_ok=True)
    return make_bare_repo(sub)


def _build_multi_repo_clones(tmp_path, bare_remotes, ctx, ticket):
    """Create clones for *bare_remotes* under the ticket workspace
    in the ``ws.dir / "repos" / <id>`` layout used by meta workspaces.

    Returns ``(repo_dir, extra_roots)`` matching the return of
    ``build_meta_workspace``.
    """
    ws = ctx.service.workspace(ticket)
    repos_dir = ws.dir / "repos"
    repos_dir.mkdir(parents=True, exist_ok=True)
    clones = []
    for idx, remote in enumerate(bare_remotes):
        repo_id = f"test-repo-{idx}"
        dest = repos_dir / repo_id
        _clone_repo_to(ctx, remote, dest)
        clones.append(dest)
    return clones[0], clones


def test_multi_repo_happy_path_two_repos_both_touched(
    ctx_factory, tmp_path, monkeypatch
):
    """AC1: N=2 repos, both modified by the agent → both get a branch
    and a commit, and touched_repos.json lists both."""
    import robotsix_mill.meta.triage as mt
    import robotsix_mill.meta.workspace as mw

    remote_a = _make_bare_repo_in(tmp_path, "a")
    remote_b = _make_bare_repo_in(tmp_path, "b")

    ctx = ctx_factory(test_command="true", review_enabled="false")
    ctx.repo_config = None
    t = _ticket(ctx, title="Cross-repo change")
    t.board_id = "meta"
    _write_file_map(ctx, t, "feature.txt")

    repo_dir, extra_roots = _build_multi_repo_clones(
        tmp_path, [remote_a, remote_b], ctx, t
    )

    monkeypatch.setattr(
        mt,
        "required_repos_for",
        lambda *, settings, spec: ["test-repo-0", "test-repo-1"],
    )
    monkeypatch.setattr(
        mw,
        "build_meta_workspace",
        lambda settings, ws, repo_ids: (repo_dir, extra_roots),
    )

    def _agent(*, repo_dir, extra_roots, **kw):
        del kw
        # Write to BOTH repos so both get a commit.
        (Path(repo_dir) / "feature.txt").write_text("primary edit")
        for rp in extra_roots or []:
            (Path(rp) / "feature.txt").write_text("extra edit")
        return ("cross-repo done", ["feature.txt"], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _agent)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING

    # Both repos on the feature branch.
    branch = f"mill/{t.id}"
    for rp in extra_roots:
        head = subprocess.run(
            ["git", "-C", str(rp), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert head == branch, f"{rp.name} should be on {branch}, got {head}"

    # Both repos have a commit.
    for rp in extra_roots:
        log = subprocess.run(
            ["git", "-C", str(rp), "log", "-1", "--pretty=%s"],
            capture_output=True,
            text=True,
        ).stdout
        assert "Cross-repo change" in log, f"{rp.name} missing commit"
        assert "[WIP]" not in log, f"{rp.name} should not have WIP suffix"

    # touched_repos.json lists both repos.
    ws = ctx.service.workspace(t)
    tr_path = ws.artifacts_dir / "touched_repos.json"
    assert tr_path.exists(), "touched_repos.json should exist"
    tr = json.loads(tr_path.read_text(encoding="utf-8"))
    assert len(tr) == 2
    repo_ids = {entry["repo_id"] for entry in tr}
    assert repo_ids == {"test-repo-0", "test-repo-1"}
    for entry in tr:
        assert entry["branch"] == branch
        assert Path(entry["repo_path"]).exists()


def test_multi_repo_partial_edit_one_of_two_touched(ctx_factory, tmp_path, monkeypatch):
    """AC1 partial: N=2 repos, only one modified → only that one gets a
    commit, but both get branches. touched_repos.json lists only the
    touched repo."""
    import robotsix_mill.meta.triage as mt
    import robotsix_mill.meta.workspace as mw

    remote_a = _make_bare_repo_in(tmp_path, "a")
    remote_b = _make_bare_repo_in(tmp_path, "b")

    ctx = ctx_factory(test_command="true", review_enabled="false")
    ctx.repo_config = None
    t = _ticket(ctx, title="Partial cross-repo")
    t.board_id = "meta"
    _write_file_map(ctx, t, "feature.txt")

    repo_dir, extra_roots = _build_multi_repo_clones(
        tmp_path, [remote_a, remote_b], ctx, t
    )

    monkeypatch.setattr(
        mt,
        "required_repos_for",
        lambda *, settings, spec: ["test-repo-0", "test-repo-1"],
    )
    monkeypatch.setattr(
        mw,
        "build_meta_workspace",
        lambda settings, ws, repo_ids: (repo_dir, extra_roots),
    )

    def _agent(*, repo_dir, extra_roots, **kw):
        del kw
        # Only write to the PRIMARY repo — extra root stays clean.
        (Path(repo_dir) / "feature.txt").write_text("primary only")
        return ("partial done", ["feature.txt"], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _agent)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING

    branch = f"mill/{t.id}"
    # Both repos still have the feature branch.
    for rp in extra_roots:
        head = subprocess.run(
            ["git", "-C", str(rp), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert head == branch, f"{rp.name} should be on {branch}, got {head}"

    # Primary has a commit; extra root does NOT.
    primary_log = subprocess.run(
        ["git", "-C", str(repo_dir), "log", "-1", "--pretty=%s"],
        capture_output=True,
        text=True,
    ).stdout
    assert "Partial cross-repo" in primary_log

    extra_log = subprocess.run(
        ["git", "-C", str(extra_roots[1]), "log", "-1", "--pretty=%s"],
        capture_output=True,
        text=True,
    ).stdout
    # The extra root should have only its initial commit (or none beyond
    # the clone), NOT a mill: commit.
    assert "mill:" not in extra_log

    # touched_repos.json lists only the touched repo.
    ws = ctx.service.workspace(t)
    tr_path = ws.artifacts_dir / "touched_repos.json"
    assert tr_path.exists()
    tr = json.loads(tr_path.read_text(encoding="utf-8"))
    assert len(tr) == 1
    assert tr[0]["repo_id"] == "test-repo-0"


def test_single_repo_meta_regression(ctx_factory, tmp_path, monkeypatch):
    """AC2: N=1 meta ticket produces one branch, one commit, and
    touched_repos.json with a single entry (backward-compatible with
    current single-repo flow)."""
    import robotsix_mill.meta.triage as mt
    import robotsix_mill.meta.workspace as mw

    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(test_command="true", review_enabled="false")
    ctx.repo_config = None
    t = _ticket(ctx, title="Single-repo meta")
    t.board_id = "meta"
    _write_file_map(ctx, t, "feature.txt")

    repo_dir, extra_roots = _build_multi_repo_clones(tmp_path, [remote], ctx, t)

    monkeypatch.setattr(
        mt, "required_repos_for", lambda *, settings, spec: ["test-repo-0"]
    )
    monkeypatch.setattr(
        mw,
        "build_meta_workspace",
        lambda settings, ws, repo_ids: (repo_dir, extra_roots),
    )

    def _agent(*, repo_dir, extra_roots, **kw):
        del kw
        (Path(repo_dir) / "feature.txt").write_text("solo edit")
        return ("single done", ["feature.txt"], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _agent)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING

    branch = f"mill/{t.id}"
    head = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head == branch

    log = subprocess.run(
        ["git", "-C", str(repo_dir), "log", "-1", "--pretty=%s"],
        capture_output=True,
        text=True,
    ).stdout
    assert "Single-repo meta" in log

    ws = ctx.service.workspace(t)
    tr_path = ws.artifacts_dir / "touched_repos.json"
    assert tr_path.exists()
    tr = json.loads(tr_path.read_text(encoding="utf-8"))
    assert len(tr) == 1
    assert tr[0]["repo_id"] == "test-repo-0"
    assert tr[0]["branch"] == branch


def test_non_meta_ticket_no_touched_repos_json(ctx_factory, tmp_path, monkeypatch):
    """AC3: non-meta ticket → _finalize with extra_roots=None → no
    touched_repos.json produced, commit only primary repo_dir."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote, test_command="true", review_enabled="false"
    )
    monkeypatch.setattr(
        coding, "run_implement_agent", _fake_agent({"feature.txt": "x"})
    )
    t = _ticket(ctx)
    _write_file_map(ctx, t, "feature.txt")

    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING

    ws = ctx.service.workspace(t)
    tr_path = ws.artifacts_dir / "touched_repos.json"
    assert not tr_path.exists(), "non-meta tickets must not produce touched_repos.json"

    # Primary repo still committed as before.
    repo = ws.dir / "repo"
    log = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--pretty=%s"],
        capture_output=True,
        text=True,
    ).stdout
    assert "Add feature" in log
    assert "[WIP]" not in log


def test_multi_repo_resume_after_blocked(ctx_factory, tmp_path, monkeypatch):
    """AC4: meta ticket BLOCKED mid-implement (budget cap) and later
    resumed correctly checkouts existing branches in all repos and
    commits only repos with new changes."""
    import robotsix_mill.meta.triage as mt
    import robotsix_mill.meta.workspace as mw

    remote_a = _make_bare_repo_in(tmp_path, "a")
    remote_b = _make_bare_repo_in(tmp_path, "b")

    ctx = ctx_factory(test_command="true", review_enabled="false")
    ctx.repo_config = None
    t = _ticket(ctx, title="Resume cross-repo")
    t.board_id = "meta"
    _write_file_map(ctx, t, "first.txt", "second.txt")

    repo_dir, extra_roots = _build_multi_repo_clones(
        tmp_path, [remote_a, remote_b], ctx, t
    )

    monkeypatch.setattr(
        mt,
        "required_repos_for",
        lambda *, settings, spec: ["test-repo-0", "test-repo-1"],
    )
    monkeypatch.setattr(
        mw,
        "build_meta_workspace",
        lambda settings, ws, repo_ids: (repo_dir, extra_roots),
    )

    call_count = {"n": 0}

    def _agent(*, repo_dir, extra_roots, **kw):
        del kw
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First pass: write to primary only, then hit budget cap.
            (Path(repo_dir) / "first.txt").write_text("partial work")
            raise coding.AgentBudgetError("cap hit", [])
        # Resume pass: write to BOTH repos.
        (Path(repo_dir) / "second.txt").write_text("resumed work")
        for rp in extra_roots or []:
            if rp != repo_dir:
                (rp / "second.txt").write_text("extra resumed work")
        return ("done on resume", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _agent)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    # First run → BLOCKED.
    out1 = ImplementStage().run(t, ctx)
    assert out1.next_state is State.BLOCKED
    assert call_count["n"] == 1

    # Primary repo has WIP commit; extra root has only branch (no commit).
    branch = f"mill/{t.id}"
    primary_log = subprocess.run(
        ["git", "-C", str(repo_dir), "log", "--pretty=%s"],
        capture_output=True,
        text=True,
    ).stdout
    assert "WIP" in primary_log
    extra_log = subprocess.run(
        ["git", "-C", str(extra_roots[1]), "log", "--pretty=%s"],
        capture_output=True,
        text=True,
    ).stdout
    assert "mill:" not in extra_log  # no commit on the untouched extra repo

    # Resume: operator moves back to READY.
    ctx.service.transition(t.id, out1.next_state, out1.note)
    ctx.service.transition(t.id, State.READY, "retry")
    t2 = ctx.service.get(t.id)
    t2.board_id = "meta"  # re-fetch loses board_id override

    out2 = ImplementStage().run(t2, ctx)
    assert out2.next_state is State.DOCUMENTING
    assert call_count["n"] == 2

    # Both repos now have the feature branch checked out.
    for rp in extra_roots:
        head = subprocess.run(
            ["git", "-C", str(rp), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert head == branch

    # Both repos have commits from the resume pass.
    ws = ctx.service.workspace(t2)
    tr = json.loads(
        (ws.artifacts_dir / "touched_repos.json").read_text(encoding="utf-8")
    )
    assert len(tr) == 2


def test_no_change_needed_guard_multi_repo_extra_has_changes(
    ctx_factory, tmp_path, monkeypatch
):
    """AC5: agent sets no_change_needed=True but an extra root has
    uncommitted changes → the DONE bypass must NOT fire; ticket
    proceeds normally so deliver can pick up the changes."""
    import robotsix_mill.meta.triage as mt
    import robotsix_mill.meta.workspace as mw

    remote_a = _make_bare_repo_in(tmp_path, "a")
    remote_b = _make_bare_repo_in(tmp_path, "b")

    ctx = ctx_factory(test_command="true", review_enabled="false")
    ctx.repo_config = None
    t = _ticket(ctx, title="No-change with dirty extra")
    t.board_id = "meta"
    _write_file_map(ctx, t, "feature.txt")

    repo_dir, extra_roots = _build_multi_repo_clones(
        tmp_path, [remote_a, remote_b], ctx, t
    )

    monkeypatch.setattr(
        mt,
        "required_repos_for",
        lambda *, settings, spec: ["test-repo-0", "test-repo-1"],
    )
    monkeypatch.setattr(
        mw,
        "build_meta_workspace",
        lambda settings, ws, repo_ids: (repo_dir, extra_roots),
    )

    def _agent(*, repo_dir, extra_roots, **kw):
        del kw
        # Do NOT touch primary repo. Write to an EXTRA root only.
        extra_root = [rp for rp in extra_roots if rp != repo_dir][0]
        (extra_root / "feature.txt").write_text("only extra edit")
        return (
            "spec already satisfied",
            [],
            "",
            None,
            None,
            True,
            "The spec's desired state is already present.",
        )

    monkeypatch.setattr(coding, "run_implement_agent", _agent)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    out = ImplementStage().run(t, ctx)

    # MUST NOT be DONE — the extra root has changes that need delivering.
    assert out.next_state is not State.DONE, (
        f"extra root has uncommitted changes; should not route to DONE, got {out.next_state}"
    )
    # Should proceed normally (DOCUMENTING since review_enabled=false).
    assert out.next_state is State.DOCUMENTING

    # touched_repos.json lists the extra root.
    ws = ctx.service.workspace(t)
    tr = json.loads(
        (ws.artifacts_dir / "touched_repos.json").read_text(encoding="utf-8")
    )
    assert len(tr) == 1
    assert tr[0]["repo_id"] == "test-repo-1"


def test_silent_no_change_guard_multi_repo(ctx_factory, tmp_path, monkeypatch):
    """AC6: agent edits an extra repo but does NOT set no_change_needed
    and does NOT set the flag. The BLOCKED guard fires only if NO repo
    has changes — with an extra-root change it proceeds normally."""
    import robotsix_mill.meta.triage as mt
    import robotsix_mill.meta.workspace as mw

    remote_a = _make_bare_repo_in(tmp_path, "a")
    remote_b = _make_bare_repo_in(tmp_path, "b")

    ctx = ctx_factory(test_command="true", review_enabled="false")
    ctx.repo_config = None
    t = _ticket(ctx, title="Silent extra edit")
    t.board_id = "meta"
    _write_file_map(ctx, t, "feature.txt")

    repo_dir, extra_roots = _build_multi_repo_clones(
        tmp_path, [remote_a, remote_b], ctx, t
    )

    monkeypatch.setattr(
        mt,
        "required_repos_for",
        lambda *, settings, spec: ["test-repo-0", "test-repo-1"],
    )
    monkeypatch.setattr(
        mw,
        "build_meta_workspace",
        lambda settings, ws, repo_ids: (repo_dir, extra_roots),
    )

    def _agent(*, repo_dir, extra_roots, **kw):
        del kw
        # Write only to an EXTRA root, leave primary untouched.
        extra_root = [rp for rp in extra_roots if rp != repo_dir][0]
        (extra_root / "feature.txt").write_text("silent extra edit")
        # Agent does NOT set no_change_needed.
        return ("silent work done", [], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _agent)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    out = ImplementStage().run(t, ctx)

    # Must NOT be BLOCKED with "no changes produced" — the extra root
    # has changes so the guard should fall through to normal proceed.
    assert "no changes" not in (out.note or "").lower(), (
        f"extra root has changes; should not BLOCK for no-changes, got {out.note}"
    )
    assert out.next_state is State.DOCUMENTING


def test_touched_repos_json_content_validation(ctx_factory, tmp_path, monkeypatch):
    """AC5 variant: verify touched_repos.json has correct schema —
    repo_id, branch, repo_path — and each repo_path points to an
    existing directory."""
    import robotsix_mill.meta.triage as mt
    import robotsix_mill.meta.workspace as mw

    remote_a = _make_bare_repo_in(tmp_path, "a")
    remote_b = _make_bare_repo_in(tmp_path, "b")

    ctx = ctx_factory(test_command="true", review_enabled="false")
    ctx.repo_config = None
    t = _ticket(ctx, title="Schema check")
    t.board_id = "meta"
    _write_file_map(ctx, t, "feature.txt")

    repo_dir, extra_roots = _build_multi_repo_clones(
        tmp_path, [remote_a, remote_b], ctx, t
    )

    monkeypatch.setattr(
        mt,
        "required_repos_for",
        lambda *, settings, spec: ["test-repo-0", "test-repo-1"],
    )
    monkeypatch.setattr(
        mw,
        "build_meta_workspace",
        lambda settings, ws, repo_ids: (repo_dir, extra_roots),
    )

    def _agent(*, repo_dir, extra_roots, **kw):
        del kw
        (Path(repo_dir) / "feature.txt").write_text("edit A")
        for rp in extra_roots or []:
            if rp != repo_dir:
                (rp / "feature.txt").write_text("edit B")
        return ("done", ["feature.txt"], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _agent)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    out = ImplementStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING

    ws = ctx.service.workspace(t)
    tr_path = ws.artifacts_dir / "touched_repos.json"
    assert tr_path.exists()
    tr = json.loads(tr_path.read_text(encoding="utf-8"))

    branch = f"mill/{t.id}"
    for entry in tr:
        assert set(entry.keys()) == {"repo_id", "branch", "repo_path"}, (
            f"unexpected keys in {entry}"
        )
        assert entry["repo_id"] in ("test-repo-0", "test-repo-1")
        assert entry["branch"] == branch
        assert Path(entry["repo_path"]).exists()
        assert Path(entry["repo_path"]).is_dir()
        assert (Path(entry["repo_path"]) / ".git").exists()


def test_touched_repos_json_empty_on_no_change(ctx_factory, tmp_path, monkeypatch):
    """On the no-change-needed path (no commits anywhere), touched_repos.json
    is written as an empty list."""
    import robotsix_mill.meta.triage as mt
    import robotsix_mill.meta.workspace as mw

    remote_a = _make_bare_repo_in(tmp_path, "a")
    remote_b = _make_bare_repo_in(tmp_path, "b")

    ctx = ctx_factory(test_command="true", review_enabled="false")
    ctx.repo_config = None
    t = _ticket(ctx, title="No real change")
    t.board_id = "meta"
    _write_file_map(ctx, t, "feature.txt")

    repo_dir, extra_roots = _build_multi_repo_clones(
        tmp_path, [remote_a, remote_b], ctx, t
    )

    monkeypatch.setattr(
        mt,
        "required_repos_for",
        lambda *, settings, spec: ["test-repo-0", "test-repo-1"],
    )
    monkeypatch.setattr(
        mw,
        "build_meta_workspace",
        lambda settings, ws, repo_ids: (repo_dir, extra_roots),
    )

    def _agent(*, repo_dir, extra_roots, **kw):
        del kw
        # Touch NOTHING. Agent says no_change_needed with rationale.
        return (
            "no work needed",
            [],
            "",
            None,
            None,
            True,
            "All repos already satisfy the spec.",
        )

    monkeypatch.setattr(coding, "run_implement_agent", _agent)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    out = ImplementStage().run(t, ctx)

    # No changes anywhere → DONE bypass fires.
    assert out.next_state is State.DONE
    assert "no change needed" in (out.note or "").lower()

    ws = ctx.service.workspace(t)
    tr_path = ws.artifacts_dir / "touched_repos.json"
    assert tr_path.exists(), "touched_repos.json must be written even on no-change path"
    tr = json.loads(tr_path.read_text(encoding="utf-8"))
    assert tr == [], f"expected empty list, got {tr}"


# --- prerequisite gate --------------------------------------------------


def _no_prereq_block_spec():
    return "## Problem\nDo a thing.\n## Acceptance criteria\n- works\n"


def test_prereq_gate_disabled_never_checks(ctx_factory, tmp_path, monkeypatch):
    """Gate explicitly disabled: run_prerequisite_check is never called and
    behaviour is unchanged — the stage proceeds to the agent."""
    from robotsix_mill.agents import prerequisite

    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
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
        FORGE_REMOTE_URL=remote,
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
        FORGE_REMOTE_URL=remote,
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
        FORGE_REMOTE_URL=remote,
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
        FORGE_REMOTE_URL=remote,
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
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
        prerequisite_gate_enabled="true",
    )

    prereq_called = []

    def _spy_prereq(*args, **kwargs):
        prereq_called.append(1)
        return None

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
        FORGE_REMOTE_URL=remote,
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
        FORGE_REMOTE_URL=remote,
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
        FORGE_REMOTE_URL=remote,
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
        FORGE_REMOTE_URL=remote,
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
        FORGE_REMOTE_URL=remote,
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
        FORGE_REMOTE_URL=remote,
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
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
        implement_max_spawns_per_ticket="1",
    )
    t = _ticket(ctx, title="Spawn cap ticket", body="Add a feature.txt file")
    _write_file_map(ctx, t, "feature.txt")

    # Seed the counter at the limit — next preflight trips the gate.
    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "implement_spawn_count").write_text("1", encoding="utf-8")

    out = ImplementStage().preflight(t, ctx)

    assert out is not None
    assert out.next_state is State.BLOCKED
    assert "spawn limit reached" in out.note.lower()


def test_spawn_counter_increments_each_run(ctx_factory, tmp_path, monkeypatch):
    """Each preflight call increments the spawn counter file."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
        implement_max_spawns_per_ticket="5",
    )
    t = _ticket(ctx, title="Counter ticket", body="Add a feature.txt file")
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    def _agent(*, repo_dir, spec, **kwargs):
        (Path(repo_dir) / "feature.txt").write_text("done")
        return ("done", ["feature.txt"], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _agent)

    # First invocation: preflight increments counter from 0 to 1.
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


def test_spawn_counter_disabled_when_set_to_zero(ctx_factory, tmp_path, monkeypatch):
    """When ``implement_max_spawns_per_ticket=0`` the counter gate is
    skipped entirely."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
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


# --- epic context in preflight spec check --------------------------------


def test_preflight_epic_context_allows_empty_direct_spec(
    ctx_factory, tmp_path, monkeypatch
):
    """An epic child with an empty direct body but non-empty parent epic
    must pass the preflight spec gate (epic context inherited)."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
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
        FORGE_REMOTE_URL=remote,
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
        FORGE_REMOTE_URL=remote,
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
        FORGE_REMOTE_URL=remote,
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
        FORGE_REMOTE_URL=remote,
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
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Fresh ticket", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    # No implement.md exists — preflight should proceed.
    out = ImplementStage().preflight(t, ctx)
    assert out is None, f"preflight must allow on first run, got: {out}"


def test_convergence_backstop_writes_implement_md(ctx_factory, tmp_path, monkeypatch):
    """When the convergence backstop fires (empty diff after review) it
    now terminates DONE (already satisfied). implement.md must still be
    written (with the passed marker + spec-fingerprint) so the artifact
    trail is intact."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="true",
        max_implement_review_cycles="10",
    )
    t = _ticket(ctx, title="Convergence ticket", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    # Run implement once so a branch and commits exist.
    def _agent(*, repo_dir, spec, **kwargs):
        (Path(repo_dir) / "feature.txt").write_text("implemented")
        return ("done", ["feature.txt"], "", None, None, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _agent)
    out1 = ImplementStage().run(t, ctx)
    assert out1.next_state is State.CODE_REVIEW

    # Simulate returning from review with no new commits.
    t = ctx.service.get(t.id)
    ctx.service.set_review_rounds(t.id, 1)
    ws = ctx.service.workspace(t)
    repo_dir = ws.dir / "repo"
    branch = f"{ctx.settings.branch_prefix}{t.id}"
    subprocess.run(
        ["git", "-C", str(repo_dir), "reset", "--hard", "origin/main"],
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

    # Second run: convergence backstop fires → DONE (already satisfied).
    out2 = ImplementStage().run(t, ctx)
    assert out2.next_state is State.DONE
    assert "already satisfied" in out2.note.lower()

    # implement.md must have been written with the passed outcome.
    md = ws.artifacts_dir / "implement.md"
    assert md.exists(), "convergence backstop must write implement.md"
    content = md.read_text(encoding="utf-8")
    assert "passed" in content
    assert "spec-fingerprint:" in content


def test_transient_agent_error_does_not_persist_fingerprint(
    ctx_factory, tmp_path, monkeypatch
):
    """When the implement agent raises AgentRunError with a transient
    cause (simulating a network blip), _finalize must NOT be called,
    so no fingerprint is persisted.  The next pass must re-implement
    normally instead of hitting the stale-re-spawn guard."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Transient blip", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    from robotsix_mill.agents.coding import AgentRunError

    # Simulate a transient error: AgentRunError wrapping an
    # httpx.ConnectError (classified as transient by
    # classify_stage_error).
    cause = None
    try:
        import httpx

        cause = httpx.ConnectError("Connection refused")
    except ImportError:
        cause = ConnectionError("Connection refused")

    def _agent_transient(*, repo_dir, spec, **kwargs):
        raise AgentRunError("transient blip", messages=[], cause=cause)

    monkeypatch.setattr(coding, "run_implement_agent", _agent_transient)

    # The implement stage should raise the transient cause, NOT
    # return a normal Outcome.
    with pytest.raises(Exception):  # noqa: B017
        ImplementStage().run(t, ctx)

    # implement.md must NOT exist (or must not have a fingerprint)
    # because _finalize was never called for this transient error.
    ws = ctx.service.workspace(t)
    implement_md = ws.artifacts_dir / "implement.md"
    if implement_md.exists():
        content = implement_md.read_text(encoding="utf-8")
        assert "spec-fingerprint:" not in content, (
            "transient error must not persist a spec fingerprint"
        )


def test_spec_determined_error_persists_fingerprint(ctx_factory, tmp_path, monkeypatch):
    """When the implement agent raises AgentRunError with a NON-transient
    cause (a genuine spec/logic dead-end), _finalize IS called and the
    fingerprint IS persisted so the stale-re-spawn guard can block a
    re-run with an unchanged spec."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Hard error", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    monkeypatch.setattr(ImplementStage, "_run_prerequisite_gate", lambda *a, **kw: None)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)

    from robotsix_mill.agents.coding import AgentRunError

    # A ValueError is NOT classified as transient — it's a spec-level
    # dead-end (the agent couldn't make sense of the spec).
    cause = ValueError("cannot parse specification")

    def _agent_fatal(*, repo_dir, spec, **kwargs):
        raise AgentRunError("fatal spec error", messages=[], cause=cause)

    monkeypatch.setattr(coding, "run_implement_agent", _agent_fatal)

    out = ImplementStage().run(t, ctx)
    # Non-transient → BLOCKED outcome with fingerprint persisted.
    assert out.next_state is State.BLOCKED

    ws = ctx.service.workspace(t)
    implement_md = ws.artifacts_dir / "implement.md"
    assert implement_md.exists(), "spec-determined error must write implement.md"
    content = implement_md.read_text(encoding="utf-8")
    assert "spec-fingerprint:" in content, (
        "spec-determined error must persist a spec fingerprint"
    )
    assert "BLOCKED — resumable" in content


def test_stale_respawn_guard_allows_after_transient(ctx_factory, tmp_path, monkeypatch):
    """When implement.md has no fingerprint (written by a pre-fix
    transient attempt, or the fingerprint was omitted), preflight
    must NOT block — it must allow the re-spawn."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="true",
        review_enabled="false",
    )
    t = _ticket(ctx, title="Transient resume", body="Implement feature X")
    _write_file_map(ctx, t, "feature.txt")

    ws = ctx.service.workspace(t)
    # Write implement.md with BLOCKED status but NO fingerprint line
    # (simulating a transient-failure artifact from before the fix
    # was applied, or a fresh transient attempt).
    (ws.artifacts_dir / "implement.md").write_text(
        "# Implement (BLOCKED — resumable)\n"
        "branch: test-branch\n"
        "\nno changes produced (transient abort)\n",
        encoding="utf-8",
    )

    # Preflight should NOT block — no fingerprint means transient.
    out = ImplementStage().preflight(t, ctx)
    assert out is None, f"preflight must allow when fingerprint is absent, got: {out}"


def test_stuck_no_diff_passes_aborts_loop(ctx_factory, tmp_path, monkeypatch):
    """After N consecutive passes with no file edits, the loop aborts
    with a 'stuck' BLOCKED instead of exhausting max_fix_iterations."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="false",  # gate always fails → retry
        max_fix_iterations="8",  # high enough that stuck fires first
    )
    call_count = [0]

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
        call_count[0] += 1
        # Produce NO file edits — the agent only reads / explores.
        # Return new_msgs that simulate a stuck read_ticket loop.
        import json

        msgs = json.dumps(
            [
                {
                    "parts": [
                        {
                            "part_kind": "tool-call",
                            "tool_name": "read_ticket",
                            "args": {},
                            "tool_call_id": f"call_rt_{call_count[0]}",
                        }
                    ]
                }
            ]
        ).encode()
        return (f"attempt {call_count[0]}", [], "", None, msgs, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)
    t = _ticket(ctx)
    _write_file_map(ctx, t, "wip.txt")

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "stuck" in out.note.lower()
    # Should abort at _STUCK_NO_DIFF_PASSES (3) + 1, not at max_fix_iterations (8).
    assert call_count[0] <= 4  # 3 no-diff passes + maybe the initial


def test_stuck_cumulative_tool_calls_aborts_loop(ctx_factory, tmp_path, monkeypatch):
    """After M cumulative tool calls across passes with no git diff,
    the loop aborts with a 'stuck' BLOCKED."""
    remote = make_bare_repo(tmp_path)
    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
        test_command="false",  # gate always fails → retry
        max_fix_iterations="8",
    )
    call_count = [0]

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
        call_count[0] += 1
        # Produce NO file edits but simulate HEAVY tool usage (many
        # read_ticket calls per pass) so the cumulative cap fires
        # before _STUCK_NO_DIFF_PASSES.
        import json

        parts = []
        for i in range(30):  # 30 tool calls per pass
            parts.append(
                {
                    "part_kind": "tool-call",
                    "tool_name": "read_ticket",
                    "args": {},
                    "tool_call_id": f"call_rt_{call_count[0]}_{i}",
                }
            )
        msgs = json.dumps([{"parts": parts}]).encode()
        return (f"heavy pass {call_count[0]}", [], "", None, msgs, False, "")

    monkeypatch.setattr(coding, "run_implement_agent", _run)
    monkeypatch.setattr(ImplementStage, "_run_baseline_check", lambda *a, **kw: None)
    t = _ticket(ctx)
    _write_file_map(ctx, t, "wip.txt")

    out = ImplementStage().run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert "stuck" in out.note.lower()
    assert "cumulative tool calls" in out.note.lower()
    # 30 calls/pass, cap at 50 → fires on second pass.
    assert call_count[0] <= 3


# --- transient fingerprint guard -------------------------------------------


def test_transient_header_skips_fingerprint_guard(ctx_factory, tmp_path, monkeypatch):
    """When implement.md records a TRANSIENT outcome, preflight must NOT
    block — the transient abort was environmental, not spec-determined."""
    remote = make_bare_repo(tmp_path)

    ctx = ctx_factory(
        FORGE_REMOTE_URL=remote,
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
        FORGE_REMOTE_URL=remote,
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
