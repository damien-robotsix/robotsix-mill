"""Tests for :mod:`robotsix_mill.agents.web_knowledge`.

The module wraps a multi-turn flash agent around a small on-disk
knowledge base (one Markdown file per library + a general-memory
file). Three concerns are exercised here:

* The synchronous utility helpers — slug normalisation, frontmatter
  parsing/stamping, and the inline index rendered into the agent's
  system prompt. These are pure-function / I/O-bounded but never
  touch the network.
* ``run_web_knowledge`` — the single mockable seam. We monkeypatch
  the Agent construction chain (``pydantic_ai.Agent``,
  ``OpenRouterProvider``, ``CostInstrumentedOpenRouterModel``) and the
  ``_aclose_async_client`` cleanup hook so the test never builds a
  real model or hits the network.
* ``make_ask_web_knowledge_tool`` — the gateway tool every other
  agent uses to reach the internet. Verifies the closure's name,
  delegation, and registration in ``ToolRegistry``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from robotsix_mill.agents import web_knowledge
from robotsix_mill.agents.tool_registry import ToolRegistry
from robotsix_mill.agents.web_knowledge import (
    _build_index,
    _is_stale,
    _KnowledgeMeta,
    _parse_frontmatter,
    _slug,
    _stamp_frontmatter,
    make_ask_web_knowledge_tool,
    run_web_knowledge,
)
from robotsix_mill.config import Settings


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _settings(tmp_path) -> Settings:
    """Settings rooted at *tmp_path* — the web_knowledge dir lives
    at ``<data_dir>/web_knowledge``."""
    return Settings(data_dir=str(tmp_path))


def _stamped(
    library: str,
    body: str,
    ts: str,
    source_url: str = "",
    verified_at: str = "",
    last_verified: str = "",
    stale: bool = False,
) -> str:
    """Build a frontmatter-stamped knowledge file body with an
    explicit ``last_updated:`` timestamp string and optional
    ``source_url`` / ``verified_at`` / ``last_verified`` / ``stale``."""
    lines = [
        "---",
        f"library: {library}",
        f"last_updated: {ts}",
    ]
    if source_url:
        lines.append(f"source_url: {source_url}")
    if verified_at:
        lines.append(f"verified_at: {verified_at}")
    if last_verified:
        lines.append(f"last_verified: {last_verified}")
    if stale:
        lines.append("stale: true")
    lines += ["---", body]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# TestHelpers — synchronous utility functions
# ---------------------------------------------------------------------------


class TestHelpers:
    """Pure-function utilities — no async, no HTTP, only the local fs."""

    # ----- _slug ----------------------------------------------------------

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("imaplib", "imaplib"),  # kebab-case passthrough
            ("FastAPI", "fastapi"),  # mixed case → lowercase
            ("Open Router", "open-router"),  # spaces → hyphens
            ("pydantic-ai", "pydantic-ai"),  # hyphens preserved
            ("scikit_learn", "scikit_learn"),  # underscore preserved
            ("foo!@#bar", "foo-bar"),  # special chars stripped → hyphen
            ("--leading-and-trailing--", "leading-and-trailing"),  # trims hyphens
            ("", "unknown"),  # empty → "unknown"
            ("   ", "unknown"),  # whitespace-only → "unknown"
            ("!!!", "unknown"),  # all-special → "unknown"
        ],
    )
    def test_slug_normalisation(self, raw, expected):
        assert _slug(raw) == expected

    # ----- _parse_frontmatter --------------------------------------------

    def test_parse_frontmatter_valid_isoformat(self):
        text = _stamped("imaplib", "body content here", "2026-01-15T12:30:00+00:00")
        meta = _parse_frontmatter(text)
        assert meta.last_updated == datetime(
            2026, 1, 15, 12, 30, 0, tzinfo=timezone.utc
        )
        assert meta.body == "body content here"
        assert meta.source_url is None
        assert meta.verified_at is None

    def test_parse_frontmatter_missing_returns_none_and_original(self):
        text = "no frontmatter here, just body"
        meta = _parse_frontmatter(text)
        assert meta.last_updated is None
        assert meta.source_url is None
        assert meta.verified_at is None
        assert meta.body == text

    def test_parse_frontmatter_naive_datetime_becomes_utc_aware(self):
        text = _stamped("x", "body", "2026-01-15T12:30:00")  # no tz
        meta = _parse_frontmatter(text)
        assert meta.last_updated is not None
        assert meta.last_updated.tzinfo is not None
        assert meta.last_updated == datetime(
            2026, 1, 15, 12, 30, 0, tzinfo=timezone.utc
        )
        assert meta.body == "body"

    def test_parse_frontmatter_malformed_timestamp_returns_none(self):
        text = _stamped("x", "body", "not-a-real-date")
        meta = _parse_frontmatter(text)
        assert meta.last_updated is None
        # When the timestamp is malformed the body is still returned
        # (the frontmatter regex matched).
        assert meta.body == "body"

    def test_parse_frontmatter_no_last_updated_line(self):
        text = "---\nlibrary: x\nfoo: bar\n---\nbody"
        meta = _parse_frontmatter(text)
        assert meta.last_updated is None
        assert meta.body == "body"

    def test_parse_frontmatter_returns_knowledge_meta_type(self):
        text = _stamped("x", "body", "2026-01-15T12:30:00+00:00")
        meta = _parse_frontmatter(text)
        assert isinstance(meta, _KnowledgeMeta)

    # ----- _stamp_frontmatter --------------------------------------------

    def test_stamp_frontmatter_shape(self):
        out = _stamp_frontmatter("imaplib", "the body")
        # Frontmatter delimiters and required keys.
        assert out.startswith("---\n")
        assert "library: imaplib\n" in out
        assert "last_updated:" in out
        assert "last_verified:" in out
        assert "stale: false" in out
        # The body trails after the closing delimiter.
        assert out.endswith("---\nthe body")

    def test_stamp_frontmatter_round_trips_through_parse(self):
        out = _stamp_frontmatter("fastapi", "preserve me")
        meta = _parse_frontmatter(out)
        assert meta.last_updated is not None
        assert meta.last_updated.tzinfo is not None
        assert meta.last_verified is not None
        assert meta.last_verified.tzinfo is not None
        assert meta.stale is False
        assert meta.body == "preserve me"
        assert meta.source_url is None
        assert meta.verified_at is None

    # --- source_url / verified_at round-trips ---------------------------

    def test_stamp_frontmatter_with_source_url_sets_verified_at(self):
        """When ``source_url`` is provided, ``verified_at`` is populated."""
        out = _stamp_frontmatter("lib", "body", source_url="https://example.com")
        assert "source_url: https://example.com" in out
        assert "verified_at:" in out
        meta = _parse_frontmatter(out)
        assert meta.source_url == "https://example.com"
        assert meta.verified_at is not None
        # verified_at should be within the last second
        now = datetime.now(timezone.utc)
        assert (now - meta.verified_at).total_seconds() < 5

    def test_stamp_frontmatter_without_source_url_no_verified_at(self):
        """Without ``source_url``, no ``verified_at`` line is stamped.
        ``last_verified`` and ``stale`` are always present."""
        out = _stamp_frontmatter("lib", "body")
        assert "source_url:" not in out
        assert "verified_at:" not in out
        assert "last_verified:" in out
        assert "stale: false" in out
        meta = _parse_frontmatter(out)
        assert meta.source_url is None
        assert meta.verified_at is None
        assert meta.last_verified is not None
        assert meta.stale is False

    def test_stamp_frontmatter_with_explicit_verified_at(self):
        """Passing ``verified_at`` overrides the auto-now."""
        explicit = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        out = _stamp_frontmatter(
            "lib", "body", source_url="https://x.com", verified_at=explicit
        )
        meta = _parse_frontmatter(out)
        assert meta.verified_at == explicit

    def test_stamp_frontmatter_empty_source_url_no_verified_at(self):
        """Empty-string ``source_url`` is treated as not-provided.
        ``last_verified`` and ``stale`` are still stamped."""
        out = _stamp_frontmatter("lib", "body", source_url="")
        assert "source_url:" not in out
        assert "verified_at:" not in out
        assert "last_verified:" in out
        assert "stale: false" in out

    def test_parse_frontmatter_extracts_source_url_and_verified_at(self):
        text = _stamped(
            "lib",
            "body",
            "2026-06-30T21:34:15+00:00",
            source_url="https://docs.example.com/api",
            verified_at="2026-06-30T21:34:15+00:00",
        )
        meta = _parse_frontmatter(text)
        assert meta.source_url == "https://docs.example.com/api"
        assert meta.verified_at == datetime(
            2026, 6, 30, 21, 34, 15, tzinfo=timezone.utc
        )

    def test_parse_frontmatter_verification_fields_absent_on_old_files(self):
        """Old knowledge files without source_url/verified_at parse cleanly."""
        text = _stamped("oldlib", "old body", "2026-01-01T00:00:00+00:00")
        meta = _parse_frontmatter(text)
        assert meta.source_url is None
        assert meta.verified_at is None
        assert meta.last_updated is not None
        assert meta.body == "old body"

    def test_parse_frontmatter_source_url_without_verified_at(self):
        """source_url may exist without verified_at (e.g. manually edited)."""
        text = "---\nlibrary: x\nlast_updated: 2026-01-01T00:00:00+00:00\nsource_url: https://x.com\n---\nbody"
        meta = _parse_frontmatter(text)
        assert meta.source_url == "https://x.com"
        assert meta.verified_at is None

    def test_parse_frontmatter_malformed_verified_at(self):
        """Unparseable verified_at → None, not an exception."""
        text = "---\nlibrary: x\nlast_updated: 2026-01-01T00:00:00+00:00\nverified_at: not-a-date\n---\nbody"
        meta = _parse_frontmatter(text)
        assert meta.verified_at is None
        assert meta.body == "body"

    def test_parse_frontmatter_verified_at_naive_becomes_utc(self):
        """verified_at without tzinfo is treated as UTC."""
        text = "---\nlibrary: x\nlast_updated: 2026-01-01T00:00:00+00:00\nverified_at: 2026-06-01T12:00:00\n---\nbody"
        meta = _parse_frontmatter(text)
        assert meta.verified_at is not None
        assert meta.verified_at.tzinfo == timezone.utc

    # ----- _build_index --------------------------------------------------

    def test_build_index_missing_directory(self, tmp_path):
        s = _settings(tmp_path)
        # Directory does not exist yet.
        assert not (s.data_dir / "web_knowledge").exists()
        assert _build_index(s) == "(empty)"

    def test_build_index_empty_directory(self, tmp_path):
        s = _settings(tmp_path)
        (s.data_dir / "web_knowledge").mkdir(parents=True)
        assert _build_index(s) == "(empty)"

    def test_build_index_general_memory_only(self, tmp_path):
        s = _settings(tmp_path)
        d = s.data_dir / "web_knowledge"
        d.mkdir(parents=True)
        (d / "_general.md").write_text("free-form notes", encoding="utf-8")
        out = _build_index(s)
        assert "_general memory_" in out
        assert "KB" in out
        # No bogus library rows when only the general file exists.
        assert "last_updated:" not in out

    def test_build_index_single_library_file(self, tmp_path):
        s = _settings(tmp_path)
        d = s.data_dir / "web_knowledge"
        d.mkdir(parents=True)
        (d / "imaplib.md").write_text(
            _stamped("imaplib", "body", "2026-01-15T12:30:00+00:00"),
            encoding="utf-8",
        )
        out = _build_index(s)
        assert "imaplib" in out
        assert "2026-01-15T12:30:00+00:00" in out
        assert "KB" in out

    def test_build_index_mixed_files_listed_alphabetically_general_first(
        self, tmp_path
    ):
        """The general-memory entry leads; library entries follow
        sorted alphabetically by stem so the index is stable."""
        s = _settings(tmp_path)
        d = s.data_dir / "web_knowledge"
        d.mkdir(parents=True)
        (d / "_general.md").write_text("g", encoding="utf-8")
        (d / "fastapi.md").write_text(
            _stamped("fastapi", "x", "2026-01-01T00:00:00+00:00"),
            encoding="utf-8",
        )
        (d / "alpha.md").write_text(
            _stamped("alpha", "x", "2026-01-02T00:00:00+00:00"),
            encoding="utf-8",
        )
        out = _build_index(s)
        # General memory leads.
        general_idx = out.index("_general memory_")
        alpha_idx = out.index("alpha")
        fastapi_idx = out.index("fastapi")
        assert general_idx < alpha_idx < fastapi_idx

    def test_build_index_skips_unreadable_files_silently(self, tmp_path, monkeypatch):
        """An ``OSError`` while reading a library file is swallowed —
        a corrupt or permission-denied file must not break the index
        for every other entry."""
        s = _settings(tmp_path)
        d = s.data_dir / "web_knowledge"
        d.mkdir(parents=True)
        good = d / "good.md"
        good.write_text(
            _stamped("good", "x", "2026-01-02T00:00:00+00:00"),
            encoding="utf-8",
        )
        bad = d / "bad.md"
        bad.write_text("placeholder", encoding="utf-8")

        real_read_text = type(bad).read_text

        def maybe_fail(self, *a, **kw):
            if self == bad:
                raise OSError("simulated unreadable")
            return real_read_text(self, *a, **kw)

        monkeypatch.setattr(type(bad), "read_text", maybe_fail)

        out = _build_index(s)
        # Good entry survives; bad entry is silently dropped.
        assert "good" in out
        assert "bad" not in out

    def test_build_index_file_without_timestamp_renders_no_timestamp_marker(
        self, tmp_path
    ):
        s = _settings(tmp_path)
        d = s.data_dir / "web_knowledge"
        d.mkdir(parents=True)
        # No frontmatter at all → parse returns ts=None.
        (d / "stamps.md").write_text("body with no frontmatter", encoding="utf-8")
        out = _build_index(s)
        assert "stamps" in out
        assert "(no timestamp)" in out

    # ----- _is_stale ---------------------------------------------------

    def test_is_stale_missing_last_verified(self):
        """``last_verified is None`` → always stale (never touched)."""
        meta = _KnowledgeMeta(
            last_updated=datetime(2026, 1, 1, tzinfo=timezone.utc),
            last_verified=None,
        )
        assert _is_stale(meta, ttl_hours=72) is True

    def test_is_stale_recent_last_verified(self):
        """``last_verified`` within the TTL → not stale."""
        now = datetime.now(timezone.utc)
        meta = _KnowledgeMeta(
            last_updated=now,
            last_verified=now,
        )
        assert _is_stale(meta, ttl_hours=72) is False

    def test_is_stale_old_last_verified(self):
        """``last_verified`` older than TTL → stale."""
        old = datetime.now(timezone.utc).replace(year=2020)
        meta = _KnowledgeMeta(
            last_updated=old,
            last_verified=old,
        )
        assert _is_stale(meta, ttl_hours=72) is True

    # ----- _parse_frontmatter — last_verified + stale -----------------

    def test_parse_frontmatter_with_last_verified(self):
        text = _stamped(
            "lib",
            "body",
            "2026-01-15T12:30:00+00:00",
            last_verified="2026-06-01T12:00:00+00:00",
        )
        meta = _parse_frontmatter(text)
        assert meta.last_verified == datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

    def test_parse_frontmatter_with_stale_true(self):
        text = (
            "---\n"
            "library: lib\n"
            "last_updated: 2026-01-01T00:00:00+00:00\n"
            "last_verified: 2026-01-01T00:00:00+00:00\n"
            "stale: true\n"
            "---\nbody"
        )
        meta = _parse_frontmatter(text)
        assert meta.stale is True

    def test_parse_frontmatter_stale_false_by_default(self):
        """When ``stale`` is absent from frontmatter, it defaults to False."""
        text = _stamped("lib", "body", "2026-01-15T12:30:00+00:00")
        meta = _parse_frontmatter(text)
        assert meta.stale is False

    def test_parse_frontmatter_last_verified_naive_becomes_utc(self):
        text = (
            "---\n"
            "library: x\n"
            "last_updated: 2026-01-01T00:00:00+00:00\n"
            "last_verified: 2026-06-01T12:00:00\n"
            "---\nbody"
        )
        meta = _parse_frontmatter(text)
        assert meta.last_verified is not None
        assert meta.last_verified.tzinfo == timezone.utc

    def test_parse_frontmatter_malformed_last_verified(self):
        """Unparseable last_verified → None, not an exception."""
        text = (
            "---\n"
            "library: x\n"
            "last_updated: 2026-01-01T00:00:00+00:00\n"
            "last_verified: not-a-date\n"
            "---\nbody"
        )
        meta = _parse_frontmatter(text)
        assert meta.last_verified is None
        assert meta.body == "body"

    # ----- _build_index — stale flagging -------------------------------

    def test_build_index_flags_stale_entry(self, tmp_path):
        """An entry whose last_verified is missing is flagged [STALE]."""
        s = _settings(tmp_path)
        d = s.data_dir / "web_knowledge"
        d.mkdir(parents=True)
        # Old file with no last_verified field → stale.
        (d / "oldlib.md").write_text(
            _stamped("oldlib", "body", "2020-01-01T00:00:00+00:00"),
            encoding="utf-8",
        )
        out = _build_index(s)
        assert "oldlib" in out
        assert "[STALE]" in out

    def test_build_index_does_not_flag_recent_entry(self, tmp_path):
        """A freshly stamped entry (with last_verified) is not stale."""
        s = _settings(tmp_path)
        d = s.data_dir / "web_knowledge"
        d.mkdir(parents=True)
        stamped = _stamp_frontmatter("newlib", "fresh body")
        (d / "newlib.md").write_text(stamped, encoding="utf-8")
        out = _build_index(s)
        assert "newlib" in out
        assert "[STALE]" not in out


# ---------------------------------------------------------------------------
# TestRunWebKnowledge — the single mockable seam
# ---------------------------------------------------------------------------


class _FakeAgent:
    """Stand-in for ``pydantic_ai.Agent`` — captures construction
    kwargs and returns a synthetic result from ``run()``."""

    def __init__(
        self,
        *,
        model=None,
        system_prompt="",
        output_type=str,
        tools=None,
        name="",
        retries=0,
        **_,
    ):
        self.model = model
        self.system_prompt = system_prompt
        self.output_type = output_type
        self.tools = tools or []
        self.name = name
        self.retries = retries

    async def run(self, question, *, usage_limits=None):
        self.last_question = question
        self.last_limits = usage_limits
        return type("_R", (), {"output": "the answer"})()


class _FakeFailingAgent(_FakeAgent):
    async def run(self, question, *, usage_limits=None):
        raise RuntimeError("model exploded")


class _FakeBudgetExhaustedAgent(_FakeAgent):
    async def run(self, question, *, usage_limits=None):
        from pydantic_ai.exceptions import UsageLimitExceeded

        raise UsageLimitExceeded(
            "The next request would exceed the request_limit of 12"
        )


def _patch_agent_chain(monkeypatch, agent_cls=_FakeAgent):
    """Replace the lazy Agent import + the level-1 model seam with stubs
    so ``run_web_knowledge`` builds nothing real."""
    import pydantic_ai
    from robotsix_mill.agents import base as bmod

    instances: list[_FakeAgent] = []

    def make(*a, **kw):
        inst = agent_cls(*a, **kw)
        instances.append(inst)
        return inst

    monkeypatch.setattr(pydantic_ai, "Agent", make)
    monkeypatch.setattr(
        bmod,
        "build_openrouter_model",
        lambda level=1, *, online=False: (object(), object()),
    )
    return instances


class TestRunWebKnowledge:
    """The async runner's contract: degrade on missing key, degrade on
    runtime error, return the agent output on success, and always
    close the http client in the ``finally`` block."""

    def test_missing_api_key_returns_unavailable_string(self, tmp_path, secrets_set):
        """No OPENROUTER_API_KEY → short, deterministic error string
        and NO agent construction."""
        secrets_set(openrouter_api_key="")
        s = _settings(tmp_path)
        out = asyncio.run(run_web_knowledge(settings=s, question="anything"))
        assert "unavailable" in out
        assert "OPENROUTER_API_KEY" in out

    def test_missing_api_key_does_not_construct_agent(
        self, tmp_path, secrets_set, monkeypatch
    ):
        """The early-return path must not even build the Agent — that
        keeps the unconfigured-mill code path token-free."""
        secrets_set(openrouter_api_key="")
        instances = _patch_agent_chain(monkeypatch)
        s = _settings(tmp_path)
        asyncio.run(run_web_knowledge(settings=s, question="anything"))
        assert instances == []

    def test_successful_run_returns_agent_output_as_string(
        self, tmp_path, secrets_set, monkeypatch
    ):
        """When the agent succeeds, the result is ``str(result.output)``
        — verbatim, no preamble."""
        secrets_set(openrouter_api_key="k")
        _patch_agent_chain(monkeypatch)
        s = _settings(tmp_path)
        out = asyncio.run(run_web_knowledge(settings=s, question="what is X?"))
        assert out == "the answer"

    def test_run_forwards_question_and_usage_limits(
        self, tmp_path, secrets_set, monkeypatch
    ):
        """The caller's question reaches ``agent.run`` and the runner
        bounds the consult with ``UsageLimits(request_limit=...)``
        from ``settings.web_knowledge_request_limit``."""
        secrets_set(openrouter_api_key="k")
        instances = _patch_agent_chain(monkeypatch)
        s = _settings(tmp_path)
        asyncio.run(run_web_knowledge(settings=s, question="precise question"))
        assert len(instances) == 1
        agent = instances[0]
        assert agent.last_question == "precise question"
        assert agent.last_limits.request_limit == s.web_knowledge_request_limit

    def test_system_prompt_interpolates_stale_days(
        self, tmp_path, secrets_set, monkeypatch
    ):
        """The ``web_knowledge_stale_days`` setting is interpolated into
        the agent's system prompt rather than hardcoded."""
        secrets_set(openrouter_api_key="k")
        instances = _patch_agent_chain(monkeypatch)
        s = _settings(tmp_path)
        s.web_knowledge_stale_days = 45
        s.web_knowledge_cache_ttl_hours = 99
        asyncio.run(run_web_knowledge(settings=s, question="q"))
        assert len(instances) == 1
        assert "~45 days" in instances[0].system_prompt
        assert "~30 days" not in instances[0].system_prompt
        assert "~99 hours" in instances[0].system_prompt

    def test_agent_failure_degrades_to_error_string(
        self, tmp_path, secrets_set, monkeypatch
    ):
        """When ``agent.run`` raises, ``run_web_knowledge`` never
        propagates — it returns ``"web_knowledge failed: <err>"``."""
        secrets_set(openrouter_api_key="k")
        _patch_agent_chain(monkeypatch, agent_cls=_FakeFailingAgent)
        s = _settings(tmp_path)
        out = asyncio.run(run_web_knowledge(settings=s, question="q"))
        assert out.startswith("web_knowledge failed:")
        assert "model exploded" in out

    def test_budget_exhaustion_returns_distinct_message(
        self, tmp_path, secrets_set, monkeypatch
    ):
        """When the sub-agent hits ``UsageLimitExceeded``, the error
        message signals budget exhaustion (not a generic failure) so
        the caller can avoid retrying the same question."""
        secrets_set(openrouter_api_key="k")
        _patch_agent_chain(monkeypatch, agent_cls=_FakeBudgetExhaustedAgent)
        s = _settings(tmp_path)
        out = asyncio.run(run_web_knowledge(settings=s, question="q"))
        assert out.startswith("web_knowledge budget exhausted:")
        assert "Do NOT retry" in out

    def test_finally_block_closes_http_client_on_success(
        self, tmp_path, secrets_set, monkeypatch
    ):
        """``_aclose_async_client`` is awaited in the ``finally``
        block on the happy path so the AsyncClient doesn't leak."""
        secrets_set(openrouter_api_key="k")
        _patch_agent_chain(monkeypatch)
        closed: list = []

        async def fake_close(client):
            closed.append(client)

        from robotsix_mill.agents import base as _agents_base

        monkeypatch.setattr(_agents_base, "_aclose_async_client", fake_close)
        s = _settings(tmp_path)
        out = asyncio.run(run_web_knowledge(settings=s, question="q"))
        assert out == "the answer"
        assert len(closed) == 1

    def test_finally_block_closes_http_client_on_failure(
        self, tmp_path, secrets_set, monkeypatch
    ):
        """The cleanup hook fires on the degrade path too — a model
        failure must not leak the AsyncClient either."""
        secrets_set(openrouter_api_key="k")
        _patch_agent_chain(monkeypatch, agent_cls=_FakeFailingAgent)
        closed: list = []

        async def fake_close(client):
            closed.append(client)

        from robotsix_mill.agents import base as _agents_base

        monkeypatch.setattr(_agents_base, "_aclose_async_client", fake_close)
        s = _settings(tmp_path)
        out = asyncio.run(run_web_knowledge(settings=s, question="q"))
        assert out.startswith("web_knowledge failed:")
        assert len(closed) == 1


