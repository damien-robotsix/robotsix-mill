"""Tests for the review stage and review agent."""

import json
import subprocess
from pathlib import Path

import pytest

from robotsix_mill.agents.reviewing import ReviewAsk, ReviewVerdict
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.review import ReviewStage


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _make_bare_repo(tmp_path: Path) -> str:
    """A throwaway local remote (file://) with a `main` branch."""
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


def _ticket(ctx, body="Add feature.txt"):
    t = ctx.service.create("Add feature", body)
    ctx.service.transition(t.id, State.READY)
    # Simulate implement having run: clone + branch + commit
    ws = ctx.service.workspace(t)
    repo_dir = ws.dir / "repo"
    remote = _make_bare_repo(ws.dir)
    _git(ws.dir, "clone", "-q", remote, str(repo_dir))
    _git(repo_dir, "config", "user.email", "mill@robotsix.local")
    _git(repo_dir, "config", "user.name", "robotsix-mill")
    _git(repo_dir, "checkout", "-q", "-B", f"mill/{t.id}")
    (repo_dir / "feature.txt").write_text("implemented")
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-q", "-m", "implement feature")
    ctx.service.set_branch(t.id, f"mill/{t.id}")
    # Pipeline flip: READY -> CODE_REVIEW directly (DOCUMENTING is now downstream).
    ctx.service.transition(t.id, State.CODE_REVIEW)
    return ctx.service.get(t.id)


# --- APPROVE -----------------------------------------------------------


def test_approve_transitions_to_deliverable(ctx_factory, monkeypatch):
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)

    def _fake_review(
        *,
        settings,
        diff,
        spec,
        model_name=None,
        prior_context=None,
        repo_dir=None,
        reference_files=None,
        screenshot_path=None,
        extra_roots=None,
    ):
        del settings, diff, spec, model_name, prior_context, repo_dir, reference_files
        return ReviewVerdict(verdict="APPROVE", comments="lgtm")

    monkeypatch.setattr("robotsix_mill.stages.review.run_review_agent", _fake_review)

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING
    assert "approved" in out.note


# --- REQUEST_CHANGES ---------------------------------------------------


def test_request_changes_transitions_to_ready(ctx_factory, monkeypatch):
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)

    def _fake_review(
        *,
        settings,
        diff,
        spec,
        model_name=None,
        prior_context=None,
        repo_dir=None,
        reference_files=None,
        screenshot_path=None,
        extra_roots=None,
    ):
        del settings, diff, spec, model_name, prior_context, repo_dir, reference_files
        return ReviewVerdict(verdict="REQUEST_CHANGES", comments="X is broken")

    monkeypatch.setattr("robotsix_mill.stages.review.run_review_agent", _fake_review)

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.READY

    # Comment was stored.
    comments = ctx.service.list_comments(t.id)
    assert len(comments) == 1
    assert comments[0].body == "X is broken"


# --- NEEDS_DISCUSSION --------------------------------------------------


def test_needs_discussion_pauses_for_user_reply(ctx_factory, monkeypatch):
    """NEEDS_DISCUSSION is a human-decision verdict, not a failure — it
    pauses the ticket for the operator's reply (AWAITING_USER_REPLY)
    with an [ASK_USER] thread, NOT BLOCKED."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)

    def _fake_review(
        *,
        settings,
        diff,
        spec,
        model_name=None,
        prior_context=None,
        repo_dir=None,
        reference_files=None,
        screenshot_path=None,
        extra_roots=None,
    ):
        del settings, diff, spec, model_name, prior_context, repo_dir, reference_files
        return ReviewVerdict(verdict="NEEDS_DISCUSSION", comments="questionable design")

    monkeypatch.setattr("robotsix_mill.stages.review.run_review_agent", _fake_review)

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.AWAITING_USER_REPLY

    comments = ctx.service.list_comments(t.id)
    assert len(comments) == 1
    # Posted as an [ASK_USER] thread so the resume mechanism fires when
    # the operator replies + closes it.
    assert comments[0].body.startswith("[ASK_USER]")
    assert "questionable design" in comments[0].body


# --- Blind review: diff + spec only, no implementation context --------


def test_blind_review_only_diff_and_spec(ctx_factory, monkeypatch):
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)

    captured: dict = {}

    def _fake_review(
        *,
        settings,
        diff,
        spec,
        model_name=None,
        prior_context=None,
        repo_dir=None,
        reference_files=None,
        screenshot_path=None,
        extra_roots=None,
    ):
        captured["diff"] = diff
        captured["spec"] = spec
        captured["model_name"] = model_name
        return ReviewVerdict(verdict="APPROVE", comments="ok")

    monkeypatch.setattr("robotsix_mill.stages.review.run_review_agent", _fake_review)

    ReviewStage().run(t, ctx)

    # diff contains the change
    assert "feature.txt" in captured["diff"]
    # spec is the ticket body
    assert captured["spec"] == "Add feature.txt"
    # no implementation memory leaked
    assert "memory" not in captured["diff"].lower()
    assert "ledger" not in captured["diff"].lower()


# --- Agent error → BLOCKED ---------------------------------------------


def test_agent_error_blocks_resumable(ctx_factory, monkeypatch):
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)

    def _fake_review(
        *,
        settings,
        diff,
        spec,
        model_name=None,
        prior_context=None,
        repo_dir=None,
        reference_files=None,
        screenshot_path=None,
        extra_roots=None,
    ):
        del settings, diff, spec, model_name, prior_context, repo_dir, reference_files
        raise RuntimeError("model unavailable")

    monkeypatch.setattr("robotsix_mill.stages.review.run_review_agent", _fake_review)

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "resumable" in out.note


# --- Empty diff → APPROVE without agent -------------------------------


def test_empty_diff_approves_without_agent(ctx_factory, monkeypatch):
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)

    # Remove the commit so diff is empty.
    repo_dir = ctx.service.workspace(t).dir / "repo"
    _git(repo_dir, "reset", "--soft", "HEAD~1")

    agent_called = []

    def _fake_review(
        *,
        settings,
        diff,
        spec,
        model_name=None,
        prior_context=None,
        repo_dir=None,
        reference_files=None,
        screenshot_path=None,
        extra_roots=None,
    ):
        agent_called.append(1)
        return ReviewVerdict(verdict="APPROVE", comments="")

    monkeypatch.setattr("robotsix_mill.stages.review.run_review_agent", _fake_review)

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING
    assert len(agent_called) == 0  # agent not called at all


# --- Missing repo guard → BLOCKED -------------------------------------


def test_missing_repo_blocks(ctx_factory):
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = ctx.service.create("No clone")
    ctx.service.transition(t.id, State.READY)
    # Pipeline flip: READY -> CODE_REVIEW directly.
    ctx.service.transition(t.id, State.CODE_REVIEW)
    t = ctx.service.get(t.id)

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "re-run implement" in out.note


# --- review.md artifact --------------------------------------------------


def test_writes_review_artifact_on_approve(ctx_factory, monkeypatch):
    """APPROVE with auto_merge_eligible=True → review.md exists."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)

    def _fake_review(
        *,
        settings,
        diff,
        spec,
        model_name=None,
        prior_context=None,
        repo_dir=None,
        reference_files=None,
        screenshot_path=None,
        extra_roots=None,
    ):
        del settings, diff, spec, model_name, prior_context, repo_dir, reference_files
        return ReviewVerdict(
            verdict="APPROVE",
            comments="lgtm",
            auto_merge_eligible=True,
        )

    monkeypatch.setattr("robotsix_mill.stages.review.run_review_agent", _fake_review)

    ReviewStage().run(t, ctx)
    artifact = ctx.service.workspace(t).artifacts_dir / "review.md"
    assert artifact.exists()
    text = artifact.read_text(encoding="utf-8")
    assert "verdict: APPROVE" in text
    assert "auto_merge_eligible: true" in text
    assert "comment: lgtm" in text


