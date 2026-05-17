import subprocess

import pytest

from robotsix_mill import sandbox
from robotsix_mill.agents.base import _compose_prompt, _model_id
from robotsix_mill.agents.skills import load_skills
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


# --- model id / prompt composition (no key, no pydantic_ai needed) ------

def test_model_id_online_only_when_web_and_enabled(tmp_path):
    s = _settings(tmp_path, MILL_MODEL="x/y")
    assert _model_id(s, web=False) == "openrouter:x/y"
    assert _model_id(s, web=True) == "openrouter:x/y:online"
    s2 = _settings(tmp_path, MILL_MODEL="x/y", MILL_WEB_SEARCH="false")
    assert _model_id(s2, web=True) == "openrouter:x/y"  # search disabled


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
    s = _settings(tmp_path, MILL_FETCH_IMAGE="curlimages/curl:latest")
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
