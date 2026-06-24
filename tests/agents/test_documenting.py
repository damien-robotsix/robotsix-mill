"""Tests for the documenting agent module — classifier gate logic,
diff reading, prompt construction with ``section()`` blocks, memory
ledger loading/persisting, reference-file preseeding, and model
resolution.

Covers ``DocClassifierResult``, ``DocResult``, ``run_doc_classifier``,
and ``run_doc_agent`` from ``robotsix_mill.agents.documenting``.

Does NOT test:
- ``load_agent_definition`` (test_yaml_loader.py)
- ``build_preseed_history`` internals (test_fs_tools.py)
- ``call_with_retry`` (test_retry.py)
- ``load_memory`` / ``persist_memory`` (test_pass_runner.py)
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from robotsix_mill.agents.documenting import (
    DocClassifierResult,
    DocResult,
    run_doc_agent,
    run_doc_classifier,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _FakeAgent:
    """Replacement for a pydantic-ai Agent with a ``run_sync`` that
    records calls and returns a controlled output."""

    def __init__(self, output):
        self.output = output
        self.closed = False
        self.calls: list[tuple] = []

    def run_sync(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        return _Result(self.output)

    def close(self):
        self.closed = True


class _Result:
    """Minimal stand-in for pydantic-ai's run result — only
    ``.output`` is read by the code under test."""

    def __init__(self, output):
        self.output = output


def _patch_build_agent_from_definition(monkeypatch, agent_factory):
    """Replace ``build_agent_from_definition`` with *agent_factory*,
    a callable ``(settings, definition, *, tools, **overrides)`` that
    returns an agent-like object.

    Also patches ``call_with_retry`` (on the retry module, since it is
    lazy-imported inside the functions under test) to just invoke the
    callable once — no actual retry loop.
    """
    monkeypatch.setattr(
        "robotsix_mill.agents.base.build_agent_from_definition",
        agent_factory,
    )
    monkeypatch.setattr(
        "robotsix_mill.agents.retry.run_agent",
        lambda agent, make_run, **kw: make_run(agent),
    )


def _make_definition(**kw):
    """Return a lightweight fake AgentDefinition with sensible defaults
    for every field that ``build_agent_from_definition`` reads."""
    defaults = dict(
        name="test-def",
        system_prompt="You are a test agent.",
        model=None,
        web_knowledge=False,
        report_issue=False,
        read_ticket=False,
        reply_to_thread=False,
        close_thread=False,
        ask_user=False,
        retries=1,
        output_type="DocResult",
        module="documenting",
        skills=[],
    )
    defaults.update(kw)
    return type("_Def", (), defaults)()


def _dummy_fs_tool(name: str):
    """Return a dummy callable whose ``__name__`` is *name*."""

    def _fn(*a, **k):
        pass

    _fn.__name__ = name
    return _fn


# ---------------------------------------------------------------------------
# DocClassifierResult model tests
# ---------------------------------------------------------------------------


class TestDocClassifierResult:
    def test_valid_construction(self):
        r = DocClassifierResult(
            user_facing=True,
            classification="user-facing — new CLI flag",
        )
        assert r.user_facing is True
        assert r.classification == "user-facing — new CLI flag"

    def test_user_facing_must_be_bool(self):
        # Pydantic v2 coerces truthy strings to bool, so a list is
        # genuinely uncoercible.
        with pytest.raises(ValidationError):
            DocClassifierResult(user_facing=[1, 2, 3], classification="ok")

    def test_classification_rejects_empty(self):
        with pytest.raises(ValidationError) as exc:
            DocClassifierResult(user_facing=False, classification="")
        assert "classification" in str(exc.value)

    def test_classification_min_length_1(self):
        # Single char is fine.
        r = DocClassifierResult(user_facing=False, classification="x")
        assert r.classification == "x"

    def test_json_round_trip(self):
        r = DocClassifierResult(
            user_facing=True,
            classification="user-facing — new endpoint",
        )
        data = r.model_dump_json()
        round_tripped = DocClassifierResult.model_validate_json(data)
        assert round_tripped.user_facing == r.user_facing
        assert round_tripped.classification == r.classification


# ---------------------------------------------------------------------------
# DocResult model tests
# ---------------------------------------------------------------------------


class TestDocResult:
    def test_valid_construction(self):
        r = DocResult(
            user_facing=True,
            summary="updated README with new config key",
            updated_memory="README has sections: Overview, Config, API",
        )
        assert r.user_facing is True
        assert r.summary == "updated README with new config key"
        assert r.updated_memory == "README has sections: Overview, Config, API"

    def test_updated_memory_defaults_empty(self):
        r = DocResult(user_facing=False, summary="no changes")
        assert r.updated_memory == ""

    def test_summary_rejects_empty(self):
        with pytest.raises(ValidationError) as exc:
            DocResult(user_facing=True, summary="")
        assert "summary" in str(exc.value)

    def test_json_round_trip(self):
        r = DocResult(
            user_facing=False,
            summary="internal-only — test changes",
            updated_memory="",
        )
        data = r.model_dump_json()
        round_tripped = DocResult.model_validate_json(data)
        assert round_tripped.user_facing == r.user_facing
        assert round_tripped.summary == r.summary
        assert round_tripped.updated_memory == r.updated_memory


# ---------------------------------------------------------------------------
# run_doc_classifier orchestration tests
# ---------------------------------------------------------------------------


class TestRunDocClassifier:
    DIFF = "diff --git a/foo.py b/foo.py"
    SPEC = "## Problem\nAdd a new CLI flag."

    def test_zero_tools(self, settings, monkeypatch):
        """The classifier agent is built with zero tools."""
        captured: dict = {}

        def fake_build(settings_, definition, *, tools=None, **overrides):
            captured["tools"] = tools
            return _FakeAgent(
                DocClassifierResult(
                    user_facing=True,
                    classification="user-facing — new flag",
                )
            )

        _patch_build_agent_from_definition(monkeypatch, fake_build)
        monkeypatch.setattr(
            "robotsix_mill.agents.yaml_loader.load_agent_definition",
            lambda path: _make_definition(),
        )

        run_doc_classifier(
            settings=settings,
            diff=self.DIFF,
            spec=self.SPEC,
        )
        assert captured["tools"] == []

    def test_prompt_sections_present(self, settings, monkeypatch):
        """The user prompt includes both ticket-spec and git-diff
        fenced sections."""
        fake_agent = _FakeAgent(
            DocClassifierResult(
                user_facing=False,
                classification="internal-only — refactor",
            )
        )

        def fake_build(*a, tools=None, **kw):
            return fake_agent

        _patch_build_agent_from_definition(monkeypatch, fake_build)
        monkeypatch.setattr(
            "robotsix_mill.agents.yaml_loader.load_agent_definition",
            lambda path: _make_definition(),
        )

        run_doc_classifier(
            settings=settings,
            diff=self.DIFF,
            spec=self.SPEC,
        )

        assert len(fake_agent.calls) == 1
        prompt, _ = fake_agent.calls[0]
        assert "````ticket-spec" in prompt
        assert self.SPEC in prompt
        assert "````git-diff" in prompt
        assert self.DIFF in prompt

    def test_long_diff_truncated_to_cap(self, settings, monkeypatch):
        """A diff longer than doc_classifier_diff_max_chars is truncated
        (and carries the omission marker) before being embedded in the
        git-diff section."""
        fake_agent = _FakeAgent(
            DocClassifierResult(user_facing=True, classification="x")
        )

        def fake_build(*a, tools=None, **kw):
            return fake_agent

        _patch_build_agent_from_definition(monkeypatch, fake_build)
        monkeypatch.setattr(
            "robotsix_mill.agents.yaml_loader.load_agent_definition",
            lambda path: _make_definition(),
        )
        cap = 200
        settings.doc_classifier_diff_max_chars = cap
        long_diff = "diff --git a/big.py b/big.py\n" + ("x" * 5000)

        run_doc_classifier(
            settings=settings,
            diff=long_diff,
            spec=self.SPEC,
        )

        prompt, _ = fake_agent.calls[0]
        assert "[... description truncated;" in prompt
        # The full untruncated diff body must NOT survive in the prompt.
        assert long_diff not in prompt
        # The embedded diff content is bounded by the cap (plus the
        # short appended marker).
        assert len(prompt) < len(self.SPEC) + cap + 200

    def test_short_diff_unchanged(self, settings, monkeypatch):
        """A diff at/under the cap is passed through verbatim with no
        truncation marker."""
        fake_agent = _FakeAgent(
            DocClassifierResult(user_facing=False, classification="x")
        )

        def fake_build(*a, tools=None, **kw):
            return fake_agent

        _patch_build_agent_from_definition(monkeypatch, fake_build)
        monkeypatch.setattr(
            "robotsix_mill.agents.yaml_loader.load_agent_definition",
            lambda path: _make_definition(),
        )
        settings.doc_classifier_diff_max_chars = 6000

        run_doc_classifier(
            settings=settings,
            diff=self.DIFF,
            spec=self.SPEC,
        )

        prompt, _ = fake_agent.calls[0]
        assert self.DIFF in prompt
        assert "truncated" not in prompt

    def test_usage_limits_wired(self, settings, monkeypatch):
        """usage_limits uses settings.doc_classifier_request_limit."""
        fake_agent = _FakeAgent(
            DocClassifierResult(
                user_facing=False,
                classification="internal-only",
            )
        )

        def fake_build(*a, tools=None, **kw):
            return fake_agent

        _patch_build_agent_from_definition(monkeypatch, fake_build)
        monkeypatch.setattr(
            "robotsix_mill.agents.yaml_loader.load_agent_definition",
            lambda path: _make_definition(),
        )

        from pydantic_ai.usage import UsageLimits

        run_doc_classifier(
            settings=settings,
            diff=self.DIFF,
            spec=self.SPEC,
        )

        _, kwargs = fake_agent.calls[0]
        limits = kwargs["usage_limits"]
        assert isinstance(limits, UsageLimits)
        assert limits.request_limit == settings.doc_classifier_request_limit

    def test_returns_fake_output(self, settings, monkeypatch):
        """The function returns the fake agent's output unchanged."""
        expected = DocClassifierResult(
            user_facing=True,
            classification="user-facing — new endpoint",
        )

        def fake_build(*a, tools=None, **kw):
            return _FakeAgent(expected)

        _patch_build_agent_from_definition(monkeypatch, fake_build)
        monkeypatch.setattr(
            "robotsix_mill.agents.yaml_loader.load_agent_definition",
            lambda path: _make_definition(),
        )

        result = run_doc_classifier(
            settings=settings,
            diff=self.DIFF,
            spec=self.SPEC,
        )
        assert result is expected
        assert result.user_facing is True

    def test_agent_close_called(self, settings, monkeypatch):
        """agent.close() is called after a successful run."""
        fake_agent = _FakeAgent(
            DocClassifierResult(
                user_facing=False,
                classification="internal-only",
            )
        )

        def fake_build(*a, tools=None, **kw):
            return fake_agent

        _patch_build_agent_from_definition(monkeypatch, fake_build)
        monkeypatch.setattr(
            "robotsix_mill.agents.yaml_loader.load_agent_definition",
            lambda path: _make_definition(),
        )

        run_doc_classifier(
            settings=settings,
            diff=self.DIFF,
            spec=self.SPEC,
        )
        assert fake_agent.closed is True

    def test_agent_close_called_on_error(self, settings, monkeypatch):
        """agent.close() is called even when run_sync raises."""
        fake_agent = _FakeAgent(
            DocClassifierResult(user_facing=False, classification="x")
        )
        fake_agent.run_sync = lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("model down")
        )

        def fake_build(*a, tools=None, **kw):
            return fake_agent

        _patch_build_agent_from_definition(monkeypatch, fake_build)
        monkeypatch.setattr(
            "robotsix_mill.agents.yaml_loader.load_agent_definition",
            lambda path: _make_definition(),
        )

        with pytest.raises(RuntimeError, match="model down"):
            run_doc_classifier(
                settings=settings,
                diff=self.DIFF,
                spec=self.SPEC,
            )
        assert fake_agent.closed is True

    def test_exception_propagates(self, settings, monkeypatch):
        """run_doc_classifier has no fallback — exceptions propagate."""

        def fake_build(*a, tools=None, **kw):
            raise RuntimeError("agent build failed")

        # Don't use _patch_build_agent_from_definition — we want the
        # real call_with_retry to be irrelevant since build itself fails.
        monkeypatch.setattr(
            "robotsix_mill.agents.base.build_agent_from_definition",
            fake_build,
        )
        monkeypatch.setattr(
            "robotsix_mill.agents.yaml_loader.load_agent_definition",
            lambda path: _make_definition(),
        )
        # call_with_retry must also be mocked so the lazy import
        # inside run_doc_classifier resolves.
        monkeypatch.setattr(
            "robotsix_mill.agents.retry.run_agent",
            lambda agent, make_run, **kw: make_run(agent),
        )

        with pytest.raises(RuntimeError, match="agent build failed"):
            run_doc_classifier(
                settings=settings,
                diff=self.DIFF,
                spec=self.SPEC,
            )

    def test_non_matching_output_validation_fails(self, settings, monkeypatch):
        """When run_sync returns output that's not a DocClassifierResult,
        it still propagates (validation happens inside pydantic-ai's
        run_sync, which is bypassed by the fake). The raw value is
        returned as-is."""
        raw_output = "not a DocClassifierResult"
        fake_agent = _FakeAgent(raw_output)

        def fake_build(*a, tools=None, **kw):
            return fake_agent

        _patch_build_agent_from_definition(monkeypatch, fake_build)
        monkeypatch.setattr(
            "robotsix_mill.agents.yaml_loader.load_agent_definition",
            lambda path: _make_definition(),
        )

        result = run_doc_classifier(
            settings=settings,
            diff=self.DIFF,
            spec=self.SPEC,
        )
        assert result is raw_output


