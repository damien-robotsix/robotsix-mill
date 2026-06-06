"""run_obsolescence_check — pre-refine obsolescence gate seam.

Mirrors tests/agents/test_dedup.py: locks the best-effort contract
(usage_limits, graceful degradation, fs-tool filtering) by
monkeypatching ``robotsix_mill.agents.base.build_agent``.
"""

from robotsix_mill.agents import obsolescence
from robotsix_mill.agents.obsolescence import ObsolescenceResult


class _Result:
    def __init__(self, output):
        self.output = output


class _FakeAgent:
    def __init__(self):
        self.calls = []

    def run_sync(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        return _Result(ObsolescenceResult(obsolete=True, reason="doc section present"))


def _patch_agent(monkeypatch, agent):
    monkeypatch.setattr("robotsix_mill.agents.base.build_agent", lambda *a, **k: agent)


def test_passes_usage_limits_not_bare_request_limit(settings, monkeypatch):
    """Must use usage_limits=UsageLimits(...), never a bare
    request_limit= kwarg (the latter raises UserError on every call)."""
    from pydantic_ai.usage import UsageLimits

    agent = _FakeAgent()
    _patch_agent(monkeypatch, agent)

    out = obsolescence.run_obsolescence_check(
        settings=settings,
        draft_title="t",
        draft_body="b",
        repo_dir=None,
    )
    assert out["obsolete"] is True
    assert out["reason"] == "doc section present"
    assert len(agent.calls) == 1
    _, kwargs = agent.calls[0]
    assert "request_limit" not in kwargs
    assert isinstance(kwargs.get("usage_limits"), UsageLimits)


def test_graceful_on_agent_error(settings, monkeypatch):
    """Any exception → obsolete=False + 'obsolescence check failed'
    (never blocks refine)."""

    class _Boom:
        def run_sync(self, *a, **k):
            raise RuntimeError("model down")

    _patch_agent(monkeypatch, _Boom())
    out = obsolescence.run_obsolescence_check(
        settings=settings,
        draft_title="t",
        draft_body="b",
        repo_dir=None,
    )
    assert out == {
        "obsolete": False,
        "reason": "obsolescence check failed",
    }


def test_non_result_output_is_handled(settings, monkeypatch):
    class _Weird:
        def run_sync(self, *a, **k):
            return _Result("not an ObsolescenceResult")

    _patch_agent(monkeypatch, _Weird())
    out = obsolescence.run_obsolescence_check(
        settings=settings,
        draft_title="t",
        draft_body="b",
        repo_dir=None,
    )
    assert out["obsolete"] is False
    assert "unexpected type" in out["reason"]


def test_fs_tools_passed_when_repo_dir_provided(settings, monkeypatch):
    """When repo_dir is set, build_agent receives read_file+list_dir
    fs tools; when None, no tools are passed."""

    def _read_file(*a, **k):
        pass

    _read_file.__name__ = "read_file"

    def _list_dir(*a, **k):
        pass

    _list_dir.__name__ = "list_dir"

    def _write_file(*a, **k):
        pass

    _write_file.__name__ = "write_file"

    fs_mocks = [_read_file, _write_file, _list_dir]

    monkeypatch.setattr(
        "robotsix_mill.agents.fs_tools.build_fs_tools",
        lambda root, s: fs_mocks,
    )

    captured_tools: list | None = None

    def _capture_agent(*a, tools=None, **k):
        nonlocal captured_tools
        captured_tools = tools
        return _FakeAgent()

    monkeypatch.setattr("robotsix_mill.agents.base.build_agent", _capture_agent)

    from pathlib import Path

    # Case 1: repo_dir provided → fs tools should be filtered in
    obsolescence.run_obsolescence_check(
        settings=settings,
        draft_title="t",
        draft_body="b",
        repo_dir=Path("/fake/repo"),
    )
    assert captured_tools is not None
    tool_names = {t.__name__ for t in captured_tools}
    assert tool_names == {"read_file", "list_dir"}

    # Case 2: repo_dir=None → no fs tools
    captured_tools = None
    obsolescence.run_obsolescence_check(
        settings=settings,
        draft_title="t",
        draft_body="b",
        repo_dir=None,
    )
    assert captured_tools == []