# ---------------------------------------------------------------------------
# TestMakeTool — the ask_web_knowledge gateway
# ---------------------------------------------------------------------------


class TestMakeTool:
    """``make_ask_web_knowledge_tool`` builds the closure other agents
    use as their only route to the internet. It also registers the
    tool in the global ``ToolRegistry`` so refine/plan can see it."""

    def test_returned_tool_callable_is_named_ask_web_knowledge(self, tmp_path):
        s = _settings(tmp_path)
        tool = make_ask_web_knowledge_tool(s)
        assert callable(tool)
        assert tool.__name__ == "ask_web_knowledge"

    def test_tool_delegates_to_run_web_knowledge(self, tmp_path, monkeypatch):
        """Calling the tool delegates to ``run_web_knowledge`` with
        the bound ``settings`` and the caller's ``question`` — no
        rewrapping."""
        s = _settings(tmp_path)
        seen: dict = {}

        async def fake_run_web_knowledge(*, settings, question):
            seen["settings"] = settings
            seen["question"] = question
            return "delegated answer"

        monkeypatch.setattr(web_knowledge, "run_web_knowledge", fake_run_web_knowledge)
        tool = make_ask_web_knowledge_tool(s)
        out = asyncio.run(tool("how does X behave on Y?"))
        assert out == "delegated answer"
        assert seen["settings"] is s
        assert seen["question"] == "how does X behave on Y?"

    def test_tool_is_registered_in_tool_registry(self, tmp_path):
        """Building the tool registers it with category=``exploration``
        so planners can discover it; the description references its
        gateway role."""
        s = _settings(tmp_path)
        make_ask_web_knowledge_tool(s)
        info = ToolRegistry._tools.get("ask_web_knowledge")
        assert info is not None
        assert info.name == "ask_web_knowledge"
        assert info.category == "exploration"
        # The description mentions the gateway role — that's the
        # property planners use to pick it over a raw web_search.
        assert "web-knowledge" in info.description.lower()

    def test_registered_tool_lists_question_parameter(self, tmp_path):
        s = _settings(tmp_path)
        make_ask_web_knowledge_tool(s)
        info = ToolRegistry._tools["ask_web_knowledge"]
        assert "question" in info.parameters

    def test_block_reason_returns_immediately_no_run_web_knowledge(
        self, tmp_path, monkeypatch
    ):
        """When *block_reason* is set, the tool returns it immediately
        without calling ``run_web_knowledge`` — no web budget is spent."""
        s = _settings(tmp_path)
        called = False

        async def fake_run_web_knowledge(*, settings, question):
            nonlocal called
            called = True
            return "should not be reached"

        monkeypatch.setattr(web_knowledge, "run_web_knowledge", fake_run_web_knowledge)
        tool = make_ask_web_knowledge_tool(s, block_reason="blocked: internal failure")
        out = asyncio.run(tool("why did mypy fail?"))
        assert out == "blocked: internal failure"
        assert not called, "run_web_knowledge should not have been called"

    def test_tool_still_registered_when_blocked(self, tmp_path):
        """Even when *block_reason* is set, the tool is still registered
        in ToolRegistry — the prompt-tool-consistency guard is not
        tripped by its absence."""
        s = _settings(tmp_path)
        make_ask_web_knowledge_tool(s, block_reason="blocked: internal failure")
        info = ToolRegistry._tools.get("ask_web_knowledge")
        assert info is not None
        assert info.name == "ask_web_knowledge"
        assert info.category == "exploration"

    def test_block_reason_none_delegates_normally(self, tmp_path, monkeypatch):
        """With *block_reason=None* (default), the tool delegates to
        ``run_web_knowledge`` as before."""
        s = _settings(tmp_path)

        async def fake_run_web_knowledge(*, settings, question):
            return "normal answer"

        monkeypatch.setattr(web_knowledge, "run_web_knowledge", fake_run_web_knowledge)
        tool = make_ask_web_knowledge_tool(s)  # block_reason defaults to None
        out = asyncio.run(tool("what is X?"))
        assert out == "normal answer"


