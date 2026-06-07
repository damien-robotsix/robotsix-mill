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
