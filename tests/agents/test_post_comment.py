"""Tests for the ``post_comment`` agent tool.

The tool exists because the implement agent's comment surface was
``reply_to_thread`` only — which requires a pre-existing parent
thread. A spec that said "post a comment with the findings" left the
agent reaching for ``reply_to_thread(thread_id=0)``, getting the
"parent comment 0 not found" error, and asking the operator a bogus
question (see ticket d129).

The tool contract this module guards:

- A top-level comment is created on the current ticket (no parent).
- Empty bodies are refused — silent no-ops would let an agent
  "succeed" without actually posting anything.
- Same-body retries within one tool lifetime are deduped — the
  retry-as-no-op contract every other agent tool has.
- No active ticket session → error string, never crash.
- DB exceptions → error string, never crash the agent loop.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from robotsix_mill.agents import post_comment as _pc
from robotsix_mill.config import Settings, Secrets, _reset_secrets
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    _reset_secrets()
    import robotsix_mill.config as _cfg
    _cfg._secrets = Secrets(openrouter_api_key="k")
    s = Settings(data_dir=str(tmp_path))
    db.reset_engine()
    db.init_db(s)
    return s


@pytest.fixture
def ticket(settings):
    svc = TicketService(settings)
    return svc.create("Investigate config drift", "body text")


class TestPostComment:
    def test_creates_top_level_comment(self, settings, ticket, monkeypatch):
        """The tool calls ``service.add_comment`` with no parent id,
        creating a fresh top-level thread. Author is the configured
        agent name so the comment is traceable to the run."""
        monkeypatch.setattr(
            "robotsix_mill.runtime.tracing.current_session",
            lambda: ticket.id,
        )

        tool = _pc.make_post_comment_tool(settings, agent_name="implement")
        out = tool("## Findings\n\nNo drift; YAML chain is wired.")
        assert out.startswith("posted comment ")

        svc = TicketService(settings)
        comments = svc.list_comments(ticket.id)
        assert len(comments) == 1
        assert comments[0].parent_id is None
        assert comments[0].author == "implement"
        assert "Findings" in comments[0].body

    def test_empty_body_refused(self, settings, ticket, monkeypatch):
        """An empty / whitespace body must not produce a comment. The
        risk is an agent ending a tool round by 'posting' an empty
        comment and counting that as completion — the operator would
        see nothing and the spec's deliverable wouldn't have been
        met."""
        monkeypatch.setattr(
            "robotsix_mill.runtime.tracing.current_session",
            lambda: ticket.id,
        )

        tool = _pc.make_post_comment_tool(settings, agent_name="implement")
        assert tool("").startswith("post_comment: empty body")
        assert tool("   \n\t  ").startswith("post_comment: empty body")

        svc = TicketService(settings)
        assert svc.list_comments(ticket.id) == []

    def test_dedupes_same_body_in_one_run(
        self, settings, ticket, monkeypatch,
    ):
        """A retried tool step that re-invokes ``post_comment`` with
        the SAME body returns a 'duplicate' status instead of posting
        a second comment. Mirrors the idempotency contract every
        other agent tool has."""
        monkeypatch.setattr(
            "robotsix_mill.runtime.tracing.current_session",
            lambda: ticket.id,
        )

        tool = _pc.make_post_comment_tool(settings, agent_name="implement")
        first = tool("Same body.")
        second = tool("Same body.")
        assert first.startswith("posted comment ")
        assert "duplicate" in second

        svc = TicketService(settings)
        assert len(svc.list_comments(ticket.id)) == 1

    def test_different_bodies_post_separately(
        self, settings, ticket, monkeypatch,
    ):
        """The dedupe is by exact body — different bodies post
        independently. Guards against an over-aggressive cache that
        would swallow genuinely-different follow-up findings."""
        monkeypatch.setattr(
            "robotsix_mill.runtime.tracing.current_session",
            lambda: ticket.id,
        )

        tool = _pc.make_post_comment_tool(settings, agent_name="implement")
        tool("First finding.")
        tool("Second finding.")
        svc = TicketService(settings)
        assert len(svc.list_comments(ticket.id)) == 2

    def test_no_active_session_returns_error_string(
        self, settings, monkeypatch,
    ):
        """When there's no current_session (e.g. the tool is invoked
        outside a stage's traced root span), the tool returns an
        explicit error string instead of crashing — the agent loop
        keeps going."""
        monkeypatch.setattr(
            "robotsix_mill.runtime.tracing.current_session",
            lambda: None,
        )
        tool = _pc.make_post_comment_tool(settings, agent_name="implement")
        out = tool("body")
        assert out.startswith("post_comment:") and "no active" in out

    def test_db_exception_returns_error_string(
        self, settings, ticket, monkeypatch,
    ):
        """A DB / service exception MUST come back as a string the
        agent can read, never bubble up into the agent loop where it
        would derail the run."""
        monkeypatch.setattr(
            "robotsix_mill.runtime.tracing.current_session",
            lambda: ticket.id,
        )

        def boom(*a, **kw):
            raise RuntimeError("DB connection refused")

        monkeypatch.setattr(TicketService, "add_comment", boom)

        tool = _pc.make_post_comment_tool(settings, agent_name="implement")
        out = tool("body")
        assert out.startswith("post_comment: could not post")
        assert "DB connection refused" in out

    def test_tool_registers_in_tool_registry(self, settings):
        """The factory side-effects a ToolInfo into the global
        registry so the operator-facing /tools page lists it
        alongside reply_to_thread and close_thread."""
        from robotsix_mill.agents.tool_registry import ToolRegistry

        _pc.make_post_comment_tool(settings, agent_name="implement")
        names = [t.name for t in ToolRegistry.list_tools()]
        assert "post_comment" in names
