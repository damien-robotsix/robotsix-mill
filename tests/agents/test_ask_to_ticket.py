"""Tests for the ask-to-ticket agent — run_ask_to_ticket_agent seam."""

from robotsix_mill.agents.ask_to_ticket import (
    AskToTicketResult,
    run_ask_to_ticket_agent,
)
from robotsix_mill.config import Settings, Secrets, _reset_secrets


def _settings(tmp_path, **env):
    env.setdefault("data_dir", str(tmp_path))
    key = env.get("OPENROUTER_API_KEY")
    if key is not None:
        import robotsix_mill.config as _cfg

        _reset_secrets()
        _cfg._secrets = Secrets(openrouter_api_key=key)
    return Settings(**env)


def test_run_ask_to_ticket_agent_without_repo_dir(tmp_path, monkeypatch):
    """Without repo_dir: no explore/fs tools, structured result returned."""
    from robotsix_mill.agents import base as bmod
    from robotsix_mill.agents import retry as rmod

    s = _settings(tmp_path, OPENROUTER_API_KEY="k")

    cap = {}

    def fake_build_agent(settings, definition, tools, model_name, output_type):
        cap["tools"] = sorted(t.__name__ for t in tools)
        cap["model"] = model_name
        cap["output_type"] = output_type

        class FakeAgent:
            def run_sync(self, prompt):
                cap["prompt"] = prompt

                class R:
                    output = AskToTicketResult(title="T", description="D")

                return R()

            def close(self):
                pass

        return FakeAgent()

    def fake_retry(agent, make_run, *, settings, what):
        cap["what"] = what
        return make_run(agent)

    monkeypatch.setattr(bmod, "build_agent_from_definition", fake_build_agent)
    monkeypatch.setattr(rmod, "run_agent", fake_retry)

    result = run_ask_to_ticket_agent(settings=s, question="Q", answer="A", comment="C")
    assert isinstance(result, AskToTicketResult)
    assert result.title == "T"
    assert result.description == "D"
    assert cap["output_type"] is AskToTicketResult
    assert cap["model"] == s.ask_to_ticket_model
    assert cap["what"] == "ask_to_ticket"
    # No repo tools when repo_dir is None.
    assert cap["tools"] == []
    # Inputs are delimited in the prompt.
    assert "<question>" in cap["prompt"] and "Q" in cap["prompt"]
    assert "<answer>" in cap["prompt"] and "A" in cap["prompt"]
    assert "<comment>" in cap["prompt"] and "C" in cap["prompt"]


def test_run_ask_to_ticket_agent_with_repo_dir(tmp_path, monkeypatch):
    """With repo_dir: explore + read-only fs tools, no write tools."""
    from robotsix_mill.agents import base as bmod
    from robotsix_mill.agents import retry as rmod

    (tmp_path / "a.txt").write_text("hi")
    s = _settings(tmp_path, OPENROUTER_API_KEY="k")

    cap = {}

    def fake_build_agent(settings, definition, tools, model_name, output_type):
        cap["tools"] = sorted(t.__name__ for t in tools)

        class FakeAgent:
            def run_sync(self, prompt):
                class R:
                    output = AskToTicketResult(title="T", description="D")

                return R()

            def close(self):
                pass

        return FakeAgent()

    def fake_retry(agent, make_run, *, settings, what):
        return make_run(agent)

    monkeypatch.setattr(bmod, "build_agent_from_definition", fake_build_agent)
    monkeypatch.setattr(rmod, "run_agent", fake_retry)

    result = run_ask_to_ticket_agent(
        settings=s, question="Q", answer="A", comment="C", repo_dir=tmp_path
    )
    assert isinstance(result, AskToTicketResult)
    assert "explore" in cap["tools"]
    assert "read_file" in cap["tools"]
    assert "list_dir" in cap["tools"]
    assert "run_command" in cap["tools"]
    for banned in ("edit_file", "write_file", "delete_file"):
        assert banned not in cap["tools"], f"{banned} must not be present"


def test_run_ask_to_ticket_agent_runtime_error_on_missing_api_key(
    tmp_path, monkeypatch
):
    """Missing OpenRouter API key propagates as RuntimeError."""
    from robotsix_mill.agents import base as bmod

    s = _settings(tmp_path, OPENROUTER_API_KEY="")

    def fake_build_agent(settings, definition, tools, model_name, output_type):
        raise RuntimeError("OPENROUTER_API_KEY is required")

    monkeypatch.setattr(bmod, "build_agent_from_definition", fake_build_agent)

    import pytest

    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        run_ask_to_ticket_agent(settings=s, question="Q", answer="A", comment="C")
