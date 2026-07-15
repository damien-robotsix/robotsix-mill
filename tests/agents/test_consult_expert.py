"""The domain expert consultation sub-agent."""

import asyncio

from robotsix_mill.agents import consult_expert
from robotsix_mill.agents.consult_expert import (
    make_consult_expert_tool,
)
from robotsix_mill.agents.consult_expert import (
    run_consult_expert as _arun_consult_expert,
)
from robotsix_mill.config import Settings, Secrets, _reset_secrets


def run_consult_expert(**kwargs):
    """Sync test shim: ``run_consult_expert`` is now a coroutine (it awaits
    the expert sub-agent's ``agent.run`` so it composes with the Claude SDK's
    running loop). Drive it to completion for these synchronous unit tests."""
    return asyncio.run(_arun_consult_expert(**kwargs))


def _settings(tmp_path, **env):
    env.setdefault("data_dir", str(tmp_path))
    key = env.get("OPENROUTER_API_KEY")
    if key is not None:
        import robotsix_mill.config as _cfg

        _reset_secrets()
        _cfg._secrets = Secrets(openrouter_api_key=key)
    # OPENROUTER_API_KEY is now a Secrets-only field; pop before Settings()
    env.pop("OPENROUTER_API_KEY", None)
    return Settings(**env)


def test_no_key_degrades_not_raises(tmp_path):
    s = _settings(tmp_path, OPENROUTER_API_KEY="")
    out = run_consult_expert(
        settings=s,
        repo_dir=tmp_path,
        domain="python-backend",
        question="where is X?",
    )
    assert "unavailable" in out and "OPENROUTER_API_KEY" in out


def test_missing_repo_degrades_not_raises(tmp_path):
    missing = tmp_path / "nonexistent"
    s = _settings(tmp_path, OPENROUTER_API_KEY="valid-key")
    out = run_consult_expert(
        settings=s,
        repo_dir=missing,
        domain="python-backend",
        question="where is X?",
    )
    assert "unavailable" in out