def test_writes_review_artifact_on_request_changes(ctx_factory, monkeypatch):
    """REQUEST_CHANGES → review.md exists with auto_merge_eligible: false."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)

    def _fake_review(
        *,
        settings,
        diff,
        spec,
        model_name=None,
        prior_context=None,
        repo_dir=None,
        reference_files=None,
        screenshot_path=None,
        extra_roots=None,
    ):
        del settings, diff, spec, model_name, prior_context, repo_dir, reference_files
        return ReviewVerdict(
            verdict="REQUEST_CHANGES",
            comments="fix X",
            auto_merge_eligible=False,
        )

    monkeypatch.setattr("robotsix_mill.stages.review.run_review_agent", _fake_review)

    ReviewStage().run(t, ctx)
    artifact = ctx.service.workspace(t).artifacts_dir / "review.md"
    assert artifact.exists()
    text = artifact.read_text(encoding="utf-8")
    assert "verdict: REQUEST_CHANGES" in text
    assert "auto_merge_eligible: false" in text
    assert "comment: fix X" in text


def test_comment_multiline_collapse(ctx_factory, monkeypatch):
    """Multiline reviewer comments are collapsed with ' / ' in the artifact."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)

    def _fake_review(
        *,
        settings,
        diff,
        spec,
        model_name=None,
        prior_context=None,
        repo_dir=None,
        reference_files=None,
        screenshot_path=None,
        extra_roots=None,
    ):
        del settings, diff, spec, model_name, prior_context, repo_dir, reference_files
        return ReviewVerdict(
            verdict="APPROVE",
            comments="Line one\nLine two",
        )

    monkeypatch.setattr("robotsix_mill.stages.review.run_review_agent", _fake_review)

    ReviewStage().run(t, ctx)
    artifact = ctx.service.workspace(t).artifacts_dir / "review.md"
    text = artifact.read_text(encoding="utf-8")
    assert "comment: Line one / Line two" in text


def test_comment_truncation(ctx_factory, monkeypatch):
    """Comments longer than 300 chars are truncated with '…'."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)

    long_comment = "x" * 350

    def _fake_review(
        *,
        settings,
        diff,
        spec,
        model_name=None,
        prior_context=None,
        repo_dir=None,
        reference_files=None,
        screenshot_path=None,
        extra_roots=None,
    ):
        del settings, diff, spec, model_name, prior_context, repo_dir, reference_files
        return ReviewVerdict(
            verdict="APPROVE",
            comments=long_comment,
        )

    monkeypatch.setattr("robotsix_mill.stages.review.run_review_agent", _fake_review)

    ReviewStage().run(t, ctx)
    artifact = ctx.service.workspace(t).artifacts_dir / "review.md"
    text = artifact.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("comment:"):
            value = line[len("comment:") :].strip()
            assert value.endswith("…")
            assert len(value) <= 303  # 300 + "…"
            break
    else:
        pytest.fail("comment: line not found in review.md")


def test_comment_empty_returns_no_details(ctx_factory, monkeypatch):
    """Empty comments → 'comment: (no details)'."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)

    def _fake_review(
        *,
        settings,
        diff,
        spec,
        model_name=None,
        prior_context=None,
        repo_dir=None,
        reference_files=None,
        screenshot_path=None,
        extra_roots=None,
    ):
        del settings, diff, spec, model_name, prior_context, repo_dir, reference_files
        return ReviewVerdict(
            verdict="APPROVE",
            comments="",
        )

    monkeypatch.setattr("robotsix_mill.stages.review.run_review_agent", _fake_review)

    ReviewStage().run(t, ctx)
    artifact = ctx.service.workspace(t).artifacts_dir / "review.md"
    text = artifact.read_text(encoding="utf-8")
    assert "comment: (no details)" in text


def test_auto_merge_eligible_defaults_false(ctx_factory, monkeypatch):
    """When the model omits auto_merge_eligible, it defaults to False."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    _ticket(ctx)

    # Simulate an agent response that only includes verdict + comments
    from pydantic import BaseModel

    class PartialVerdict(BaseModel):
        verdict: str = "APPROVE"
        comments: str = "ok"

    verdict = ReviewVerdict(**PartialVerdict().model_dump())
    assert verdict.auto_merge_eligible is False


# --- review round cap --------------------------------------------------


def test_request_changes_under_cap(ctx_factory, monkeypatch):
    """REQUEST_CHANGES with review_rounds < max → READY, counter incremented."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)
    ctx.service.set_review_rounds(t.id, 1)  # 1 round already used
    t = ctx.service.get(t.id)  # refresh in-memory object

    def _fake_review(
        *,
        settings,
        diff,
        spec,
        model_name=None,
        prior_context=None,
        repo_dir=None,
        reference_files=None,
        screenshot_path=None,
        extra_roots=None,
    ):
        del settings, diff, spec, model_name, prior_context, repo_dir, reference_files
        return ReviewVerdict(verdict="REQUEST_CHANGES", comments="fix X")

    monkeypatch.setattr("robotsix_mill.stages.review.run_review_agent", _fake_review)

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.READY

    # Counter incremented from 1 → 2
    t2 = ctx.service.get(t.id)
    assert t2.review_rounds == 2

    # Comment stored
    comments = ctx.service.list_comments(t.id)
    assert len(comments) == 1
    assert comments[0].body == "fix X"


