"""Tests for the answer stage."""

import subprocess

import pytest

from robotsix_mill.core import db
from robotsix_mill.core.models import TicketKind
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.answer import AnswerStage


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


def _ticket(ctx, title="What is the meaning of life?", body="The question"):
    """Create an inquiry ticket (starts in ASKED state)."""
    return ctx.service.create(title, body, kind=TicketKind.INQUIRY)


# --- happy path with clone --------------------------------------------


def test_happy_path_with_clone(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)

    fake_answer = "**42** — the ultimate answer."

    def _fake_run_answer(*, settings, title, question, repo_dir=None, repo_config=None):
        return fake_answer

    monkeypatch.setattr(
        "robotsix_mill.stages.answer.answering.run_answer_agent",
        _fake_run_answer,
    )
    # Also stub the clone so we don't need a real git remote.
    monkeypatch.setattr(
        "robotsix_mill.stages.answer._resolve_remote_url",
        lambda s, rc: None,
    )

    out = AnswerStage().run(t, ctx)
    assert out.next_state is State.ANSWERED
    assert out.note == "answered"

    # Verify the answer was written to description.md.
    assert ws.read_description() == fake_answer

    # Verify artifact was written.
    artifact = ws.artifacts_dir / "question-original.md"
    assert artifact.exists()
    assert "The question" in artifact.read_text(encoding="utf-8")


# --- happy path with existing clone (idempotent) ----------------------


def test_happy_path_with_existing_clone(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)

    repo_cand = ws.dir / "repo"
    repo_cand.mkdir()
    (repo_cand / ".git").mkdir()

    clone_called = []

    def _fake_clone(*args, **kwargs):
        clone_called.append(1)

    monkeypatch.setattr(
        "robotsix_mill.stages.answer.git_ops.clone",
        _fake_clone,
    )

    fake_answer = "Cached answer."

    def _fake_run_answer(*, settings, title, question, repo_dir=None, repo_config=None):
        return fake_answer

    monkeypatch.setattr(
        "robotsix_mill.stages.answer.answering.run_answer_agent",
        _fake_run_answer,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.answer._resolve_remote_url",
        lambda s, rc: "https://example.com/repo.git",
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.answer.github_token",
        lambda s, repo_config=None: "fake-token",
    )

    out = AnswerStage().run(t, ctx)
    assert out.next_state is State.ANSWERED
    assert len(clone_called) == 0  # no re-clone

    # repo_dir should be passed to agent.
    imported_calls = []

    def _capture(*, settings, title, question, repo_dir=None, repo_config=None):
        imported_calls.append(repo_dir)
        return fake_answer

    # Re-run with capture to verify repo_dir is passed.
    # Create a fresh ticket for a clean run.
    t2 = _ticket(ctx, "Another question", "body2")
    ws2 = ctx.service.workspace(t2)
    (ws2.dir / "repo").mkdir()
    (ws2.dir / "repo" / ".git").mkdir()

    monkeypatch.setattr(
        "robotsix_mill.stages.answer.answering.run_answer_agent",
        _capture,
    )

    out2 = AnswerStage().run(t2, ctx)
    assert out2.next_state is State.ANSWERED
    assert imported_calls[0] == (ws2.dir / "repo")


# --- happy path without remote (no clone) -----------------------------


def test_happy_path_without_remote(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx)

    monkeypatch.setattr(
        "robotsix_mill.stages.answer._resolve_remote_url",
        lambda s, rc: None,
    )

    agent_called = []

    def _fake_run_answer(*, settings, title, question, repo_dir=None, repo_config=None):
        agent_called.append(repo_dir)
        return "Answer without repo."

    monkeypatch.setattr(
        "robotsix_mill.stages.answer.answering.run_answer_agent",
        _fake_run_answer,
    )

    out = AnswerStage().run(t, ctx)
    assert out.next_state is State.ANSWERED
    assert agent_called[0] is None  # no repo_dir passed


# --- clone failure (best-effort, agent still runs) --------------------


def test_clone_failure_agent_still_runs(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx)
    ws = ctx.service.workspace(t)

    def _failing_clone(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "git clone", stderr=b"network error")

    monkeypatch.setattr(
        "robotsix_mill.stages.answer.git_ops.clone",
        _failing_clone,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.answer._resolve_remote_url",
        lambda s, rc: "https://example.com/repo.git",
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.answer.github_token",
        lambda s, repo_config=None: "fake-token",
    )

    agent_called = []

    def _fake_run_answer(*, settings, title, question, repo_dir=None, repo_config=None):
        agent_called.append(repo_dir)
        return "Answer even without clone."

    monkeypatch.setattr(
        "robotsix_mill.stages.answer.answering.run_answer_agent",
        _fake_run_answer,
    )

    out = AnswerStage().run(t, ctx)
    assert out.next_state is State.ANSWERED
    assert agent_called[0] is None  # repo_dir is None after clone failure
    assert ws.read_description() == "Answer even without clone."


# --- empty title+question → BLOCKED -----------------------------------


def test_empty_title_and_question_blocks(ctx_factory):
    ctx = ctx_factory()
    t = _ticket(ctx, title="", body="")

    out = AnswerStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "empty title and question" in out.note


# --- RuntimeError from agent → BLOCKED --------------------------------


def test_runtime_error_from_agent_blocks(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx)

    monkeypatch.setattr(
        "robotsix_mill.stages.answer._resolve_remote_url",
        lambda s, rc: None,
    )

    def _failing_agent(*, settings, title, question, repo_dir=None, repo_config=None):
        raise RuntimeError("OPENROUTER_API_KEY not set")

    monkeypatch.setattr(
        "robotsix_mill.stages.answer.answering.run_answer_agent",
        _failing_agent,
    )

    out = AnswerStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "OPENROUTER_API_KEY not set" in out.note


