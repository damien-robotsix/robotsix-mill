"""Tests for the document stage and doc agent."""

import subprocess
from pathlib import Path

import pytest

from robotsix_mill.agents.documenting import DocClassifierResult, DocResult
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.document import DocumentStage


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


def _git_log(cwd) -> str:
    """Return the git log as a string for assertion."""
    return subprocess.run(
        ["git", "-C", str(cwd), "log", "--oneline"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def ctx_factory(tmp_path):
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
    ctx.service.transition(t.id, State.DOCUMENTING)
    return ctx.service.get(t.id)


# --- user-facing diff → doc edits + commit ----------------------------


def test_user_facing_commits_and_progresses(ctx_factory, monkeypatch):
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"

    step_events = []
    orig_add_step = ctx.service.add_step_event

    def _spy_add_step_event(ticket_id, note):
        step_events.append(note)
        return orig_add_step(ticket_id, note)

    monkeypatch.setattr(ctx.service, "add_step_event", _spy_add_step_event)

    def _fake_doc(
        self,
        *,
        settings,
        repo_dir,
        diff,
        spec,
        extra_roots=None,
        board_id="",
        reference_files=None,
    ):
        del self, settings, diff, spec
        # Simulate agent writing a doc file as a side effect.
        (Path(repo_dir) / "README.md").write_text("# Updated README\n")
        return DocResult(user_facing=True, summary="updated README")

    monkeypatch.setattr(DocumentStage, "_run_doc_agent", _fake_doc)

    out = DocumentStage().run(t, ctx)
    assert out.next_state is State.DELIVERABLE
    assert out.note == "updated README"

    # Verify the doc file was written.
    assert (repo_dir / "README.md").read_text() == "# Updated README\n"

    # Verify a commit exists with the doc prefix.
    log = _git_log(repo_dir)
    assert "mill(docs):" in log

    # The guardrail did NOT fire — edits were applied.
    assert not any("recommendation-only" in note for note in step_events)


# --- internal-only diff → no-op ---------------------------------------


def test_internal_skips_commit(ctx_factory, monkeypatch):
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"

    # Count commits before the stage runs.
    commits_before = _git_log(repo_dir).count("\n") + 1

    def _fake_doc(
        self,
        *,
        settings,
        repo_dir,
        diff,
        spec,
        extra_roots=None,
        board_id="",
        reference_files=None,
    ):
        del self, settings, diff, spec
        return DocResult(
            user_facing=False,
            summary="no user-facing changes (internal-only)",
        )

    monkeypatch.setattr(DocumentStage, "_run_doc_agent", _fake_doc)

    out = DocumentStage().run(t, ctx)
    assert out.next_state is State.DELIVERABLE
    assert out.note == "no user-facing changes (internal-only)"

    # No new commits.
    commits_after = _git_log(repo_dir).count("\n") + 1
    assert commits_after == commits_before

    # README was not modified (still the seed content).
    assert (repo_dir / "README.md").read_text() == "seed\n"


# --- user-facing=True but no changes → no commit ----------------------


def test_user_facing_no_changes_skips_commit(ctx_factory, monkeypatch):
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"

    commits_before = _git_log(repo_dir).count("\n") + 1

    step_events = []
    orig_add_step = ctx.service.add_step_event

    def _spy_add_step_event(ticket_id, note):
        step_events.append(note)
        return orig_add_step(ticket_id, note)

    monkeypatch.setattr(ctx.service, "add_step_event", _spy_add_step_event)

    def _fake_doc(
        self,
        *,
        settings,
        repo_dir,
        diff,
        spec,
        extra_roots=None,
        board_id="",
        reference_files=None,
    ):
        del self, settings, repo_dir, diff, spec
        # Agent claims user-facing but writes nothing.
        return DocResult(user_facing=True, summary="updated README")

    monkeypatch.setattr(DocumentStage, "_run_doc_agent", _fake_doc)

    out = DocumentStage().run(t, ctx)
    assert out.next_state is State.DELIVERABLE
    assert out.note == "updated README"

    # No new commits — agent claimed user-facing but wrote nothing.
    commits_after = _git_log(repo_dir).count("\n") + 1
    assert commits_after == commits_before

    # The non-blocking guardrail fired: a step event flags the
    # recommendation-only doc deliverable.
    assert any("recommendation-only" in note for note in step_events)


# --- empty diff → pass-through without agent --------------------------


def test_empty_diff_skips_agent(ctx_factory, monkeypatch):
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)

    # Remove the commit so diff is empty.
    repo_dir = ctx.service.workspace(t).dir / "repo"
    _git(repo_dir, "reset", "--soft", "HEAD~1")

    agent_called = []

    def _fake_doc(
        self,
        *,
        settings,
        repo_dir,
        diff,
        spec,
        extra_roots=None,
        board_id="",
        reference_files=None,
    ):
        agent_called.append(1)
        return DocResult(user_facing=False, summary="")

    monkeypatch.setattr(DocumentStage, "_run_doc_agent", _fake_doc)

    out = DocumentStage().run(t, ctx)
    assert out.next_state is State.DELIVERABLE
    assert len(agent_called) == 0  # agent not called at all


# --- missing clone → BLOCKED ------------------------------------------


def test_missing_clone_blocks(ctx_factory):
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = ctx.service.create("No clone")
    ctx.service.transition(t.id, State.READY)
    ctx.service.transition(t.id, State.DOCUMENTING)
    t = ctx.service.get(t.id)

    out = DocumentStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "re-run implement" in out.note


# --- agent exception → warn-and-pass ----------------------------------


def test_agent_exception_warns_and_passes(ctx_factory, monkeypatch):
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)

    def _fake_doc(
        self,
        *,
        settings,
        repo_dir,
        diff,
        spec,
        extra_roots=None,
        board_id="",
        reference_files=None,
    ):
        del self, settings, repo_dir, diff, spec
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(DocumentStage, "_run_doc_agent", _fake_doc)

    out = DocumentStage().run(t, ctx)
    assert out.next_state is State.DELIVERABLE  # not BLOCKED
    assert "doc agent failed (non-blocking)" in out.note