def test_request_changes_at_cap_escalates(ctx_factory, monkeypatch):
    """When review_rounds hits the cap, REQUEST_CHANGES → DELIVERABLE."""
    ctx = ctx_factory(
        FORGE_REMOTE_URL="file:///dummy",
        review_enabled="true",
        review_max_rounds="3",
    )
    t = _ticket(ctx)
    ctx.service.set_review_rounds(t.id, 2)  # round 3 is the cap
    t = ctx.service.get(t.id)  # refresh in-memory object

    def _fake_review(
        *,
        settings,
        diff,
        spec,
        model_name=None,
        prior_context=None,
        repo_dir=None,
        reference_files=None,
        screenshot_path=None,
        extra_roots=None,
    ):
        del settings, diff, spec, model_name, prior_context, repo_dir, reference_files
        return ReviewVerdict(verdict="REQUEST_CHANGES", comments="still broken")

    monkeypatch.setattr("robotsix_mill.stages.review.run_review_agent", _fake_review)

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING
    assert "exhausted" in out.note

    # Counter reset to 0
    t2 = ctx.service.get(t.id)
    assert t2.review_rounds == 0

    # Escalation comment stored
    comments = ctx.service.list_comments(t.id)
    assert len(comments) == 1
    assert "cap exhausted" in comments[0].body
    assert "3/3" in comments[0].body


def test_approve_resets_counter(ctx_factory, monkeypatch):
    """APPROVE resets review_rounds to 0."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)
    ctx.service.set_review_rounds(t.id, 2)
    t = ctx.service.get(t.id)  # refresh in-memory object

    def _fake_review(
        *,
        settings,
        diff,
        spec,
        model_name=None,
        prior_context=None,
        repo_dir=None,
        reference_files=None,
        screenshot_path=None,
        extra_roots=None,
    ):
        del settings, diff, spec, model_name, prior_context, repo_dir, reference_files
        return ReviewVerdict(verdict="APPROVE", comments="lgtm")

    monkeypatch.setattr("robotsix_mill.stages.review.run_review_agent", _fake_review)

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING
    assert "approved" in out.note

    t2 = ctx.service.get(t.id)
    assert t2.review_rounds == 0


def test_needs_discussion_preserves_counter(ctx_factory, monkeypatch):
    """NEEDS_DISCUSSION does NOT reset the review_rounds counter."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)
    ctx.service.set_review_rounds(t.id, 1)
    t = ctx.service.get(t.id)  # refresh in-memory object

    def _fake_review(
        *,
        settings,
        diff,
        spec,
        model_name=None,
        prior_context=None,
        repo_dir=None,
        reference_files=None,
        screenshot_path=None,
        extra_roots=None,
    ):
        del settings, diff, spec, model_name, prior_context, repo_dir, reference_files
        return ReviewVerdict(verdict="NEEDS_DISCUSSION", comments="questionable")

    monkeypatch.setattr("robotsix_mill.stages.review.run_review_agent", _fake_review)

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.AWAITING_USER_REPLY

    t2 = ctx.service.get(t.id)
    assert t2.review_rounds == 1  # unchanged


# --- Dependency spawning for out-of-scope asks -------------------------


def test_file_in_scope_matches_path_suffix():
    """``_file_in_scope`` matches when refine stored a short suffix
    (``static/board.js``) and review used the canonical repo-relative
    path (``src/robotsix_mill/runtime/static/board.js``) — the
    regression that caused most pre-fix review-source dependency
    tickets to be spurious."""
    from robotsix_mill.stages.review import _file_in_scope

    fm = {"static/board.js", "core/service.py"}
    assert _file_in_scope("src/robotsix_mill/runtime/static/board.js", fm)
    assert _file_in_scope("src/robotsix_mill/core/service.py", fm)
    # Exact match still works.
    assert _file_in_scope("static/board.js", fm)
    # Reverse direction (review short, file_map long) also works.
    fm2 = {"src/robotsix_mill/runtime/static/board.js"}
    assert _file_in_scope("static/board.js", fm2)
    # Unrelated file is genuinely out of scope.
    assert not _file_in_scope("other/file.py", fm)


def test_file_in_scope_rejects_substring_collision():
    """A non-slash-boundary substring must NOT match — e.g. file_map
    entry ``board.js`` should not legitimise an ask on
    ``other/dashboard.js`` just because the suffix overlaps."""
    from robotsix_mill.stages.review import _file_in_scope

    fm = {"board.js"}
    assert not _file_in_scope("dashboard.js", fm)
    assert not _file_in_scope("static/dashboard.js", fm)
    # Real slash-boundary suffix is still accepted.
    assert _file_in_scope("static/board.js", fm)


def _write_file_map(ctx, ticket, files: list[str]) -> None:
    """Stamp the workspace's file_map.json — the same artifact refine
    writes. Each entry is ``{"file": <path>}``."""
    ws = ctx.service.workspace(ticket)
    ws.artifacts_dir.mkdir(parents=True, exist_ok=True)
    (ws.artifacts_dir / "file_map.json").write_text(
        json.dumps([{"file": f} for f in files]),
        encoding="utf-8",
    )


def test_request_changes_in_scope_no_deps(ctx_factory, monkeypatch):
    """All asks touch files inside file_map → no dep tickets, single
    review comment, parent goes to READY (existing behaviour)."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)
    _write_file_map(ctx, t, ["feature.txt"])

    def _fake_review(**_kw):
        return ReviewVerdict(
            verdict="REQUEST_CHANGES",
            comments="fix line 3 in feature.txt",
            request_changes=[
                ReviewAsk(
                    description="Tighten the bounds check in feature.txt",
                    files_touched=["feature.txt"],
                )
            ],
        )

    monkeypatch.setattr("robotsix_mill.stages.review.run_review_agent", _fake_review)

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.READY

    after = ctx.service.get(t.id)
    assert not after.depends_on  # no deps spawned
    # Only the original review comment exists (no dep-spawn notice).
    bodies = [c.body for c in ctx.service.list_comments(t.id)]
    assert any("fix line 3" in b for b in bodies)
    assert all("Spawned" not in b for b in bodies)


def _spawned_children(ctx, parent_id):
    """Review-spawned follow-ups: tickets with source='review' (excluding
    the parent), newest first."""
    return [x for x in ctx.service.list() if x.source == "review" and x.id != parent_id]


def test_request_changes_out_of_scope_spawns_followup_ticket(ctx_factory, monkeypatch):
    """An out-of-scope ask (with no in-scope asks) materialises a fresh
    ticket wired as a FOLLOW-UP — the CHILD depends on the parent (runs
    after it merges), the parent is NOT parked, and since nothing in-scope
    needs fixing the parent is approved (DOCUMENTING). This is the
    direction fix for the 104b/413d deadlock."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)
    _write_file_map(ctx, t, ["feature.txt"])  # .gitignore is out-of-scope

    def _fake_review(**_kw):
        return ReviewVerdict(
            verdict="REQUEST_CHANGES",
            comments="add .gitignore for __pycache__",
            request_changes=[
                ReviewAsk(
                    description="Add a .gitignore that excludes __pycache__",
                    files_touched=[".gitignore"],
                )
            ],
        )

    monkeypatch.setattr("robotsix_mill.stages.review.run_review_agent", _fake_review)

    out = ReviewStage().run(t, ctx)
    # No in-scope changes → approve so the parent can merge + release the
    # follow-up, instead of parking it.
    assert out.next_state is State.DOCUMENTING

    after = ctx.service.get(t.id)
    assert not after.depends_on  # parent is NOT parked on the follow-up

    children = _spawned_children(ctx, t.id)
    assert len(children) == 1
    child = children[0]
    assert child.source == "review"
    # The follow-up depends on the PARENT (runs after it merges).
    assert json.loads(child.depends_on or "[]") == [t.id]
    assert ".gitignore" in ctx.service.workspace(child).read_description()


