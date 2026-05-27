"""run_dedup_check (dd73). Shipped with no tests and a broken
pydantic-ai call (request_limit passed as a bare run_sync kwarg ->
UserError every time -> dedup silently dead). These lock the contract.
"""

from robotsix_mill.agents import dedup
from robotsix_mill.agents.dedup import DedupResult


class _Result:
    def __init__(self, output):
        self.output = output


class _FakeAgent:
    def __init__(self):
        self.calls = []

    def run_sync(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        return _Result(
            DedupResult(duplicate_of="T-1", already_done=None, reason="dup")
        )


def _patch_agent(monkeypatch, agent):
    monkeypatch.setattr(
        "robotsix_mill.agents.base.build_agent", lambda *a, **k: agent
    )


def test_passes_usage_limits_not_bare_request_limit(settings, monkeypatch):
    """Regression: must use usage_limits=UsageLimits(...), never a bare
    request_limit= kwarg (the latter raised UserError on every call)."""
    from pydantic_ai.usage import UsageLimits

    agent = _FakeAgent()
    _patch_agent(monkeypatch, agent)

    out = dedup.run_dedup_check(
        settings=settings, draft_title="t", draft_body="b",
        candidates_json="[]",
    )
    assert out["duplicate_of"] == "T-1"  # real result, not the failure path
    assert len(agent.calls) == 1
    _, kwargs = agent.calls[0]
    assert "request_limit" not in kwargs
    assert isinstance(kwargs.get("usage_limits"), UsageLimits)


def test_graceful_on_agent_error(settings, monkeypatch):
    """Any exception → null verdict + 'dedup check failed' (never
    blocks refine), but it must be the EXCEPTION path, not the bug."""
    class _Boom:
        def run_sync(self, *a, **k):
            raise RuntimeError("model down")

    _patch_agent(monkeypatch, _Boom())
    out = dedup.run_dedup_check(
        settings=settings, draft_title="t", draft_body="b",
        candidates_json="[]",
    )
    assert out == {
        "duplicate_of": None,
        "already_done": None,
        "reason": "dedup check failed",
    }


def test_non_dedup_result_output_is_handled(settings, monkeypatch):
    class _Weird:
        def run_sync(self, *a, **k):
            return _Result("not a DedupResult")

    _patch_agent(monkeypatch, _Weird())
    out = dedup.run_dedup_check(
        settings=settings, draft_title="t", draft_body="b",
        candidates_json="[]",
    )
    assert out["duplicate_of"] is None
    assert "unexpected type" in out["reason"]


def test_fs_tools_passed_when_repo_dir_provided(settings, monkeypatch):
    """When repo_dir is set, build_agent receives read_file+list_dir
    fs tools; when None, no tools are passed."""

    # Controlled fs mocks with __name__
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

    monkeypatch.setattr(
        "robotsix_mill.agents.base.build_agent", _capture_agent
    )

    # Case 1: repo_dir provided → fs tools should be filtered in
    from pathlib import Path

    dedup.run_dedup_check(
        settings=settings, draft_title="t", draft_body="b",
        candidates_json="[]",
        repo_dir=Path("/fake/repo"),
    )
    assert captured_tools is not None
    tool_names = {t.__name__ for t in captured_tools}
    assert tool_names == {"read_file", "list_dir"}

    # Case 2: repo_dir=None → no fs tools
    captured_tools = None
    dedup.run_dedup_check(
        settings=settings, draft_title="t", draft_body="b",
        candidates_json="[]",
        repo_dir=None,
    )
    assert captured_tools == []