# ---------------------------------------------------------------------------
# run_doc_agent orchestration tests
# ---------------------------------------------------------------------------


class TestRunDocAgent:
    DIFF = "diff --git a/src/app.py b/src/app.py"
    SPEC = "## Problem\nAdd rate-limiting middleware."

    @pytest.fixture
    def repo_dir(self, tmp_path):
        d = tmp_path / "repo"
        d.mkdir()
        return d

    def _patch_dependencies(self, monkeypatch, fake_agent):
        """Common patching for run_doc_agent tests: replace
        build_agent_from_definition, call_with_retry, load_agent_definition,
        build_fs_tools, make_explore_tool, load_memory, persist_memory,
        and build_preseed_history."""
        _patch_build_agent_from_definition(
            monkeypatch,
            lambda *a, tools=None, **kw: fake_agent,
        )
        monkeypatch.setattr(
            "robotsix_mill.agents.yaml_loader.load_agent_definition",
            lambda path: _make_definition(system_prompt="Doc system prompt."),
        )
        # FS tools — return 6 tools, only 4 should survive the filter.
        monkeypatch.setattr(
            "robotsix_mill.agents.fs_tools.build_fs_tools",
            lambda repo_dir, settings, extra_roots=None: [
                _dummy_fs_tool(n)
                for n in (
                    "read_file",
                    "write_file",
                    "list_dir",
                    "edit_file",
                    "run_command",
                    "delete_file",
                )
            ],
        )
        # Explore tool.
        monkeypatch.setattr(
            "robotsix_mill.agents.explore.make_explore_tool",
            lambda settings, repo_dir, extra_roots=None, **kwargs: _dummy_fs_tool(
                "explore"
            ),
        )
        # Memory — no existing ledger by default.
        monkeypatch.setattr(
            "robotsix_mill.runners.pass_runner.load_memory",
            lambda path, max_chars=None: "",
        )
        monkeypatch.setattr(
            "robotsix_mill.runners.pass_runner.persist_memory",
            lambda path, text: None,
        )
        monkeypatch.setattr(
            "robotsix_mill.agents.fs_tools.build_preseed_history",
            lambda repo_dir, paths, user_prompt=None: [],
        )

    # -- tools ---------------------------------------------------------

    def test_fs_tools_filtered_correctly(self, settings, repo_dir, monkeypatch):
        """Only read_file, write_file, list_dir, edit_file + explore
        are passed as tools."""
        captured_tools: list | None = None
        fake_agent = _FakeAgent(DocResult(user_facing=False, summary="no changes"))

        def fake_build(settings_, definition, *, tools=None, **overrides):
            nonlocal captured_tools
            captured_tools = tools
            return fake_agent

        _patch_build_agent_from_definition(monkeypatch, fake_build)
        monkeypatch.setattr(
            "robotsix_mill.agents.yaml_loader.load_agent_definition",
            lambda path: _make_definition(system_prompt="Doc system prompt."),
        )
        monkeypatch.setattr(
            "robotsix_mill.agents.fs_tools.build_fs_tools",
            lambda repo_dir, settings, extra_roots=None: [
                _dummy_fs_tool(n)
                for n in (
                    "read_file",
                    "write_file",
                    "list_dir",
                    "edit_file",
                    "run_command",
                    "delete_file",
                )
            ],
        )
        monkeypatch.setattr(
            "robotsix_mill.agents.explore.make_explore_tool",
            lambda settings, repo_dir, extra_roots=None, **kwargs: _dummy_fs_tool(
                "explore"
            ),
        )
        monkeypatch.setattr(
            "robotsix_mill.runners.pass_runner.load_memory",
            lambda path, max_chars=None: "",
        )

        run_doc_agent(
            settings=settings,
            repo_dir=repo_dir,
            diff=self.DIFF,
            spec=self.SPEC,
            board_id="test-board",
        )

        assert captured_tools is not None
        tool_names = {t.__name__ for t in captured_tools}
        assert tool_names == {
            "read_file",
            "write_file",
            "list_dir",
            "edit_file",
            "explore",
            "parallel_explore",
        }

    # -- memory section in system prompt -------------------------------

    def test_memory_section_when_no_ledger(self, settings, repo_dir, monkeypatch):
        """When no memory file exists, the system prompt includes the
        empty-ledger placeholder."""
        captured_system_prompt: str | None = None
        fake_agent = _FakeAgent(DocResult(user_facing=False, summary="no changes"))

        def fake_build(settings_, definition, *, tools=None, **overrides):
            nonlocal captured_system_prompt
            captured_system_prompt = overrides.get(
                "system_prompt", definition.system_prompt
            )
            return fake_agent

        _patch_build_agent_from_definition(monkeypatch, fake_build)
        monkeypatch.setattr(
            "robotsix_mill.agents.yaml_loader.load_agent_definition",
            lambda path: _make_definition(system_prompt="Doc system prompt."),
        )
        monkeypatch.setattr(
            "robotsix_mill.agents.fs_tools.build_fs_tools",
            lambda repo_dir, settings, extra_roots=None: [],
        )
        monkeypatch.setattr(
            "robotsix_mill.agents.explore.make_explore_tool",
            lambda settings, repo_dir, extra_roots=None, **kwargs: _dummy_fs_tool(
                "explore"
            ),
        )
        monkeypatch.setattr(
            "robotsix_mill.runners.pass_runner.load_memory",
            lambda path, max_chars=None: "",
        )

        run_doc_agent(
            settings=settings,
            repo_dir=repo_dir,
            diff=self.DIFF,
            spec=self.SPEC,
            board_id="test-board",
        )

        assert captured_system_prompt is not None
        assert "````memory" in captured_system_prompt
        assert "(empty — start a new ledger)" in captured_system_prompt

    def test_memory_section_with_existing_ledger(self, settings, repo_dir, monkeypatch):
        """When a memory ledger exists, its content appears in the
        system prompt's memory section."""
        captured_system_prompt: str | None = None
        fake_agent = _FakeAgent(DocResult(user_facing=False, summary="no changes"))
        existing = "docs/ lives at repo root; README covers config keys."

        def fake_build(settings_, definition, *, tools=None, **overrides):
            nonlocal captured_system_prompt
            captured_system_prompt = overrides.get(
                "system_prompt", definition.system_prompt
            )
            return fake_agent

        _patch_build_agent_from_definition(monkeypatch, fake_build)
        monkeypatch.setattr(
            "robotsix_mill.agents.yaml_loader.load_agent_definition",
            lambda path: _make_definition(system_prompt="Doc system prompt."),
        )
        monkeypatch.setattr(
            "robotsix_mill.agents.fs_tools.build_fs_tools",
            lambda repo_dir, settings, extra_roots=None: [],
        )
        monkeypatch.setattr(
            "robotsix_mill.agents.explore.make_explore_tool",
            lambda settings, repo_dir, extra_roots=None, **kwargs: _dummy_fs_tool(
                "explore"
            ),
        )
        monkeypatch.setattr(
            "robotsix_mill.runners.pass_runner.load_memory",
            lambda path, max_chars=None: existing,
        )

        run_doc_agent(
            settings=settings,
            repo_dir=repo_dir,
            diff=self.DIFF,
            spec=self.SPEC,
            board_id="test-board",
        )

        assert captured_system_prompt is not None
        assert "````memory" in captured_system_prompt
        assert existing in captured_system_prompt

    # -- user prompt sections ------------------------------------------

    def test_user_prompt_sections(self, settings, repo_dir, monkeypatch):
        """The user prompt contains ticket-spec and git-diff sections."""
        fake_agent = _FakeAgent(DocResult(user_facing=False, summary="no changes"))
        self._patch_dependencies(monkeypatch, fake_agent)

        run_doc_agent(
            settings=settings,
            repo_dir=repo_dir,
            diff=self.DIFF,
            spec=self.SPEC,
            board_id="test-board",
        )

        assert len(fake_agent.calls) == 1
        prompt, _ = fake_agent.calls[0]
        assert "````ticket-spec" in prompt
        assert self.SPEC in prompt
        assert "````git-diff" in prompt
        assert self.DIFF in prompt

    # -- reference_files → message_history -----------------------------

    def test_reference_files_triggers_message_history(
        self, settings, repo_dir, monkeypatch
    ):
        """When reference_files is provided, message_history is passed
        and run_user_prompt is None."""
        fake_agent = _FakeAgent(DocResult(user_facing=True, summary="updated docs"))
        self._patch_dependencies(monkeypatch, fake_agent)
        # Override build_preseed_history to return a non-empty history.
        preseed_val = [{"role": "system"}, {"role": "user"}]
        monkeypatch.setattr(
            "robotsix_mill.agents.fs_tools.build_preseed_history",
            lambda repo_dir, paths, user_prompt=None: preseed_val,
        )

        run_doc_agent(
            settings=settings,
            repo_dir=repo_dir,
            diff=self.DIFF,
            spec=self.SPEC,
            board_id="test-board",
            reference_files=["README.md", "src/app.py"],
        )

        assert len(fake_agent.calls) == 1
        prompt, kwargs = fake_agent.calls[0]
        assert prompt is None
        assert kwargs.get("message_history") is preseed_val
        assert "usage_limits" in kwargs

    def test_reference_files_empty_preseed_still_runs(
        self, settings, repo_dir, monkeypatch
    ):
        """When build_preseed_history returns an empty list,
        message_history is NOT set and run_user_prompt stays."""
        fake_agent = _FakeAgent(DocResult(user_facing=False, summary="no changes"))
        self._patch_dependencies(monkeypatch, fake_agent)
        # Empty preseed.
        monkeypatch.setattr(
            "robotsix_mill.agents.fs_tools.build_preseed_history",
            lambda repo_dir, paths, user_prompt=None: [],
        )

        run_doc_agent(
            settings=settings,
            repo_dir=repo_dir,
            diff=self.DIFF,
            spec=self.SPEC,
            board_id="test-board",
            reference_files=["README.md"],
        )

        prompt, kwargs = fake_agent.calls[0]
        assert prompt is not None
        assert "message_history" not in kwargs

    # -- level override -------------------------------------------------

    def test_explicit_level_overrides(self, settings, repo_dir, monkeypatch):
        """When an explicit level is passed, it appears in overrides and is
        forwarded to build_agent_from_definition."""
        captured_overrides: dict = {}
        fake_agent = _FakeAgent(DocResult(user_facing=False, summary="no changes"))

        def fake_build(settings_, definition, *, tools=None, **overrides):
            captured_overrides.update(overrides)
            return fake_agent

        _patch_build_agent_from_definition(monkeypatch, fake_build)
        monkeypatch.setattr(
            "robotsix_mill.agents.yaml_loader.load_agent_definition",
            lambda path: _make_definition(system_prompt="Doc system prompt."),
        )
        monkeypatch.setattr(
            "robotsix_mill.agents.fs_tools.build_fs_tools",
            lambda repo_dir, settings, extra_roots=None: [],
        )
        monkeypatch.setattr(
            "robotsix_mill.agents.explore.make_explore_tool",
            lambda settings, repo_dir, extra_roots=None, **kwargs: _dummy_fs_tool(
                "explore"
            ),
        )
        monkeypatch.setattr(
            "robotsix_mill.runners.pass_runner.load_memory",
            lambda path, max_chars=None: "",
        )

        run_doc_agent(
            settings=settings,
            repo_dir=repo_dir,
            diff=self.DIFF,
            spec=self.SPEC,
            board_id="test-board",
            level=3,
        )

        assert captured_overrides.get("level") == 3

    def test_level_defaults_to_definition_when_none(
        self, settings, repo_dir, monkeypatch
    ):
        """When level is None, no level override is injected — the document
        definition's own level (1) flows through unchanged."""
        captured_overrides: dict = {}
        fake_agent = _FakeAgent(DocResult(user_facing=False, summary="no changes"))

        def fake_build(settings_, definition, *, tools=None, **overrides):
            captured_overrides.update(overrides)
            return fake_agent

        _patch_build_agent_from_definition(monkeypatch, fake_build)
        monkeypatch.setattr(
            "robotsix_mill.agents.yaml_loader.load_agent_definition",
            lambda path: _make_definition(system_prompt="Doc system prompt."),
        )
        monkeypatch.setattr(
            "robotsix_mill.agents.fs_tools.build_fs_tools",
            lambda repo_dir, settings, extra_roots=None: [],
        )
        monkeypatch.setattr(
            "robotsix_mill.agents.explore.make_explore_tool",
            lambda settings, repo_dir, extra_roots=None, **kwargs: _dummy_fs_tool(
                "explore"
            ),
        )
        monkeypatch.setattr(
            "robotsix_mill.runners.pass_runner.load_memory",
            lambda path, max_chars=None: "",
        )

        run_doc_agent(
            settings=settings,
            repo_dir=repo_dir,
            diff=self.DIFF,
            spec=self.SPEC,
            board_id="test-board",
            # level not passed → None
        )

        assert "level" not in captured_overrides

    def test_definition_model_used_when_no_explicit_override(
        self, settings, repo_dir, monkeypatch
    ):
        """When definition.model is set and no explicit model_name is
        given, no override is injected (definition.model flows through)."""
        captured_overrides: dict = {}
        fake_agent = _FakeAgent(DocResult(user_facing=False, summary="no changes"))

        def fake_build(settings_, definition, *, tools=None, **overrides):
            captured_overrides.update(overrides)
            return fake_agent

        _patch_build_agent_from_definition(monkeypatch, fake_build)
        monkeypatch.setattr(
            "robotsix_mill.agents.yaml_loader.load_agent_definition",
            lambda path: _make_definition(
                system_prompt="Doc system prompt.",
                model="anthropic/claude-sonnet",
            ),
        )
        monkeypatch.setattr(
            "robotsix_mill.agents.fs_tools.build_fs_tools",
            lambda repo_dir, settings, extra_roots=None: [],
        )
        monkeypatch.setattr(
            "robotsix_mill.agents.explore.make_explore_tool",
            lambda settings, repo_dir, extra_roots=None, **kwargs: _dummy_fs_tool(
                "explore"
            ),
        )
        monkeypatch.setattr(
            "robotsix_mill.runners.pass_runner.load_memory",
            lambda path, max_chars=None: "",
        )

        run_doc_agent(
            settings=settings,
            repo_dir=repo_dir,
            diff=self.DIFF,
            spec=self.SPEC,
            board_id="test-board",
        )

        # No model_name in overrides — definition.model is left alone.
        assert "model_name" not in captured_overrides

    # -- UsageLimits ---------------------------------------------------

    def test_usage_limits_wired(self, settings, repo_dir, monkeypatch):
        """usage_limits uses settings.doc_request_limit."""
        fake_agent = _FakeAgent(DocResult(user_facing=False, summary="no changes"))
        self._patch_dependencies(monkeypatch, fake_agent)

        from pydantic_ai.usage import UsageLimits

        run_doc_agent(
            settings=settings,
            repo_dir=repo_dir,
            diff=self.DIFF,
            spec=self.SPEC,
            board_id="test-board",
        )

        _, kwargs = fake_agent.calls[0]
        limits = kwargs["usage_limits"]
        assert isinstance(limits, UsageLimits)
        assert limits.request_limit == settings.doc_request_limit

    # -- extra_roots propagation ---------------------------------------

    def test_extra_roots_propagates_to_build_fs_tools(
        self, settings, repo_dir, monkeypatch
    ):
        """extra_roots is forwarded to build_fs_tools."""
        captured: list = []
        fake_agent = _FakeAgent(DocResult(user_facing=False, summary="no changes"))

        self._patch_dependencies(monkeypatch, fake_agent)
        monkeypatch.setattr(
            "robotsix_mill.agents.fs_tools.build_fs_tools",
            lambda repo_dir, settings, extra_roots=None: (
                captured.append(extra_roots) or []
            ),
        )

        extra = [Path("/extra/root")]
        run_doc_agent(
            settings=settings,
            repo_dir=repo_dir,
            diff=self.DIFF,
            spec=self.SPEC,
            board_id="test-board",
            extra_roots=extra,
        )
        assert captured == [extra]

    def test_extra_roots_propagates_to_make_explore_tool(
        self, settings, repo_dir, monkeypatch
    ):
        """extra_roots is forwarded to make_explore_tool."""
        captured: list = []
        fake_agent = _FakeAgent(DocResult(user_facing=False, summary="no changes"))

        self._patch_dependencies(monkeypatch, fake_agent)
        monkeypatch.setattr(
            "robotsix_mill.agents.explore.make_explore_tool",
            lambda settings, repo_dir, extra_roots=None, **kwargs: (
                captured.append(extra_roots) or _dummy_fs_tool("explore")
            ),
        )

        extra = [Path("/another/extra")]
        run_doc_agent(
            settings=settings,
            repo_dir=repo_dir,
            diff=self.DIFF,
            spec=self.SPEC,
            board_id="test-board",
            extra_roots=extra,
        )
        assert captured == [extra]

    # -- board_id propagation ------------------------------------------

    def test_board_id_flows_to_memory_file_for(self, settings, repo_dir, monkeypatch):
        """board_id is forwarded to settings.memory_file_for for both
        load and persist paths."""
        load_paths: list = []
        persist_paths: list = []
        fake_agent = _FakeAgent(
            DocResult(
                user_facing=True,
                summary="docs updated",
                updated_memory="new layout",
            )
        )

        self._patch_dependencies(monkeypatch, fake_agent)
        monkeypatch.setattr(
            "robotsix_mill.runners.pass_runner.load_memory",
            lambda path, max_chars=None: load_paths.append(path) or "",
        )
        monkeypatch.setattr(
            "robotsix_mill.runners.pass_runner.persist_memory",
            lambda path, text: persist_paths.append(path),
        )

        run_doc_agent(
            settings=settings,
            repo_dir=repo_dir,
            diff=self.DIFF,
            spec=self.SPEC,
            board_id="bespoke-board-42",
        )

        expected_path = settings.memory_file_for("doc", "bespoke-board-42")
        assert len(load_paths) == 1
        assert load_paths[0] == expected_path
        assert len(persist_paths) == 1
        assert persist_paths[0] == expected_path

    def test_empty_board_id_skips_memory_operations(
        self, settings, repo_dir, monkeypatch
    ):
        """With an empty board_id, memory_file_for is NOT called (no
        ValueError), load_memory is NOT called, persist_memory is NOT
        called, and the agent still runs and returns a result."""
        load_calls: list = []
        persist_calls: list = []

        fake_agent = _FakeAgent(
            DocResult(
                user_facing=True,
                summary="docs updated",
                updated_memory="new layout",  # non-empty to exercise guard
            )
        )
        self._patch_dependencies(monkeypatch, fake_agent)

        # Spy on memory operations AFTER _patch_dependencies so the
        # recording lambdas aren't overwritten by the common no-op.
        monkeypatch.setattr(
            "robotsix_mill.runners.pass_runner.load_memory",
            lambda path, max_chars=None: load_calls.append(path) or "",
        )
        monkeypatch.setattr(
            "robotsix_mill.runners.pass_runner.persist_memory",
            lambda path, text: persist_calls.append(path),
        )

        result = run_doc_agent(
            settings=settings,
            repo_dir=repo_dir,
            diff=self.DIFF,
            spec=self.SPEC,
            board_id="",  # empty — must not raise ValueError
        )

        assert isinstance(result, DocResult)
        assert result.user_facing is True
        assert result.summary == "docs updated"

        # load_memory must NOT have been called.
        assert len(load_calls) == 0
        # persist_memory must NOT have been called, even though
        # updated_memory is non-empty.
        assert len(persist_calls) == 0

    # -- persist_memory -------------------------------------------------

    def test_persist_memory_called_when_updated_memory_non_empty(
        self,
        settings,
        repo_dir,
        monkeypatch,
    ):
        """When output.updated_memory is non-empty, persist_memory is
        called with the memory content."""
        mem_calls: list = []

        fake_agent = _FakeAgent(
            DocResult(
                user_facing=True,
                summary="updated docs",
                updated_memory="new doc layout discovered",
            )
        )
        self._patch_dependencies(monkeypatch, fake_agent)
        # Patch persist_memory AFTER _patch_dependencies so it isn't
        # overwritten by the common no-op.
        monkeypatch.setattr(
            "robotsix_mill.runners.pass_runner.persist_memory",
            lambda path, text: mem_calls.append((path, text)),
        )

        run_doc_agent(
            settings=settings,
            repo_dir=repo_dir,
            diff=self.DIFF,
            spec=self.SPEC,
            board_id="test-board",
        )

        assert len(mem_calls) == 1
        path, text = mem_calls[0]
        assert text == "new doc layout discovered"
        # Verify the path is the expected doc_memory file.
        assert path == settings.memory_file_for("doc", "test-board")

    def test_persist_memory_not_called_when_updated_memory_empty(
        self,
        settings,
        repo_dir,
        monkeypatch,
    ):
        """When output.updated_memory is empty, persist_memory is NOT called."""
        fake_agent = _FakeAgent(
            DocResult(
                user_facing=False,
                summary="no changes",
                updated_memory="",  # empty
            )
        )
        self._patch_dependencies(monkeypatch, fake_agent)
        # Patch persist_memory AFTER _patch_dependencies so the
        # recording lambda is not overwritten by the common no-op.
        mem_calls: list = []
        monkeypatch.setattr(
            "robotsix_mill.runners.pass_runner.persist_memory",
            lambda path, text: mem_calls.append((path, text)),
        )

        run_doc_agent(
            settings=settings,
            repo_dir=repo_dir,
            diff=self.DIFF,
            spec=self.SPEC,
            board_id="test-board",
        )

        assert len(mem_calls) == 0

    # -- agent close ---------------------------------------------------

    def test_agent_close_called_on_success(self, settings, repo_dir, monkeypatch):
        """agent.close() is called after a successful run."""
        fake_agent = _FakeAgent(DocResult(user_facing=False, summary="no changes"))
        self._patch_dependencies(monkeypatch, fake_agent)

        run_doc_agent(
            settings=settings,
            repo_dir=repo_dir,
            diff=self.DIFF,
            spec=self.SPEC,
            board_id="test-board",
        )
        assert fake_agent.closed is True

    def test_agent_close_called_on_error(self, settings, repo_dir, monkeypatch):
        """agent.close() is called even when run_sync raises."""
        fake_agent = _FakeAgent(DocResult(user_facing=False, summary="no changes"))
        fake_agent.run_sync = lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("model timeout")
        )
        self._patch_dependencies(monkeypatch, fake_agent)

        with pytest.raises(RuntimeError, match="model timeout"):
            run_doc_agent(
                settings=settings,
                repo_dir=repo_dir,
                diff=self.DIFF,
                spec=self.SPEC,
                board_id="test-board",
            )
        assert fake_agent.closed is True

    # -- returned DocResult --------------------------------------------

    def test_returns_doc_result(self, settings, repo_dir, monkeypatch):
        """The returned value is the fake agent's DocResult."""
        expected = DocResult(
            user_facing=True,
            summary="updated README and docs/config.md",
            updated_memory="README structure: Overview, Config, API",
        )
        fake_agent = _FakeAgent(expected)
        self._patch_dependencies(monkeypatch, fake_agent)

        result = run_doc_agent(
            settings=settings,
            repo_dir=repo_dir,
            diff=self.DIFF,
            spec=self.SPEC,
            board_id="test-board",
        )
        assert result is expected
        assert result.user_facing is True
        assert result.summary == "updated README and docs/config.md"