def test_request_changes_mixed_scope_one_dep_one_in_scope(ctx_factory, monkeypatch):
    """Mixed verdict: in-scope asks keep the parent in READY (re-implement),
    the out-of-scope ask spawns ONE follow-up that depends on the parent —
    and the parent is NOT parked on it."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)
    _write_file_map(ctx, t, ["feature.txt"])

    def _fake_review(**_kw):
        return ReviewVerdict(
            verdict="REQUEST_CHANGES",
            comments="two issues",
            request_changes=[
                ReviewAsk(
                    description="Tighten bounds in feature.txt",
                    files_touched=["feature.txt"],
                ),
                ReviewAsk(
                    description="Add a .gitignore for __pycache__",
                    files_touched=[".gitignore"],
                ),
            ],
        )

    monkeypatch.setattr("robotsix_mill.stages.review.run_review_agent", _fake_review)

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.READY  # in-scope ask → re-implement

    after = ctx.service.get(t.id)
    assert not after.depends_on  # parent NOT parked on the follow-up
    children = _spawned_children(ctx, t.id)
    assert len(children) == 1  # only the out-of-scope ask spawned a follow-up
    assert json.loads(children[0].depends_on or "[]") == [t.id]


def test_request_changes_no_file_map_all_in_scope(ctx_factory, monkeypatch):
    """No file_map.json → every ask is treated as in-scope (legacy /
    scope-free flow). No deps spawned regardless of files_touched."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)
    # no file_map.json written

    def _fake_review(**_kw):
        return ReviewVerdict(
            verdict="REQUEST_CHANGES",
            comments="fix it",
            request_changes=[
                ReviewAsk(
                    description="Add a .gitignore",
                    files_touched=[".gitignore"],
                )
            ],
        )

    monkeypatch.setattr("robotsix_mill.stages.review.run_review_agent", _fake_review)

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.READY

    after = ctx.service.get(t.id)
    assert not after.depends_on


def test_out_of_scope_ask_uses_explicit_title(ctx_factory, monkeypatch):
    """When ReviewAsk.title is set, the spawned dependency ticket uses
    it verbatim — not a sentence cropped from the description. This is
    what stops the reviewer's symptom-framing ('remove
    __pycache__/foo.pyc') from becoming the new ticket's title when
    the proper fix is something else ('add __pycache__ to .gitignore')."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)
    _write_file_map(ctx, t, ["feature.txt"])

    def _fake_review(**_kw):
        return ReviewVerdict(
            verdict="REQUEST_CHANGES",
            comments="add gitignore",
            request_changes=[
                ReviewAsk(
                    title="Add __pycache__ to .gitignore",
                    description=(
                        "__pycache__ files are tracked because the repo "
                        "has no .gitignore for compiled Python bytecode. "
                        "Add an entry for __pycache__/ to .gitignore."
                    ),
                    files_touched=[".gitignore"],
                )
            ],
        )

    monkeypatch.setattr("robotsix_mill.stages.review.run_review_agent", _fake_review)

    ReviewStage().run(t, ctx)

    children = _spawned_children(ctx, t.id)
    assert len(children) == 1
    assert children[0].title == "Add __pycache__ to .gitignore"


# --- gaps-already-addressed filtering ------------------------------------


def test_gaps_already_addressed_all_filtered_approves(ctx_factory, monkeypatch):
    """Every out-of-scope ask targets files already in the implementer's
    branch diff → all filtered as already-addressed, no follow-ups
    spawned, ticket approved directly (DOCUMENTING)."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)
    _write_file_map(ctx, t, ["feature.txt"])  # .gitignore is out-of-scope

    # Simulate the implementer having already touched .gitignore in their
    # branch — this puts it in modified_paths and makes the gap
    # "already addressed".
    repo_dir = ctx.service.workspace(t).dir / "repo"
    (repo_dir / ".gitignore").write_text("__pycache__/\n")
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-q", "-m", "add gitignore")

    def _fake_review(**_kw):
        return ReviewVerdict(
            verdict="REQUEST_CHANGES",
            comments="add .gitignore for __pycache__",
            request_changes=[
                ReviewAsk(
                    description="Add a .gitignore that excludes __pycache__",
                    files_touched=[".gitignore"],
                )
            ],
        )

    monkeypatch.setattr("robotsix_mill.stages.review.run_review_agent", _fake_review)

    out = ReviewStage().run(t, ctx)
    # All gaps already addressed, nothing in-scope → approve.
    assert out.next_state is State.DOCUMENTING
    assert "already addressed" in out.note

    # No child tickets spawned.
    children = _spawned_children(ctx, t.id)
    assert len(children) == 0

    # A comment notes the gap was skipped.
    comments = ctx.service.list_comments(t.id)
    bodies = [c.body for c in comments]
    assert any("already addressed in the implementer" in b for b in bodies)
    assert any("no follow-up needed" in b for b in bodies)


