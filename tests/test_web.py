import subprocess

import pytest

from robotsix_mill import sandbox
from robotsix_mill.agents import web_research as wr
from robotsix_mill.agents.base import _compose_prompt, _model_name
from robotsix_mill.agents.skills import load_skills
from robotsix_mill.agents.web_research import make_web_research_tool
from robotsix_mill.agents.web_tools import make_web_fetch
from robotsix_mill.config import Settings


def _settings(tmp_path, **env):
    env.setdefault("MILL_DATA_DIR", str(tmp_path))
    return Settings(**env)


# --- skills loader ------------------------------------------------------

def test_load_skills_from_dir(tmp_path):
    sk = tmp_path / "skills" / "demo"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text(
        "---\nname: Demo\ndescription: does demo things\n"
        "when_to_use: when demoing\n---\n# Body\nstep one\n"
    )
    out = load_skills(tmp_path / "skills")
    assert "# Skills" in out
    assert "Skill: Demo" in out
    assert "does demo things" in out
    assert "When to use:" in out and "when demoing" in out
    assert "step one" in out


def test_load_skills_missing_dir_is_empty(tmp_path):
    assert load_skills(tmp_path / "nope") == ""


def test_repo_ships_web_skills():
    # the real skills/ dir in the repo has the two web skills
    out = load_skills("skills")
    assert "Web Fetch" in out and "Web Search" in out


# --- agent_references/ folder shipped with the repo ---------------------

def test_repo_ships_agent_references():
    """The real agent_references/ dir is committed and discoverable, so
    the implement agent can read entries when AGENT.md points it there.
    No auto-loading — agents pull on demand."""
    from pathlib import Path
    p = Path("agent_references")
    assert p.is_dir(), "agent_references/ folder is missing from the repo"
    files = sorted(x.name for x in p.glob("*.md"))
    assert "sqlalchemy-sqlite.md" in files, files


# --- model id / prompt composition (no key, no pydantic_ai needed) ------

def test_main_model_never_online(tmp_path):
    # The expensive main agent must never carry ":online" — web search
    # is delegated to the cheap sub-agent. (No "openrouter:" prefix: the
    # provider is set explicitly for the cost-instrumented model.)
    s = _settings(tmp_path, MILL_MODEL="x/y")
    assert _model_name(s) == "x/y"
    assert ":online" not in _model_name(s)
    # even with web search enabled (the default) the main model is plain
    s2 = _settings(tmp_path, MILL_MODEL="x/y", MILL_WEB_SEARCH="true")
    assert _model_name(s2) == "x/y"


# --- web research sub-agent (the cost fix) ------------------------------

def test_web_research_tool_delegates_to_seam(tmp_path, monkeypatch):
    """The tool exposed to the main agent must return EXACTLY the
    sub-agent seam's conclusion string (no raw pages leak through)."""
    s = _settings(tmp_path)
    seen = {}

    def fake(*, settings, query):
        seen["query"] = query
        return f"CONCLUSION about {query}"

    monkeypatch.setattr(wr, "run_web_research", fake)
    tool = make_web_research_tool(s)
    assert tool("python 3.14 release date") == (
        "CONCLUSION about python 3.14 release date"
    )
    assert seen["query"] == "python 3.14 release date"


def test_web_research_no_key_degrades(tmp_path):
    # No OPENROUTER_API_KEY -> a short message, never an exception.
    s = _settings(tmp_path, OPENROUTER_API_KEY="")
    out = wr.run_web_research(settings=s, query="anything")
    assert "unavailable" in out and "OPENROUTER_API_KEY" in out


def test_web_research_subagent_uses_cheap_online_model(tmp_path, monkeypatch):
    """The sub-agent (and only it) builds the cheap model WITH ":online"
    and bounds itself by web_research_request_limit."""
    s = _settings(
        tmp_path,
        OPENROUTER_API_KEY="k",
        MILL_WEB_RESEARCH_MODEL="cheap/mini",
        MILL_WEB_RESEARCH_REQUEST_LIMIT="5",
    )
    captured = {}

    class FakeModel:
        def __init__(self, name, **kw):
            captured["model"] = name

    class FakeAgent:
        def __init__(self, **kw):
            captured["name"] = kw.get("name")

        def run_sync(self, query, *, usage_limits=None):
            captured["limit"] = usage_limits.request_limit
            captured["query"] = query
            return type("R", (), {"output": "ok"})()

    import pydantic_ai
    import pydantic_ai.providers.openrouter as orp
    from robotsix_mill.agents import openrouter_cost as oc

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())
    monkeypatch.setattr(oc, "CostInstrumentedOpenRouterModel", FakeModel)

    out = wr.run_web_research(settings=s, query="q")
    assert out == "ok"
    assert captured["model"] == "cheap/mini:online"
    assert captured["limit"] == 5
    assert captured["name"] == "web_research"


def test_compose_prompt_appends_skills(tmp_path):
    s = _settings(tmp_path, MILL_SKILLS_DIR="skills")
    p = _compose_prompt(s, "BASE PROMPT")
    assert p.startswith("BASE PROMPT")
    assert "Web Fetch" in p


# --- web_fetch tool -----------------------------------------------------

def test_web_fetch_tool_uses_sandbox_fetch(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setattr(
        sandbox, "fetch", lambda url, *, settings: (0, f"BODY:{url}")
    )
    wf = make_web_fetch(s)
    assert wf("https://x.test/doc") == "BODY:https://x.test/doc"


def test_web_fetch_tool_reports_errors(tmp_path, monkeypatch):
    s = _settings(tmp_path)

    def boom(url, *, settings):
        raise sandbox.SandboxError("no docker")

    monkeypatch.setattr(sandbox, "fetch", boom)
    assert "fetch error: no docker" in make_web_fetch(s)("https://x.test")


# --- sandbox.fetch container argv (no daemon needed) --------------------

def test_fetch_rejects_non_http(tmp_path):
    s = _settings(tmp_path)
    rc, msg = sandbox.fetch("file:///etc/passwd", settings=s)
    assert rc == 1 and "only http(s)" in msg


def test_fetch_argv_is_locked_down(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="hello", stderr="")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    rc, body = sandbox.fetch("https://docs.example/api", settings=s)

    a = seen["argv"]
    assert rc == 0 and body == "hello"
    assert a[:3] == ["docker", "run", "--rm"]
    assert "--read-only" in a
    assert a[a.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges" in a
    assert "-v" not in a  # NO repo/data mount in the fetch container
    assert a[-2:] == ["--", "https://docs.example/api"]  # URL is argv, not shell


def test_fetch_image_config_default():
    """fetch_image defaults to a pinned version, not a mutable tag."""
    s = Settings()
    assert s.fetch_image == "curlimages/curl:8.17.0"
