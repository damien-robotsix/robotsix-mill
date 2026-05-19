"""run_dedup_check (dd73). Shipped with no tests and a broken
pydantic-ai call (request_limit passed as a bare run_sync kwarg ->
UserError every time -> dedup silently dead). These lock the contract.
"""

from robotsix_mill.agents import dedup


class _Result:
    def __init__(self, output):
        self.output = output


class _FakeAgent:
    def __init__(self):
        self.calls = []

    def run_sync(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        return _Result(
            {"duplicate_of": "T-1", "already_done": None, "reason": "dup"}
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
        candidates_json="[]", recent_commits_json=None,
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
        candidates_json="[]", recent_commits_json=None,
    )
    assert out == {
        "duplicate_of": None,
        "already_done": None,
        "reason": "dedup check failed",
    }


def test_non_dict_output_is_handled(settings, monkeypatch):
    class _Weird:
        def run_sync(self, *a, **k):
            return _Result("not a dict")

    _patch_agent(monkeypatch, _Weird())
    out = dedup.run_dedup_check(
        settings=settings, draft_title="t", draft_body="b",
        candidates_json="[]", recent_commits_json=None,
    )
    assert out["duplicate_of"] is None
    assert "unexpected type" in out["reason"]