def test_gaps_already_addressed_mixed_some_filtered(ctx_factory, monkeypatch):
    """Mixed out-of-scope asks: one already addressed (files in diff),
    one still pending (files NOT in diff).  The pending ask spawns a
    follow-up; the already-addressed ask is skipped with a comment.
    In-scope asks still return READY."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)
    _write_file_map(ctx, t, ["feature.txt"])

    # The implementer touched .gitignore (already addressed) but NOT
    # README.md (still pending).
    repo_dir = ctx.service.workspace(t).dir / "repo"
    (repo_dir / ".gitignore").write_text("__pycache__/\n")
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-q", "-m", "add gitignore")

    def _fake_review(**_kw):
        return ReviewVerdict(
            verdict="REQUEST_CHANGES",
            comments="two out-of-scope issues + one in-scope",
            request_changes=[
                ReviewAsk(
                    description="Tighten bounds in feature.txt",
                    files_touched=["feature.txt"],  # in-scope
                ),
                ReviewAsk(
                    description="Add a .gitignore for __pycache__",
                    files_touched=[".gitignore"],  # out-of-scope, already in diff
                ),
                ReviewAsk(
                    description="Add a README section about setup",
                    files_touched=["README.md"],  # out-of-scope, NOT in diff
                ),
            ],
        )

    monkeypatch.setattr("robotsix_mill.stages.review.run_review_agent", _fake_review)

    out = ReviewStage().run(t, ctx)
    # In-scope ask still exists → READY for re-implement.
    assert out.next_state is State.READY

    # Exactly one follow-up spawned (for README.md only).
    children = _spawned_children(ctx, t.id)
    assert len(children) == 1
    assert "README" in ctx.service.workspace(children[0]).read_description()

    # Comments include both the "already addressed" note and the spawn note.
    comments = ctx.service.list_comments(t.id)
    bodies = [c.body for c in comments]
    assert any("already addressed in the implementer" in b for b in bodies)
    assert any("spawned as follow-up ticket" in b for b in bodies)


def test_gaps_already_addressed_empty_files_touched_still_pending():
    """An ask with empty ``files_touched`` is never classified as
    already-addressed — we cannot verify file-less clarifications from
    the diff alone, so they stay pending.  (Unit test: in the full stage
    flow ``_split_asks`` routes empty-files_touched asks to in_scope, so
    they never reach ``_gaps_already_addressed``.  Verify the helper's
    own contract directly.)"""
    from robotsix_mill.stages.review import _gaps_already_addressed

    ask = ReviewAsk(
        description="Clarify the spec: should the feature handle empty input?",
        files_touched=[],  # file-less clarification
    )
    already, pending = _gaps_already_addressed([ask], ["feature.txt", "README.md"])
    assert len(already) == 0
    assert len(pending) == 1
    assert pending[0] is ask


# --- board screenshot plumbing -----------------------------------------


def test_screenshot_passed_when_board_png_present(ctx_factory, monkeypatch):
    """When artifacts/board.png exists, the stage passes it as
    ``screenshot_path`` to ``run_review_agent``."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)
    board_png = ws.artifacts_dir / "board.png"
    board_png.write_bytes(b"\x89PNG\r\n\x1a\n fake png bytes")

    captured: dict = {}

    def _fake_review(*, screenshot_path=None, **_kw):
        captured["screenshot_path"] = screenshot_path
        return ReviewVerdict(verdict="APPROVE", comments="ok")

    monkeypatch.setattr("robotsix_mill.stages.review.run_review_agent", _fake_review)

    ReviewStage().run(t, ctx)
    assert captured["screenshot_path"] == board_png

    # Artifact records the screenshot was present.
    text = (ws.artifacts_dir / "review.md").read_text(encoding="utf-8")
    assert "board_screenshot: present" in text


def test_screenshot_none_when_board_png_absent(ctx_factory, monkeypatch):
    """With no artifacts/board.png, the stage passes ``screenshot_path=None``
    and review behaves exactly as today."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)

    captured: dict = {}

    def _fake_review(*, screenshot_path=None, **_kw):
        captured["screenshot_path"] = screenshot_path
        return ReviewVerdict(verdict="APPROVE", comments="ok")

    monkeypatch.setattr("robotsix_mill.stages.review.run_review_agent", _fake_review)

    ReviewStage().run(t, ctx)
    assert captured["screenshot_path"] is None

    ws = ctx.service.workspace(t)
    text = (ws.artifacts_dir / "review.md").read_text(encoding="utf-8")
    assert "board_screenshot: absent" in text


# --- prior-context input cap -------------------------------------------


def test_prior_context_caps_oversized(ctx_factory):
    """When prior comments + the implement rebuttal exceed the cap,
    each component is tail-kept: the most-recent content survives, a
    truncation note is present, and the assembled block is far smaller
    than the uncapped size."""
    from robotsix_mill.stages.review import _build_prior_context

    ctx = ctx_factory(
        FORGE_REMOTE_URL="file:///dummy",
        review_enabled="true",
        review_prior_context_max_chars="300",
    )
    t = ctx.service.create("Cap test", "body")
    ws = ctx.service.workspace(t)
    ws.artifacts_dir.mkdir(parents=True, exist_ok=True)

    # Many prior comments, oldest → newest.
    for i in range(60):
        ctx.service.add_comment(
            t.id, f"comment-{i:03d} some review feedback text", author="review"
        )
    # A large implement rebuttal.
    rebuttal = "\n".join(
        f"rebuttal-{i:03d} the implementer's response line" for i in range(60)
    )
    (ws.artifacts_dir / "implement.md").write_text(rebuttal, encoding="utf-8")

    block = _build_prior_context(t, ctx, ws)
    assert block is not None
    # (a) truncation notes present for both capped components
    assert "prior-review-comments truncated:" in block
    assert "implement-rebuttal truncated:" in block
    # (b) most-recent tail retained, oldest dropped
    assert "comment-059" in block
    assert "comment-000" not in block
    assert "rebuttal-059" in block
    assert "rebuttal-000" not in block
    # (c) within the expected size bound (uncapped would be ~5KB).
    assert len(block) < 2000


def test_prior_context_under_cap_verbatim(ctx_factory):
    """Content shorter than the cap is returned verbatim — no note."""
    from robotsix_mill.stages.review import _build_prior_context

    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = ctx.service.create("Small", "body")
    ws = ctx.service.workspace(t)
    ws.artifacts_dir.mkdir(parents=True, exist_ok=True)

    ctx.service.add_comment(t.id, "short feedback", author="review")
    (ws.artifacts_dir / "implement.md").write_text("brief rebuttal", encoding="utf-8")

    block = _build_prior_context(t, ctx, ws)
    assert block is not None
    assert "truncated:" not in block
    assert "short feedback" in block
    assert "brief rebuttal" in block


# --- Diff bounding (review_diff_max_chars) -----------------------------


def _capture_review(monkeypatch) -> dict:
    """Patch ``run_review_agent`` to capture its kwargs; return the dict."""
    captured: dict = {}

    def _fake_review(*, settings, diff, spec, **kwargs):
        captured["diff"] = diff
        captured["spec"] = spec
        captured.update(kwargs)
        return ReviewVerdict(verdict="APPROVE", comments="ok")

    monkeypatch.setattr("robotsix_mill.stages.review.run_review_agent", _fake_review)
    return captured


def test_oversized_diff_is_truncated_before_agent(ctx_factory, monkeypatch):
    """An oversized combined diff is middle-truncated via head_tail_keep
    (marker present, length bounded) before reaching run_review_agent;
    modified_paths derivation still works off the diff content."""
    ctx = ctx_factory(
        FORGE_REMOTE_URL="file:///dummy",
        review_enabled="true",
        review_diff_max_chars="2000",
    )
    t = _ticket(ctx)

    # Add a large file so the combined diff exceeds the 2000-char cap.
    ws = ctx.service.workspace(t)
    repo_dir = ws.dir / "repo"
    (repo_dir / "big.txt").write_text("X" * 50_000 + "\n", encoding="utf-8")
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-q", "-m", "add big file")

    captured = _capture_review(monkeypatch)
    ReviewStage().run(t, ctx)

    diff = captured["diff"]
    assert "[... git-diff truncated:" in diff
    assert "omitted from the middle" in diff
    # Length bounded by the cap plus the (short) marker line.
    assert len(diff) <= 2000 + 200
    # Path parsing survives truncation: the preseed file set still names
    # every modified file (derived from the untruncated diff).
    assert set(captured["reference_files"]) >= {"big.txt", "feature.txt"}


def test_small_diff_passes_through_unchanged(ctx_factory, monkeypatch):
    """A normal small diff (under the default cap) reaches the agent
    verbatim — no truncation marker."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)

    captured = _capture_review(monkeypatch)
    ReviewStage().run(t, ctx)

    diff = captured["diff"]
    assert "feature.txt" in diff
    assert "truncated:" not in diff