# --- empty answer → BLOCKED -------------------------------------------


def test_empty_answer_blocks(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx)

    monkeypatch.setattr(
        "robotsix_mill.stages.answer._resolve_remote_url",
        lambda s, rc: None,
    )

    def _empty_agent(*, settings, title, question, repo_dir=None, repo_config=None):
        return ""

    monkeypatch.setattr(
        "robotsix_mill.stages.answer.answering.run_answer_agent",
        _empty_agent,
    )

    out = AnswerStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "empty answer" in out.note


def test_whitespace_only_answer_blocks(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx)

    monkeypatch.setattr(
        "robotsix_mill.stages.answer._resolve_remote_url",
        lambda s, rc: None,
    )

    def _whitespace_agent(
        *, settings, title, question, repo_dir=None, repo_config=None
    ):
        return "   \n  "

    monkeypatch.setattr(
        "robotsix_mill.stages.answer.answering.run_answer_agent",
        _whitespace_agent,
    )

    out = AnswerStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "empty answer" in out.note


# --- epic context enrichment ------------------------------------------


def test_epic_context_enrichment(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    # Create an epic with a description.
    epic = ctx.service.create("Epic", "This is the epic context.", kind=TicketKind.EPIC)

    # Create a child ticket.
    t = _ticket(ctx, title="Child question", body="Child body")
    ctx.service.set_parent(t.id, epic.id)
    t = ctx.service.get(t.id)

    monkeypatch.setattr(
        "robotsix_mill.stages.answer._resolve_remote_url",
        lambda s, rc: None,
    )

    received_question = []

    def _capture_question(
        *, settings, title, question, repo_dir=None, repo_config=None
    ):
        received_question.append(question)
        return "Answer."

    monkeypatch.setattr(
        "robotsix_mill.stages.answer.answering.run_answer_agent",
        _capture_question,
    )

    out = AnswerStage().run(t, ctx)
    assert out.next_state is State.ANSWERED
    assert "epic-context" in received_question[0]
    assert "This is the epic context." in received_question[0]


# --- artifact preservation --------------------------------------------


def test_artifact_preservation(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx, title="Title only", body="Full question body")

    monkeypatch.setattr(
        "robotsix_mill.stages.answer._resolve_remote_url",
        lambda s, rc: None,
    )

    def _fake_agent(*, settings, title, question, repo_dir=None, repo_config=None):
        return "Answer."

    monkeypatch.setattr(
        "robotsix_mill.stages.answer.answering.run_answer_agent",
        _fake_agent,
    )

    AnswerStage().run(t, ctx)
    ws = ctx.service.workspace(t)
    artifact = ws.artifacts_dir / "question-original.md"
    assert artifact.exists()
    content = artifact.read_text(encoding="utf-8")
    assert "Full question body" in content


def test_artifact_preservation_title_only(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx, title="Title only", body="")

    monkeypatch.setattr(
        "robotsix_mill.stages.answer._resolve_remote_url",
        lambda s, rc: None,
    )

    def _fake_agent(*, settings, title, question, repo_dir=None, repo_config=None):
        return "Answer."

    monkeypatch.setattr(
        "robotsix_mill.stages.answer.answering.run_answer_agent",
        _fake_agent,
    )

    AnswerStage().run(t, ctx)
    ws = ctx.service.workspace(t)
    artifact = ws.artifacts_dir / "question-original.md"
    assert artifact.exists()
    content = artifact.read_text(encoding="utf-8")
    # When title is non-empty and body is empty, question becomes just the title
    # (read_description returns body which is "", so question = epic_ctx + "\n\n" + "" = "" if no epic)
    # Actually: read_description() returns body = "", title is "Title only"
    # The question variable ends up being "" (from read_description), and title is non-empty
    # so it does NOT hit the early BLOCKED return
    # The artifact is written with question="" which is falsy so it falls to the or branch
    assert "title-only inquiry" in content


# --- title-only question (no body but has title) ----------------------


def test_title_only_question_answered(ctx_factory, monkeypatch):
    ctx = ctx_factory()
    t = _ticket(ctx, title="Title only", body="")

    monkeypatch.setattr(
        "robotsix_mill.stages.answer._resolve_remote_url",
        lambda s, rc: None,
    )

    def _fake_agent(*, settings, title, question, repo_dir=None, repo_config=None):
        return "Answered title-only."

    monkeypatch.setattr(
        "robotsix_mill.stages.answer.answering.run_answer_agent",
        _fake_agent,
    )

    out = AnswerStage().run(t, ctx)
    assert out.next_state is State.ANSWERED


def test_stage_passes_repo_config_to_agent(ctx_factory, monkeypatch):
    """The answer stage forwards ctx.repo_config to run_answer_agent."""
    ctx = ctx_factory()
    t = _ticket(ctx)

    monkeypatch.setattr(
        "robotsix_mill.stages.answer._resolve_remote_url",
        lambda s, rc: None,
    )

    captured = {}

    def _capture(*, settings, title, question, repo_dir=None, repo_config=None):
        captured["repo_config"] = repo_config
        return "ok"

    monkeypatch.setattr(
        "robotsix_mill.stages.answer.answering.run_answer_agent",
        _capture,
    )

    out = AnswerStage().run(t, ctx)
    assert out.next_state is State.ANSWERED
    assert captured["repo_config"] is ctx.repo_config
    assert captured["repo_config"].langfuse_public_key == "pk-test"
