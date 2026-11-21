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
- Self-referential meta-noise bodies are refused — the comment stream
  is for deliverable substance, not process noise.
- An agent is capped at a configurable number of comments per
  (ticket, author) — default 1 — counted against *persisted* comments
  so it survives tool-closure rebuilds.
- A refusal is logged as a warning and recorded as a step event.
- No active ticket session → error string, never crash.
- DB exceptions → error string, never crash the agent loop.
"""

from __future__ import annotations

import pytest

from robotsix_mill.agents import post_comment as _pc
from robotsix_mill.config import Secrets, Settings, _reset_secrets
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
    db.init_db(s, board_id="test-board")
    return s


@pytest.fixture
def ticket(settings):
    svc = TicketService(settings, board_id="test-board")
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

        svc = TicketService(settings, board_id="test-board")
        comments = svc.list_comments(ticket.id)
        assert len(comments) == 1
        assert comments[0].parent_id is None
        assert comments[0].author == "implement"
        assert "Findings" in comments[0].body

    def test_comment_alias_posts(self, settings, ticket, monkeypatch):
        """``comment`` (and a stray ``ticket_id``) are accepted as aliases of
        ``body`` — live 2026-09-08 three calls were schema-rejected with
        "Additional properties are not allowed ('comment' was unexpected)"."""
        from pydantic_ai import Tool

        monkeypatch.setattr(
            "robotsix_mill.runtime.tracing.current_session",
            lambda: ticket.id,
        )
        tool = _pc.make_post_comment_tool(settings, agent_name="implement")
        schema = Tool(tool).tool_def.parameters_json_schema
        assert {"body", "comment", "ticket_id"} <= set(schema["properties"])
        assert not schema.get("required")
        out = tool(comment="No change needed.", ticket_id="current")
        assert out.startswith("posted comment ")
        svc = TicketService(settings, board_id="test-board")
        assert svc.list_comments(ticket.id)[0].body == "No change needed."

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

        svc = TicketService(settings, board_id="test-board")
        assert svc.list_comments(ticket.id) == []

    def test_dedupes_same_body_in_one_run(
        self,
        settings,
        ticket,
        monkeypatch,
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

        svc = TicketService(settings, board_id="test-board")
        assert len(svc.list_comments(ticket.id)) == 1

    def test_different_bodies_post_separately(
        self,
        settings,
        ticket,
        monkeypatch,
    ):
        """The dedupe is by exact body — different bodies post
        independently (up to the per-agent comment cap). Guards against
        an over-aggressive cache that would swallow genuinely-different
        follow-up findings on a multi-finding info ticket (raised cap)."""
        monkeypatch.setattr(
            "robotsix_mill.runtime.tracing.current_session",
            lambda: ticket.id,
        )

        tool = _pc.make_post_comment_tool(
            settings, agent_name="implement", max_comments=2
        )
        tool("First finding.")
        tool("Second finding.")
        svc = TicketService(settings, board_id="test-board")
        assert len(svc.list_comments(ticket.id)) == 2

    def test_refuses_self_referential_meta_noise(
        self,
        settings,
        ticket,
        monkeypatch,
    ):
        """Pure self-referential meta-comments are refused even when the
        wording varies — the byte-identical dedupe cannot catch
        semantically-similar noise ('Done.', 'Stand down — no further
        action.', 'Delivered — see diff and summary thread X', ...).
        The refused body must go to the final structured summary
        instead, never the comment stream."""
        monkeypatch.setattr(
            "robotsix_mill.runtime.tracing.current_session",
            lambda: ticket.id,
        )
        tool = _pc.make_post_comment_tool(settings, agent_name="implement")
        noise = [
            "Done.",
            "Stand down — no further action.",
            "(no further replies)",
            "Stopping here deliberately: this is the last comment.",
            "Ignore my previous comment",
            "Delivered — see diff and summary thread X",
            "final note for the record",
            "(stop-noise marker)",
            "No further replies.",
        ]
        for body in noise:
            assert tool(body).startswith("post_comment: refused"), (
                f"{body!r} should be refused as meta-noise"
            )
        svc = TicketService(settings, board_id="test-board")
        assert svc.list_comments(ticket.id) == []

    def test_substantive_body_with_meta_words_is_allowed(
        self,
        settings,
        ticket,
        monkeypatch,
    ):
        """A body that contains a meta word but also substantive content
        is deliverable material, not noise — it must post."""
        monkeypatch.setattr(
            "robotsix_mill.runtime.tracing.current_session",
            lambda: ticket.id,
        )
        tool = _pc.make_post_comment_tool(settings, agent_name="implement")
        out = tool("No further action: the YAML chain is wired and tests pass.")
        assert out.startswith("posted comment ")
        svc = TicketService(settings, board_id="test-board")
        assert len(svc.list_comments(ticket.id)) == 1

    def test_caps_comments_at_default_one(
        self,
        settings,
        ticket,
        monkeypatch,
    ):
        """Default cap is 1 comment per (ticket, agent). A second,
        different body is refused with an explicit status, not posted."""
        monkeypatch.setattr(
            "robotsix_mill.runtime.tracing.current_session",
            lambda: ticket.id,
        )
        tool = _pc.make_post_comment_tool(settings, agent_name="implement")
        assert tool("First finding.").startswith("posted comment ")
        out = tool("Second finding.")
        assert out.startswith("post_comment: refused")
        assert "already posted 1 comment" in out
        svc = TicketService(settings, board_id="test-board")
        assert len(svc.list_comments(ticket.id)) == 1

    def test_cap_survives_tool_closure_rebuilds(
        self,
        settings,
        ticket,
        monkeypatch,
    ):
        """The cap counts PERSISTED comments (author == agent_name), so a
        fresh tool closure — a rebuilt agent / retried pass that loses
        the in-memory seen-set — still refuses once the cap is reached."""
        monkeypatch.setattr(
            "robotsix_mill.runtime.tracing.current_session",
            lambda: ticket.id,
        )
        first = _pc.make_post_comment_tool(settings, agent_name="implement")
        second = _pc.make_post_comment_tool(settings, agent_name="implement")
        assert first("First finding.").startswith("posted comment ")
        assert "refused" in second("Second finding.")
        svc = TicketService(settings, board_id="test-board")
        assert len(svc.list_comments(ticket.id)) == 1

    def test_cap_raised_for_multi_finding_info_tickets(
        self,
        settings,
        ticket,
        monkeypatch,
    ):
        """Multi-finding info tickets may raise the cap (e.g. 2-3) so a
        genuine set of findings can all be posted — the cap is a
        parameter, not hardcoded."""
        monkeypatch.setattr(
            "robotsix_mill.runtime.tracing.current_session",
            lambda: ticket.id,
        )
        tool = _pc.make_post_comment_tool(
            settings, agent_name="implement", max_comments=3
        )
        for body in ("Finding one.", "Finding two.", "Finding three."):
            assert tool(body).startswith("posted comment ")
        out = tool("Finding four.")
        assert out.startswith("post_comment: refused")
        svc = TicketService(settings, board_id="test-board")
        assert len(svc.list_comments(ticket.id)) == 3

    def test_refusal_is_recorded_as_step_event(
        self,
        settings,
        ticket,
        monkeypatch,
    ):
        """A refused body is recorded as a step event in the ticket
        history (never a comment), so the recurring violation stays
        observable and the prompt can be tightened further."""
        from sqlmodel import select

        from robotsix_mill.core.models import TicketEvent

        monkeypatch.setattr(
            "robotsix_mill.runtime.tracing.current_session",
            lambda: ticket.id,
        )
        tool = _pc.make_post_comment_tool(settings, agent_name="implement")
        assert "refused" in tool("Stand down.")
        with db.session(settings, "test-board") as sess:
            notes = [
                ev.note
                for ev in sess.exec(
                    select(TicketEvent).where(TicketEvent.ticket_id == ticket.id)
                ).all()
            ]
        assert any("meta-noise" in n for n in notes)
        svc = TicketService(settings, board_id="test-board")
        assert svc.list_comments(ticket.id) == []

    def test_dedupes_against_previously_posted_comment_across_closure(
        self,
        settings,
        ticket,
        monkeypatch,
    ):
        """A duplicate is caught even when a FRESH tool closure (a
        rebuilt agent / retried pass) loses the in-memory seen-set. The
        guard consults the persisted comments, so posting the same body
        again via a new closure still skips instead of double-posting."""
        monkeypatch.setattr(
            "robotsix_mill.runtime.tracing.current_session",
            lambda: ticket.id,
        )

        # Two separate closures (simulating a rebuilt agent mid-run) post
        # the SAME body — only the first should create a comment.
        first = _pc.make_post_comment_tool(settings, agent_name="implement")
        second = _pc.make_post_comment_tool(settings, agent_name="implement")
        assert first("Same body.").startswith("posted comment ")
        assert "duplicate" in second("Same body.")

        svc = TicketService(settings, board_id="test-board")
        assert len(svc.list_comments(ticket.id)) == 1

    def test_no_active_session_returns_error_string(
        self,
        settings,
        monkeypatch,
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
        assert out.startswith("post_comment:")
        assert "no active" in out

    def test_db_exception_returns_error_string(
        self,
        settings,
        ticket,
        monkeypatch,
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

    def test_tool_description_carries_comment_discipline(self, settings):
        """The registered description tells the model to post the
        deliverable at most once and forbids self-referential
        meta-comments — the observable behaviour rule documented by
        the implement-comment-spam retrospect gap (PR #3638/#3639
        comment streams were majority noise: 'Done.',
        'Delivered — see diff and summary thread X',
        '(Stop-noise marker)', 'ignore my previous comment')."""
        from robotsix_mill.agents.tool_registry import ToolRegistry

        _pc.make_post_comment_tool(settings, agent_name="implement")
        infos = [t for t in ToolRegistry.list_tools() if t.name == "post_comment"]
        assert infos, "post_comment must self-register a ToolInfo"
        desc = infos[0].description
        assert "at most once" in desc
        assert "self-referential meta-comments" in desc