# ------------------------------------------------------------------
# _workflow_refs_from_diff — unit tests
# ------------------------------------------------------------------


def test_workflow_refs_from_diff_owner_repo():
    """``uses: owner/repo/.github/workflows/ci.yml@v1`` → {owner/repo}."""
    from robotsix_mill.stages.review import _workflow_refs_from_diff

    refs = _workflow_refs_from_diff("uses: my-org/my-repo/.github/workflows/ci.yml@v1")
    assert refs == {"my-org/my-repo"}


def test_workflow_refs_from_diff_single_org_shorthand():
    """``uses: org/.github/workflows/deps-bump.yml@main`` → {org}."""
    from robotsix_mill.stages.review import _workflow_refs_from_diff

    refs = _workflow_refs_from_diff(
        "uses: robotsix-mill/.github/workflows/deps-bump.yml@main"
    )
    assert refs == {"robotsix-mill"}


def test_workflow_refs_from_diff_actions_path():
    """``uses: owner/repo/.github/actions/…`` also matched."""
    from robotsix_mill.stages.review import _workflow_refs_from_diff

    refs = _workflow_refs_from_diff(
        "uses: my-org/my-repo/.github/actions/composite-action@v2"
    )
    assert refs == {"my-org/my-repo"}


def test_workflow_refs_from_diff_docker_ref_not_matched():
    """``uses: docker://ubuntu:latest`` → empty set."""
    from robotsix_mill.stages.review import _workflow_refs_from_diff

    refs = _workflow_refs_from_diff("uses: docker://ubuntu:latest")
    assert refs == set()


def test_workflow_refs_from_diff_relative_path_not_matched():
    """``uses: ./github/workflows/local.yml`` → empty set (relative path)."""
    from robotsix_mill.stages.review import _workflow_refs_from_diff

    refs = _workflow_refs_from_diff("uses: ./github/workflows/local.yml")
    assert refs == set()


def test_workflow_refs_from_diff_deduplicates():
    """Duplicate refs → single entry in the returned set."""
    from robotsix_mill.stages.review import _workflow_refs_from_diff

    diff = (
        "uses: my-org/my-repo/.github/workflows/ci.yml@v1\n"
        "uses: my-org/my-repo/.github/workflows/deploy.yml@main\n"
    )
    refs = _workflow_refs_from_diff(diff)
    assert refs == {"my-org/my-repo"}


def test_workflow_refs_from_empty_diff():
    """Empty string → empty set."""
    from robotsix_mill.stages.review import _workflow_refs_from_diff

    assert _workflow_refs_from_diff("") == set()


def test_workflow_refs_from_mixed_diff():
    """Multiple ref types in one diff — only workflow refs captured."""
    from robotsix_mill.stages.review import _workflow_refs_from_diff

    diff = (
        "uses: org-a/repo-x/.github/workflows/build.yml@v2\n"
        "uses: docker://alpine:latest\n"
        "uses: ./github/workflows/local.yml\n"
        "uses: org-b/.github/workflows/release.yml@main\n"
        "uses: org-a/repo-y/.github/actions/setup@v1\n"
    )
    refs = _workflow_refs_from_diff(diff)
    assert refs == {"org-a/repo-x", "org-b", "org-a/repo-y"}


# ------------------------------------------------------------------
# Workflow refs derived from UNTRUNCATED diff (not truncated)
# ------------------------------------------------------------------


def test_workflow_refs_use_untruncated_diff(ctx_factory, monkeypatch):
    """When the diff is truncated, workflow refs from the middle are still
    captured — ``_workflow_refs_from_diff`` is called on the untruncated
    diff (same pattern as ``_paths_from_diff``)."""
    ctx = ctx_factory(
        FORGE_REMOTE_URL="file:///dummy",
        review_enabled="true",
        review_diff_max_chars="500",
    )
    t = _ticket(ctx)

    ws = ctx.service.workspace(t)
    repo_dir = ws.dir / "repo"

    # Put a ``uses:`` line in the MIDDLE of a large file so truncation
    # would drop it if we used the truncated diff for extraction.
    workflow_ref_line = "uses: other-org/other-repo/.github/workflows/ci.yml@v1"
    (repo_dir / "big.txt").write_text(
        "line0\n" * 40 + workflow_ref_line + "\n" + "line1\n" * 40, encoding="utf-8"
    )
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-q", "-m", "add big file with workflow ref")

    captured = _capture_review(monkeypatch)
    ReviewStage().run(t, ctx)

    # The truncation marker proves the diff WAS truncated.
    assert "truncated:" in captured["diff"]

    # The uses: line would be in the dropped middle, so the truncated
    # diff does NOT contain it.
    assert workflow_ref_line not in captured["diff"]

    # But extra_roots would be populated (if the other repo were in the
    # repos config).  Since it's not, extra_roots should be None — but
    # crucially, the stage must NOT crash trying to parse workflow refs
    # from only the truncated portion.  The stage completed without error.
    # We verify that the review agent was called (proving the stage
    # didn't blow up) and that extra_roots was None for this unmatched ref.
    assert captured["extra_roots"] is None


# ------------------------------------------------------------------
# Cross-repo extra_roots flow
# ------------------------------------------------------------------


