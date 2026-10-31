"""Shared fakes for the split tests/agents/test_refine*.py modules."""

from robotsix_mill.agents.refining import RefineResult


class _FakeRunResult:
    """Minimal fake for a pydantic-ai AgentRunResult, exposing only the
    attributes that the continuation guard reads."""

    def __init__(
        self,
        *,
        output,
        finish_reason,
        all_messages,
        all_messages_json=b"[]",
        new_messages_json=b"[]",
        usage=None,
    ):
        self._output = output
        self._all_messages = all_messages
        self._all_messages_json = all_messages_json
        self._new_messages_json = new_messages_json
        self.response = (
            _FakeResponse(finish_reason) if finish_reason is not None else None
        )
        self.usage = usage if usage is not None else _FakeUsage()

    @property
    def output(self):
        return self._output

    def all_messages(self):
        return self._all_messages

    def all_messages_json(self):
        return self._all_messages_json

    def new_messages_json(self):
        return self._new_messages_json


class _FakeUsage:
    """Minimal fake for pydantic_ai.usage.RunUsage."""

    def __init__(self, requests: int = 0):
        self.requests = requests

    def __call__(self):
        return self


class _FakeResponse:
    def __init__(self, finish_reason):
        self.finish_reason = finish_reason


def _simple_agent():
    """Return a mock agent that returns a trivial RefineResult."""

    class FakeResult:
        output = RefineResult(spec_markdown="ok")
        response = type("R", (), {"finish_reason": "stop"})()

        def all_messages_json(self):
            return b"[]"

        def new_messages_json(self):
            return b"[]"

    class FakeAgent:
        def run_sync(self, prompt, *, message_history=None, usage_limits=None):
            return FakeResult()

    return FakeAgent()