# --- agent exception with real error in note ---------------------------


def test_agent_exception_contains_real_error(ctx_factory, monkeypatch):
    """When doc agent fails, the note and notification carry the actual
    exception type and message — NOT a heuristic hint about deps."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"

    # pyproject.toml with deps, uv-sources, AND a uv.lock.
    # The old heuristic would have emitted hint strings here;
    # the new code ignores them and reports the real exception.
    (repo_dir / "pyproject.toml").write_text(
        "[project]\n"
        'name = "x"\n'
        'dependencies = ["requests"]\n'
        "\n"
        "[tool.uv.sources]\n"
        "x = { git = 'https://github.com/org/x' }\n",
        encoding="utf-8",
    )
    (repo_dir / "uv.lock").write_text("version = 1\n", encoding="utf-8")

    notifications = []

    def _spy_notification(ticket, dst, note, settings):
        notifications.append((dst, note))

    monkeypatch.setattr(
        "robotsix_mill.stages.document.send_notification",
        _spy_notification,
    )

    def _fake_doc(
        self,
        *,
        settings,
        repo_dir,
        diff,
        spec,
        extra_roots=None,
        board_id="",
        reference_files=None,
    ):
        del self, settings, repo_dir, diff, spec
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(DocumentStage, "_run_doc_agent", _fake_doc)

    out = DocumentStage().run(t, ctx)
    assert out.next_state is State.DELIVERABLE  # not BLOCKED
    assert "doc agent failed (non-blocking)" in out.note
    assert "RuntimeError" in out.note
    assert "model unavailable" in out.note

    # None of the old heuristic strings appear.
    for deleted in (
        "uv-only git deps",
        "uv.lock present but sync may have failed",
        "pip install may be needed",
        "[tool.uv.sources]",
        "project has Python dependencies",
        "no uv.lock — pip fallback cannot resolve git deps",
    ):
        assert deleted not in out.note

    # Notification also carries the real error.
    assert len(notifications) == 1
    assert notifications[0][0] == State.ERRORED
    assert "doc agent failed (non-blocking)" in notifications[0][1]
    assert "RuntimeError" in notifications[0][1]
    assert "model unavailable" in notifications[0][1]


# --- diff_base failure → BLOCKED --------------------------------------


def test_diff_base_failure_blocks(ctx_factory, monkeypatch):
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)

    def _failing_diff_base(repo, target_branch, **kw):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(
        "robotsix_mill.stages.document.git_ops.diff_base",
        _failing_diff_base,
    )

    out = DocumentStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "network unreachable" in out.note


# --- commit_all failure → warn-and-pass --------------------------------


def test_commit_all_failure_warns_and_passes(ctx_factory, monkeypatch):
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)

    def _fake_doc(
        self,
        *,
        settings,
        repo_dir,
        diff,
        spec,
        extra_roots=None,
        board_id="",
        reference_files=None,
    ):
        del self, settings, diff, spec
        (Path(repo_dir) / "README.md").write_text("# Changed\n")
        return DocResult(user_facing=True, summary="updated README")

    def _failing_commit_all(repo, msg):
        raise RuntimeError("commit failed")

    monkeypatch.setattr(DocumentStage, "_run_doc_agent", _fake_doc)
    monkeypatch.setattr(
        "robotsix_mill.stages.document.git_ops.commit_all",
        _failing_commit_all,
    )

    out = DocumentStage().run(t, ctx)
    assert out.next_state is State.DELIVERABLE  # not BLOCKED
    assert out.note == "updated README"


# --- review disabled → transitions to DELIVERABLE ---------------------


def test_review_disabled_transitions_to_deliverable(ctx_factory, monkeypatch):
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="false")
    t = _ticket(ctx)

    def _fake_doc(
        self,
        *,
        settings,
        repo_dir,
        diff,
        spec,
        extra_roots=None,
        board_id="",
        reference_files=None,
    ):
        del self, settings, repo_dir, diff, spec
        return DocResult(user_facing=True, summary="updated docs")

    monkeypatch.setattr(DocumentStage, "_run_doc_agent", _fake_doc)

    out = DocumentStage().run(t, ctx)
    assert out.next_state is State.DELIVERABLE
    assert out.note == "updated docs"


# --- classifier internal-only → skips full agent ----------------------


def test_classifier_internal_skips_full_agent(ctx_factory, monkeypatch):
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"

    commits_before = _git_log(repo_dir).count("\n") + 1

    full_agent_called = []

    def _fake_classifier(self, *, settings, diff, spec):
        del self, settings, diff, spec
        return DocClassifierResult(
            user_facing=False,
            classification="internal-only — test changes only",
        )

    def _fake_full_agent(self, *args, **kwargs):
        full_agent_called.append(1)
        return DocResult(user_facing=False, summary="")

    monkeypatch.setattr(DocumentStage, "_run_doc_classifier", _fake_classifier)
    monkeypatch.setattr(DocumentStage, "_run_doc_agent", _fake_full_agent)

    out = DocumentStage().run(t, ctx)
    assert out.next_state is State.DELIVERABLE
    assert "no user-facing changes" in out.note
    assert len(full_agent_called) == 0  # full agent never invoked

    # No new commits.
    commits_after = _git_log(repo_dir).count("\n") + 1
    assert commits_after == commits_before


# --- classifier user-facing → runs full agent -------------------------


def test_classifier_user_facing_runs_full_agent(ctx_factory, monkeypatch):
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"

    def _fake_classifier(self, *, settings, diff, spec):
        del self, settings, diff, spec
        return DocClassifierResult(
            user_facing=True,
            classification="user-facing — new config key",
        )

    def _fake_full_agent(
        self,
        *,
        settings,
        repo_dir,
        diff,
        spec,
        extra_roots=None,
        board_id="",
        reference_files=None,
    ):
        del self, settings, diff, spec
        (Path(repo_dir) / "README.md").write_text("# Updated by doc agent\n")
        return DocResult(user_facing=True, summary="updated README")

    monkeypatch.setattr(DocumentStage, "_run_doc_classifier", _fake_classifier)
    monkeypatch.setattr(DocumentStage, "_run_doc_agent", _fake_full_agent)

    out = DocumentStage().run(t, ctx)
    assert out.next_state is State.DELIVERABLE
    assert out.note == "updated README"

    # Full agent wrote docs and they were committed.
    assert (repo_dir / "README.md").read_text() == "# Updated by doc agent\n"
    log = _git_log(repo_dir)
    assert "mill(docs):" in log


# --- classifier exception → fall through to full agent ----------------


def test_classifier_exception_falls_through_to_full_agent(ctx_factory, monkeypatch):
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"

    full_agent_called = []

    def _failing_classifier(self, *, settings, diff, spec):
        del self, settings, diff, spec
        raise RuntimeError("model unavailable")

    def _fake_full_agent(
        self,
        *,
        settings,
        repo_dir,
        diff,
        spec,
        extra_roots=None,
        board_id="",
        reference_files=None,
    ):
        full_agent_called.append(1)
        del self, settings, diff, spec
        (Path(repo_dir) / "README.md").write_text("# Full agent ran\n")
        return DocResult(user_facing=True, summary="updated docs")

    monkeypatch.setattr(DocumentStage, "_run_doc_classifier", _failing_classifier)
    monkeypatch.setattr(DocumentStage, "_run_doc_agent", _fake_full_agent)

    out = DocumentStage().run(t, ctx)
    assert out.next_state is State.DELIVERABLE
    assert out.note == "updated docs"
    assert len(full_agent_called) == 1  # full agent still ran
    assert (repo_dir / "README.md").read_text() == "# Full agent ran\n"


# --- classifier verdict recorded in history ---------------------------


def test_classifier_verdict_recorded_in_history(ctx_factory, monkeypatch):
    """The classifier verdict is an agent conclusion — it lands in the
    transition note (history), not in comments. The previous behaviour
    posted a comment authored by `doc_classifier`; v1 removed that to
    keep comments reserved for ASK_USER + review threads."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)

    add_comment_calls = []
    orig_add = ctx.service.add_comment

    def _spy_add_comment(ticket_id, body, *, author="user", parent_id=None):
        add_comment_calls.append({"body": body, "author": author})
        return orig_add(ticket_id, body, author=author, parent_id=parent_id)

    monkeypatch.setattr(ctx.service, "add_comment", _spy_add_comment)

    def _fake_classifier(self, *, settings, diff, spec):
        del self, settings, diff, spec
        return DocClassifierResult(
            user_facing=False,
            classification="internal-only — model field rename",
        )

    monkeypatch.setattr(DocumentStage, "_run_doc_classifier", _fake_classifier)

    out = DocumentStage().run(t, ctx)

    classifier_comments = [
        c for c in add_comment_calls if c["author"] == "doc_classifier"
    ]
    assert classifier_comments == []  # no comment emitted
    # Verdict captured in transition note (visible in history).
    assert "doc_classifier" in (out.note or "")
    assert "internal-only" in (out.note or "")