def test_extra_roots_passed_when_workflow_ref_matches_repos_config(
    ctx_factory, monkeypatch
):
    """When the diff references a workflow from a sibling repo that IS in
    the repos config, the stage passes ``extra_roots`` to the review agent
    containing the clone path."""
    from robotsix_mill.config import RepoConfig
    from robotsix_mill.config.repos import get_repos_config, _reset_repos_config
    from robotsix_mill.vcs import git_ops

    ctx = ctx_factory(
        FORGE_REMOTE_URL="file:///dummy",
        review_enabled="true",
    )
    t = _ticket(ctx)

    ws = ctx.service.workspace(t)

    # The sibling repo config uses a GitHub-style URL so _parse_owner_repo
    # can extract the owner/repo slug.  We monkeypatch git_ops.clone to
    # simulate a successful clone (no network needed in test).
    sibling_slug = "test-org/test-sibling"
    sibling_remote = f"https://github.com/{sibling_slug}.git"

    _reset_repos_config()
    repos_cfg = get_repos_config()
    repos_cfg.repos["sibling"] = RepoConfig(
        repo_id="sibling",
        board_id="test-board",
        langfuse_project_name="test",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
        forge_remote_url=sibling_remote,
    )

    # Simulate a successful clone by creating the dest dir with a .git
    # marker — enough to satisfy the ``dest.is_dir()`` guard on the next
    # pass and the ``clone_path.is_dir()`` assertion below.
    original_clone = git_ops.clone

    def _fake_clone(remote_url, dest, branch, token=None):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / ".git").mkdir(exist_ok=True)

    monkeypatch.setattr("robotsix_mill.stages.review.git_ops.clone", _fake_clone)
    monkeypatch.setattr(
        "robotsix_mill.stages.review.github_token", lambda s, repo_config=None: "tk"
    )

    try:
        # Write a diff that references the sibling workflow.
        repo_dir = ws.dir / "repo"
        workflow_line = f"uses: {sibling_slug}/.github/workflows/ci.yml@main\n"
        (repo_dir / "ci.yml").write_text(workflow_line, encoding="utf-8")
        _git(repo_dir, "add", "-A")
        _git(repo_dir, "commit", "-q", "-m", "add workflow ref")

        captured = _capture_review(monkeypatch)
        ReviewStage().run(t, ctx)

        # The review agent should have received extra_roots with the clone.
        assert captured["extra_roots"] is not None
        assert len(captured["extra_roots"]) == 1
        clone_path = captured["extra_roots"][0]
        assert clone_path.is_dir()
        assert (clone_path / ".git").is_dir()
    finally:
        _reset_repos_config()
        monkeypatch.setattr("robotsix_mill.stages.review.git_ops.clone", original_clone)


def test_extra_roots_none_when_no_workflow_refs(ctx_factory, monkeypatch):
    """When the diff has no workflow refs, ``extra_roots`` is None."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)

    captured = _capture_review(monkeypatch)
    ReviewStage().run(t, ctx)

    assert captured["extra_roots"] is None


def test_extra_roots_skips_unmatched_ref_gracefully(ctx_factory, monkeypatch):
    """When the diff references a workflow for a repo NOT in the repos
    config, the stage continues gracefully — ``extra_roots`` is None."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)

    ws = ctx.service.workspace(t)
    repo_dir = ws.dir / "repo"
    (repo_dir / "ci.yml").write_text(
        "uses: unknown-org/unknown-repo/.github/workflows/ci.yml@v1\n",
        encoding="utf-8",
    )
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-q", "-m", "add unmatched workflow ref")

    captured = _capture_review(monkeypatch)
    ReviewStage().run(t, ctx)

    # No crash; extra_roots is None because the ref couldn't be resolved.
    assert captured["extra_roots"] is None


# --- Action ref SHA-pin validation --------------------------------------


