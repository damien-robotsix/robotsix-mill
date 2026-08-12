import json
import subprocess
from pathlib import Path

from robotsix_mill.agents import coding
from robotsix_mill.core.states import State
from robotsix_mill.stages.implement import ImplementStage
from tests.stages.implement.conftest import _ticket, _write_file_map
from tests.stages.implement.test_implement import (
    _clone_repo_to,
    _fake_agent,
    make_bare_repo,
)

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
        extra_root = next(rp for rp in extra_roots if rp != repo_dir)
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
        extra_root = next(rp for rp in extra_roots if rp != repo_dir)
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