# --- agent exception, non-Python project → still reports real error ---


def test_agent_exception_non_python_project_reports_real_error(
    ctx_factory, monkeypatch
):
    """Doc agent fails but there is no pyproject.toml — the note still
    contains the real exception type and message, not the old hint."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)

    # No pyproject.toml at all.

    def _fake_doc(
        self,
        *,
        settings,
        repo_dir,
        diff,
        spec,
        extra_roots=None,
        board_id="",
        reference_files=None,
    ):
        del self, settings, repo_dir, diff, spec
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(DocumentStage, "_run_doc_agent", _fake_doc)

    out = DocumentStage().run(t, ctx)
    assert out.next_state is State.DELIVERABLE
    assert "doc agent failed (non-blocking)" in out.note
    assert "RuntimeError" in out.note
    assert "model unavailable" in out.note


# --- agent exception with tokenized URL → credential redaction --------


def test_agent_exception_credential_redaction(ctx_factory, monkeypatch):
    """When the exception message embeds a tokenized URL, the secret
    must be redacted from the note and notification."""
    ctx = ctx_factory(FORGE_REMOTE_URL="file:///dummy", review_enabled="true")
    t = _ticket(ctx)

    tokenized_url = "https://x-access-token:SECRET@github.com/org/repo"
    exception_msg = f"Command failed: git fetch {tokenized_url} — network unreachable"

    notifications = []

    def _spy_notification(ticket, dst, note, settings):
        notifications.append((dst, note))

    monkeypatch.setattr(
        "robotsix_mill.stages.document.send_notification",
        _spy_notification,
    )

    def _fake_doc(
        self,
        *,
        settings,
        repo_dir,
        diff,
        spec,
        extra_roots=None,
        board_id="",
        reference_files=None,
    ):
        del self, settings, repo_dir, diff, spec
        raise RuntimeError(exception_msg)

    monkeypatch.setattr(DocumentStage, "_run_doc_agent", _fake_doc)

    out = DocumentStage().run(t, ctx)
    assert out.next_state is State.DELIVERABLE
    assert "doc agent failed (non-blocking)" in out.note
    assert "RuntimeError" in out.note

    # The secret must NOT appear.
    assert "SECRET" not in out.note
    assert "x-access-token" not in out.note
    # The URL should be redacted.
    assert "***@" in out.note
    assert "github.com/org/repo" in out.note

    # Notification is also redacted.
    assert len(notifications) == 1
    assert "SECRET" not in notifications[0][1]
    assert "***@" in notifications[0][1]