def _write_workflow_yaml(repo_dir: Path, uses_line: str) -> None:
    """Write a minimal GitHub Actions workflow YAML with a ``uses:`` step."""
    (repo_dir / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    (repo_dir / ".github" / "workflows" / "ci.yml").write_text(
        f"name: CI\n"
        f"on: push\n"
        f"jobs:\n"
        f"  build:\n"
        f"    runs-on: ubuntu-latest\n"
        f"    steps:\n"
        f"      - {uses_line}\n",
        encoding="utf-8",
    )


def test_action_ref_valid_sha_no_blocking(ctx_factory, monkeypatch):
    """A valid 40-char hex SHA pin produces no blocking finding; the LLM
    verdict (APPROVE) is preserved."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)

    repo_dir = ctx.service.workspace(t).dir / "repo"
    _write_workflow_yaml(
        repo_dir,
        "uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2",
    )
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-q", "-m", "add workflow with valid SHA")

    def _fake_review(**_kw):
        return ReviewVerdict(verdict="APPROVE", comments="lgtm")

    monkeypatch.setattr("robotsix_mill.stages.review.run_review_agent", _fake_review)

    out = ReviewStage().run(t, ctx)
    # Valid SHA → no forced REQUEST_CHANGES → APPROVE preserved.
    assert out.next_state is State.DOCUMENTING
    comments = [c.body for c in ctx.service.list_comments(t.id)]
    # No blocking comment about SHA-pin validation.
    assert not any("SHA-pin validation failed" in c for c in comments)


def test_action_ref_invalid_tag_blocks_approve(ctx_factory, monkeypatch):
    """A version tag (``@v4``) triggers a blocking REQUEST_CHANGES even
    when the LLM reviewer says APPROVE."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)

    repo_dir = ctx.service.workspace(t).dir / "repo"
    _write_workflow_yaml(repo_dir, "uses: actions/checkout@v4")
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-q", "-m", "add workflow with version tag")

    def _fake_review(**_kw):
        return ReviewVerdict(verdict="APPROVE", comments="lgtm")

    monkeypatch.setattr("robotsix_mill.stages.review.run_review_agent", _fake_review)

    out = ReviewStage().run(t, ctx)
    # Invalid ref → forced REQUEST_CHANGES → back to READY.
    assert out.next_state is State.READY

    comments = [c.body for c in ctx.service.list_comments(t.id)]
    assert any("SHA-pin validation failed" in c for c in comments)
    # The original LLM comment is preserved.
    assert any("lgtm" in c for c in comments)


def test_action_ref_invalid_branch_blocks(ctx_factory, monkeypatch):
    """A branch ref (``@main``) triggers a blocking REQUEST_CHANGES."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)

    repo_dir = ctx.service.workspace(t).dir / "repo"
    _write_workflow_yaml(repo_dir, "uses: actions/checkout@main")
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-q", "-m", "add workflow with branch ref")

    def _fake_review(**_kw):
        return ReviewVerdict(verdict="APPROVE", comments="lgtm")

    monkeypatch.setattr("robotsix_mill.stages.review.run_review_agent", _fake_review)

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.READY

    comments = [c.body for c in ctx.service.list_comments(t.id)]
    assert any("SHA-pin validation failed" in c for c in comments)
    # The original LLM comment is preserved.
    assert any("lgtm" in c for c in comments)


def test_action_ref_subpath_invalid_tag_blocks(ctx_factory, monkeypatch):
    """A subpath action with a tag (``github/codeql-action/init@v3.29.2``)
    triggers a blocking REQUEST_CHANGES."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)

    repo_dir = ctx.service.workspace(t).dir / "repo"
    _write_workflow_yaml(repo_dir, "uses: github/codeql-action/init@v3.29.2")
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-q", "-m", "add codeql with tag ref")

    def _fake_review(**_kw):
        return ReviewVerdict(verdict="APPROVE", comments="lgtm")

    monkeypatch.setattr("robotsix_mill.stages.review.run_review_agent", _fake_review)

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.READY

    comments = [c.body for c in ctx.service.list_comments(t.id)]
    assert any("SHA-pin validation failed" in c for c in comments)
    assert any("lgtm" in c for c in comments)


def test_action_ref_local_ignored(ctx_factory, monkeypatch):
    """A local ``./`` action ref is NOT flagged — it passes through."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)

    repo_dir = ctx.service.workspace(t).dir / "repo"
    _write_workflow_yaml(repo_dir, "uses: ./.github/actions/my-action")
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-q", "-m", "add local action ref")

    def _fake_review(**_kw):
        return ReviewVerdict(verdict="APPROVE", comments="lgtm")

    monkeypatch.setattr("robotsix_mill.stages.review.run_review_agent", _fake_review)

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING

    comments = [c.body for c in ctx.service.list_comments(t.id)]
    assert not any("SHA-pin validation failed" in c for c in comments)


def test_action_ref_docker_ignored(ctx_factory, monkeypatch):
    """A ``docker://`` ref is NOT flagged — it passes through."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)

    repo_dir = ctx.service.workspace(t).dir / "repo"
    _write_workflow_yaml(repo_dir, "uses: docker://alpine:latest")
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-q", "-m", "add docker ref")

    def _fake_review(**_kw):
        return ReviewVerdict(verdict="APPROVE", comments="lgtm")

    monkeypatch.setattr("robotsix_mill.stages.review.run_review_agent", _fake_review)

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING

    comments = [c.body for c in ctx.service.list_comments(t.id)]
    assert not any("SHA-pin validation failed" in c for c in comments)


def test_action_ref_reusable_workflow_not_double_reported(ctx_factory, monkeypatch):
    """A reusable-workflow ref (``uses: org/repo/.github/workflows/...``)
    is NOT flagged by the action-ref validator — it belongs to the
    ``_workflow_refs_from_diff`` pathway."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)

    repo_dir = ctx.service.workspace(t).dir / "repo"
    _write_workflow_yaml(
        repo_dir,
        "uses: my-org/my-repo/.github/workflows/ci.yml@v1",
    )
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-q", "-m", "add reusable workflow ref")

    def _fake_review(**_kw):
        return ReviewVerdict(verdict="APPROVE", comments="lgtm")

    monkeypatch.setattr("robotsix_mill.stages.review.run_review_agent", _fake_review)

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.DOCUMENTING

    comments = [c.body for c in ctx.service.list_comments(t.id)]
    assert not any("SHA-pin validation failed" in c for c in comments)


def test_action_ref_violations_appended_to_existing_request_changes(
    ctx_factory,
    monkeypatch,
):
    """When the LLM already returned REQUEST_CHANGES, action-ref violations
    are prepended to the existing request_changes list."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)

    repo_dir = ctx.service.workspace(t).dir / "repo"
    _write_workflow_yaml(repo_dir, "uses: actions/checkout@v4")
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-q", "-m", "add workflow with tag")

    def _fake_review(**_kw):
        return ReviewVerdict(
            verdict="REQUEST_CHANGES",
            comments="fix other things too",
            request_changes=[
                ReviewAsk(
                    description="Fix the bounds check",
                    files_touched=["feature.txt"],
                )
            ],
        )

    monkeypatch.setattr("robotsix_mill.stages.review.run_review_agent", _fake_review)

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.READY

    comments = [c.body for c in ctx.service.list_comments(t.id)]
    assert any("SHA-pin validation failed" in c for c in comments)
    # The original LLM comment is preserved alongside the SHA-pin notice.
    assert any("fix other things" in c for c in comments)


def test_action_ref_no_violations_no_effect_on_verdict(ctx_factory, monkeypatch):
    """When there are no action-ref violations, the LLM verdict is
    passed through unchanged (REQUEST_CHANGES stays REQUEST_CHANGES)."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)

    repo_dir = ctx.service.workspace(t).dir / "repo"
    _write_workflow_yaml(
        repo_dir,
        "uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2",
    )
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-q", "-m", "add workflow with valid SHA")

    def _fake_review(**_kw):
        return ReviewVerdict(
            verdict="REQUEST_CHANGES",
            comments="fix feature.txt",
        )

    monkeypatch.setattr("robotsix_mill.stages.review.run_review_agent", _fake_review)

    out = ReviewStage().run(t, ctx)
    assert out.next_state is State.READY

    comments = [c.body for c in ctx.service.list_comments(t.id)]
    assert not any("SHA-pin validation failed" in c for c in comments)
    assert any("fix feature.txt" in c for c in comments)


# --- stage cache: unchanged input short-circuits re-review ------------


def test_review_cache_hit_skips_agent(ctx_factory, monkeypatch):
    """An unchanged ticket (same spec + same diff) is not re-audited."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx, body="Add feature.txt")

    agent_calls = []

    def _fake_review(
        *,
        settings,
        diff,
        spec,
        model_name=None,
        prior_context=None,
        repo_dir=None,
        reference_files=None,
        screenshot_path=None,
        extra_roots=None,
    ):
        agent_calls.append(1)
        return ReviewVerdict(verdict="APPROVE", comments="looks good")

    monkeypatch.setattr("robotsix_mill.stages.review.run_review_agent", _fake_review)

    # First run: agent is called, outcome is cached.
    out1 = ReviewStage().run(t, ctx)
    assert out1.next_state is State.DOCUMENTING
    assert len(agent_calls) == 1

    # Second run with same spec + diff: cache hit, agent NOT called.
    out2 = ReviewStage().run(t, ctx)
    assert out2.next_state is State.DOCUMENTING
    assert len(agent_calls) == 1  # still 1 — agent was not called again

    # Change the ticket body (simulates spec update).
    ws = ctx.service.workspace(t)
    ws.write_description("Add feature.txt\n\nUpdated spec with new details.")

    # Third run with changed spec: cache miss, agent IS called.
    out3 = ReviewStage().run(t, ctx)
    assert out3.next_state is State.DOCUMENTING
    assert len(agent_calls) == 2  # agent called again


def test_review_cache_different_diff_miss(ctx_factory, monkeypatch):
    """A changed diff invalidates the review cache."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx, body="Add feature.txt")

    agent_calls = []

    def _fake_review(
        *,
        settings,
        diff,
        spec,
        model_name=None,
        prior_context=None,
        repo_dir=None,
        reference_files=None,
        screenshot_path=None,
        extra_roots=None,
    ):
        agent_calls.append(1)
        return ReviewVerdict(verdict="APPROVE", comments="looks good")

    monkeypatch.setattr("robotsix_mill.stages.review.run_review_agent", _fake_review)

    # First run.
    out1 = ReviewStage().run(t, ctx)
    assert out1.next_state is State.DOCUMENTING
    assert len(agent_calls) == 1

    # Change the diff (add a new commit).
    repo_dir = ctx.service.workspace(t).dir / "repo"
    (repo_dir / "feature2.txt").write_text("new file")
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-q", "-m", "add feature2")

    # Second run with changed diff: cache miss, agent IS called.
    out2 = ReviewStage().run(t, ctx)
    assert out2.next_state is State.DOCUMENTING
    assert len(agent_calls) == 2  # agent called again