def test_tool_delegates_to_seam(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    seen = {}

    async def fake(*, settings, repo_dir, domain, question, board_id=""):
        seen["domain"] = domain
        seen["question"] = question
        seen["dir"] = repo_dir
        seen["board_id"] = board_id
        return f"ANSWER: {domain} -> {question}"

    monkeypatch.setattr(consult_expert, "run_consult_expert", fake)
    tool = make_consult_expert_tool(s, tmp_path)
    result = asyncio.run(tool("python-backend", "where is the Settings class defined?"))
    assert result == "ANSWER: python-backend -> where is the Settings class defined?"
    assert seen["domain"] == "python-backend"
    assert seen["question"] == "where is the Settings class defined?"
    assert seen["dir"] == tmp_path


def test_missing_expert_definition_degrades_not_raises(tmp_path, monkeypatch):
    """When the expert definition YAML file doesn't exist,
    run_consult_expert returns a failure string without raising."""
    from robotsix_mill.agents import expert_manager
    from robotsix_mill.agents.expert_loader import (
        ExpertMemoryConfig,
        ExpertDefinition,
    )

    s = _settings(tmp_path, OPENROUTER_API_KEY="valid-key")

    # Patch load_definitions to return a known dict that lacks
    # 'nonexistent' — mimics a missing YAML file.
    def fake_load_defs(self, definitions_dir=None):
        return {
            "python-backend": ExpertDefinition(
                domain="python-backend",
                module_paths=["src/**/*.py"],
                system_prompt="You are a Python expert.",
                level=2,
                memory=ExpertMemoryConfig(max_memory_chars=8000),
                tools=["explore", "read_file", "list_dir"],
            ),
        }

    monkeypatch.setattr(
        expert_manager.ExpertManager,
        "load_definitions",
        fake_load_defs,
    )

    out = run_consult_expert(
        settings=s,
        repo_dir=tmp_path,
        domain="nonexistent",
        question="where is X?",
    )
    assert "nonexistent" in out
    assert "no expert definition found" in out


def test_expert_agent_read_only_tools(tmp_path, monkeypatch):
    """The expert agent built inside run_consult_expert has ONLY
    explore, read_file, list_dir — no edit_file, write_file,
    run_command, delete_file."""
    from robotsix_mill.agents import expert_manager
    from robotsix_mill.agents.expert_loader import (
        ExpertMemoryConfig,
        ExpertDefinition,
    )

    s = _settings(
        tmp_path,
        OPENROUTER_API_KEY="k",
        consult_request_limit="5",
    )
    cap = {}

    class FakeAgent:
        def __init__(self, **kw):
            cap["tools"] = sorted(t.__name__ for t in kw.get("tools", []))
            cap["name"] = kw.get("name")
            cap["output_type"] = kw.get("output_type")

        async def run(self, prompt, **kw):
            class R:
                output = "expert answer"

            return R()

    def fake_load_defs(self, definitions_dir=None):
        return {
            "python-backend": ExpertDefinition(
                domain="python-backend",
                module_paths=["src/**/*.py"],
                system_prompt="You are a Python expert.",
                level=2,
                memory=ExpertMemoryConfig(max_memory_chars=8000),
                tools=["explore", "read_file", "list_dir"],
            ),
        }

    monkeypatch.setattr(
        expert_manager.ExpertManager,
        "load_definitions",
        fake_load_defs,
    )
    import pydantic_ai
    from robotsix_mill.agents import base as bmod

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    # The level→model seam: build_openrouter_model returns (model, client).
    monkeypatch.setattr(
        bmod,
        "build_openrouter_model",
        lambda level=1, *, online=False: (object(), object()),
    )
    # Prevent the fs_tools build from trying to access a real repo
    from robotsix_mill.agents import fs_tools as ft

    monkeypatch.setattr(ft, "build_fs_tools", lambda root, settings, **kw: [])

    out = run_consult_expert(
        settings=s,
        repo_dir=tmp_path,
        domain="python-backend",
        question="where is X?",
        board_id="test-board",
    )
    assert out == "expert answer"
    assert cap["name"] == "consult:python-backend"
    # Only read-only tools — no mutation tools.
    for banned in ("edit_file", "write_file", "run_command", "delete_file"):
        assert banned not in cap["tools"], f"{banned} should not be in expert tools"
    # Output is structured so the expert can return both an answer and
    # an updated memory ledger; the wrapper unwraps .answer to a string.
    from pydantic_ai import PromptedOutput
    from robotsix_mill.agents.consult_expert import ExpertConsultResult

    assert isinstance(cap["output_type"], PromptedOutput)
    # Underlying type must be ExpertConsultResult.
    assert ExpertConsultResult in (
        cap["output_type"].outputs
        if isinstance(cap["output_type"].outputs, tuple)
        else (cap["output_type"].outputs,)
    )


def test_expert_persists_updated_memory(tmp_path, monkeypatch):
    """When the expert returns ``updated_memory``, ``run_consult_expert``
    writes it to ``<data_dir>/<board>/expert_<domain>_memory.md``."""
    from robotsix_mill.agents import expert_manager
    from robotsix_mill.agents.consult_expert import ExpertConsultResult
    from robotsix_mill.agents.expert_loader import (
        ExpertMemoryConfig,
        ExpertDefinition,
    )

    s = _settings(tmp_path, OPENROUTER_API_KEY="k")

    class FakeAgent:
        def __init__(self, **kw):
            pass

        async def run(self, prompt, **kw):
            class R:
                output = ExpertConsultResult(
                    answer="here is the answer",
                    updated_memory="## What I learned\n- ticket-42: X uses Y\n",
                )

            return R()

    def fake_load_defs(self, definitions_dir=None):
        return {
            "python-backend": ExpertDefinition(
                domain="python-backend",
                module_paths=["src/**/*.py"],
                system_prompt="You are a Python expert.",
                level=2,
                memory=ExpertMemoryConfig(max_memory_chars=8000),
                tools=["explore", "read_file", "list_dir"],
            ),
        }

    monkeypatch.setattr(
        expert_manager.ExpertManager, "load_definitions", fake_load_defs
    )
    import pydantic_ai
    from robotsix_mill.agents import fs_tools as ft
    from robotsix_mill.agents import base as bmod

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(
        bmod,
        "build_openrouter_model",
        lambda level=1, *, online=False: (object(), object()),
    )
    monkeypatch.setattr(ft, "build_fs_tools", lambda root, settings, **kw: [])

    out = run_consult_expert(
        settings=s,
        repo_dir=tmp_path,
        domain="python-backend",
        question="where is X?",
        board_id="myboard",
    )
    # Answer is returned to the coordinator.
    assert out == "here is the answer"
    # Memory was persisted to the expected per-board path.
    memory_file = s.memory_file_for("expert_python-backend", "myboard")
    assert memory_file.exists()
    assert "ticket-42: X uses Y" in memory_file.read_text(encoding="utf-8")


def test_expert_persist_memory_receives_max_chars_kwarg(tmp_path, monkeypatch):
    """run_consult_expert passes max_chars=definition.memory.max_memory_chars
    to persist_memory, capping the written memory ledger."""
    from robotsix_mill.agents import expert_manager
    from robotsix_mill.agents.consult_expert import ExpertConsultResult
    from robotsix_mill.agents.expert_loader import (
        ExpertMemoryConfig,
        ExpertDefinition,
    )
    from robotsix_mill.runners import pass_runner

    s = _settings(tmp_path, OPENROUTER_API_KEY="k")

    class FakeAgent:
        def __init__(self, **kw):
            pass

        async def run(self, prompt, **kw):
            class R:
                output = ExpertConsultResult(
                    answer="here is the answer",
                    updated_memory="## What I learned\n- ticket-42: X uses Y\n",
                )

            return R()

    def fake_load_defs(self, definitions_dir=None):
        return {
            "python-backend": ExpertDefinition(
                domain="python-backend",
                module_paths=["src/**/*.py"],
                system_prompt="You are a Python expert.",
                level=2,
                memory=ExpertMemoryConfig(max_memory_chars=8000),
                tools=["explore", "read_file", "list_dir"],
            ),
        }

    monkeypatch.setattr(
        expert_manager.ExpertManager, "load_definitions", fake_load_defs
    )
    import pydantic_ai
    from robotsix_mill.agents import fs_tools as ft, base as bmod

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(
        bmod,
        "build_openrouter_model",
        lambda level=1, *, online=False: (object(), object()),
    )
    monkeypatch.setattr(ft, "build_fs_tools", lambda root, settings, **kw: [])

    # Capture the kwargs passed to persist_memory.
    persist_kwargs = {}

    def fake_persist_memory(memory_file, text, **kwargs):
        persist_kwargs["file"] = memory_file
        persist_kwargs["text"] = text
        persist_kwargs["kwargs"] = kwargs

    monkeypatch.setattr(pass_runner, "persist_memory", fake_persist_memory)

    out = run_consult_expert(
        settings=s,
        repo_dir=tmp_path,
        domain="python-backend",
        question="where is X?",
        board_id="myboard",
    )
    assert out == "here is the answer"
    assert persist_kwargs["kwargs"].get("max_chars") == 8000


def test_expert_skips_persist_when_updated_memory_empty(tmp_path, monkeypatch):
    """When ``updated_memory`` is empty/whitespace, the memory file is
    NOT created — preserves any existing ledger as-is."""
    from robotsix_mill.agents import expert_manager
    from robotsix_mill.agents.consult_expert import ExpertConsultResult
    from robotsix_mill.agents.expert_loader import (
        ExpertMemoryConfig,
        ExpertDefinition,
    )

    s = _settings(tmp_path, OPENROUTER_API_KEY="k")

    class FakeAgent:
        def __init__(self, **kw):
            pass

        async def run(self, prompt, **kw):
            class R:
                output = ExpertConsultResult(answer="ok", updated_memory="")

            return R()

    monkeypatch.setattr(
        expert_manager.ExpertManager,
        "load_definitions",
        lambda self, definitions_dir=None: {
            "python-backend": ExpertDefinition(
                domain="python-backend",
                module_paths=["src/**/*.py"],
                system_prompt="P",
                level=2,
                memory=ExpertMemoryConfig(max_memory_chars=8000),
                tools=["explore", "read_file", "list_dir"],
            ),
        },
    )
    import pydantic_ai
    from robotsix_mill.agents import fs_tools as ft, base as bmod

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(
        bmod,
        "build_openrouter_model",
        lambda level=1, *, online=False: (object(), object()),
    )
    monkeypatch.setattr(ft, "build_fs_tools", lambda root, settings, **kw: [])

    out = run_consult_expert(
        settings=s,
        repo_dir=tmp_path,
        domain="python-backend",
        question="?",
        board_id="b",
    )
    assert out == "ok"
    memory_file = s.memory_file_for("expert_python-backend", "b")
    assert not memory_file.exists()


def test_tool_registry_registration(tmp_path):
    """make_consult_expert_tool registers the tool in ToolRegistry."""
    from robotsix_mill.agents.tool_registry import ToolRegistry

    s = _settings(tmp_path)
    make_consult_expert_tool(s, tmp_path)
    tools = ToolRegistry.list_tools()
    consult = [t for t in tools if t.name == "consult_expert"]
    assert len(consult) == 1
    assert consult[0].category == "exploration"
    assert "domain" in consult[0].parameters
    assert "question" in consult[0].parameters