# ---------------------------------------------------------------------------
# TestTraceWebSearchBudget — per-survey-run web_search budget
# ---------------------------------------------------------------------------


class TestTraceWebSearchBudget:
    """Per-survey-run web_search budget — trace-level cap that survives
    across multiple ask_web_knowledge consults."""

    def test_trace_web_search_cap(self, tmp_path, monkeypatch):
        """After reset_trace_web_search_budget(2), the 3rd web_search call
        returns a budget-exhausted sentinel, regardless of how many
        run_web_knowledge consults it spans."""
        import asyncio

        from robotsix_mill.agents.web_knowledge import (
            reset_trace_web_search_budget,
            _make_tools,
        )

        s = _settings(tmp_path)

        # Stub run_web_research to return a fake conclusion.
        async def fake_run_web_research(*, settings, query):
            return f"conclusion for: {query}"

        # web_search closure lazy-imports web_research inside _make_tools,
        # so monkeypatch the web_research module, not web_knowledge.
        import robotsix_mill.agents.web_research as wr_mod

        monkeypatch.setattr(wr_mod, "run_web_research", fake_run_web_research)

        reset_trace_web_search_budget(2)
        tools = _make_tools(s)
        web_search = tools[-1]  # web_search is the last tool

        # First two searches succeed.
        r1 = asyncio.run(web_search("query 1"))
        r2 = asyncio.run(web_search("query 2"))
        assert "conclusion for: query 1" == r1
        assert "conclusion for: query 2" == r2

        # Third search hits the trace budget cap.
        r3 = asyncio.run(web_search("query 3"))
        assert "trace budget exhausted" in r3.lower()
        assert "web_search trace budget exhausted" in r3

    def test_trace_web_search_inactive_when_not_set(self, tmp_path, monkeypatch):
        """reset_trace_web_search_budget(0) or never called → no-op.
        All searches succeed (bounded only by per-consult caps)."""
        import asyncio

        from robotsix_mill.agents.web_knowledge import (
            reset_trace_web_search_budget,
            _make_tools,
        )

        s = _settings(tmp_path)

        async def fake_run_web_research(*, settings, query):
            return f"ok: {query}"

        # web_search closure lazy-imports web_research inside _make_tools,
        # so monkeypatch the web_research module, not web_knowledge.
        import robotsix_mill.agents.web_research as wr_mod

        monkeypatch.setattr(wr_mod, "run_web_research", fake_run_web_research)

        reset_trace_web_search_budget(0)  # deactivated
        tools = _make_tools(s)
        web_search = tools[-1]

        # Many searches all succeed — trace budget is inactive.
        for i in range(10):
            r = asyncio.run(web_search(f"query {i}"))
            assert f"ok: query {i}" == r


# --- trace_stage child-span tests ---------------------------------------


def test_trace_stage_ask_web_knowledge_nests_under_parent(
    tmp_path, secrets_set, monkeypatch
):
    """run_web_knowledge opens a child span named 'ask_web_knowledge'
    via trace_stage."""
    import contextlib

    spans: list[str] = []

    @contextlib.contextmanager
    def fake_trace_stage(name):
        spans.append(name)
        yield

    monkeypatch.setattr(web_knowledge, "trace_stage", fake_trace_stage)
    secrets_set(openrouter_api_key="k")
    _patch_agent_chain(monkeypatch)
    s = _settings(tmp_path)
    out = asyncio.run(run_web_knowledge(settings=s, question="q"))
    assert out == "the answer"
    assert spans == ["ask_web_knowledge"]
