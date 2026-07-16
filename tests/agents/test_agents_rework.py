"""The implement agent + test sub-agent: the main agent reads/edits
itself, with a concise `explore` scout and a distilling `run_tests`
sub-agent (no implement sub-agent, no deep layer)."""

import pydantic_ai
import pytest

from robotsix_mill.agents import coordinating, testing
from robotsix_mill.agents import base as bmod
from robotsix_mill.agents.coordinating import ImplementResult, ValidationResult
from robotsix_mill.config import Settings


def _settings(tmp_path, **env):
    env.setdefault("data_dir", str(tmp_path))
    env.setdefault("OPENROUTER_API_KEY", "k")
    # Populate Secrets so get_secrets() returns matching values
    from robotsix_mill.config import Secrets, _reset_secrets
    import robotsix_mill.config as _cfg

    _reset_secrets()
    _cfg._secrets = Secrets(openrouter_api_key=env.get("OPENROUTER_API_KEY", "k"))
    # OPENROUTER_API_KEY is now a Secrets-only field; pop before Settings()
    env.pop("OPENROUTER_API_KEY", None)
    return Settings(**env)


@pytest.fixture
def fake_ai(monkeypatch):
    cap = {}

    class FakeModel:
        def __init__(self, name, **kw):
            cap["model"] = name

    class FakeAgent:
        def __init__(self, **kw):
            cap["tools"] = sorted(t.__name__ for t in (kw.get("tools") or []))
            cap["name"] = kw.get("name")

        def run_sync(self, prompt, *, usage_limits=None, **kw):
            cap["limit"] = getattr(usage_limits, "request_limit", None)
            return type("R", (), {"output": ImplementResult(summary="did it")})()

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(
        bmod,
        "new_deepseek_model",
        lambda model_name, level: (FakeModel(model_name), object()),
    )
    return cap


def test_implement_agent_reads_and_edits_itself(tmp_path, fake_ai):
    """The main agent uses MILL_MODEL and gets explore (scout) + its
    OWN fs tools (incl. run_command for focused diagnosis) +
    ask_web_knowledge (single gateway to the internet). There is NO
    run_tests tool — the implement stage owns the test→retry→escalate
    loop and runs the suite itself."""
    s = _settings(
        tmp_path,
        coordinator_request_limit="9",
    )
    out = coordinating.run_coordinator(
        settings=s, repo_dir=tmp_path, spec="build a thing"
    )
    assert out.summary == "did it"
    # implement.yaml declares level 2 → DeepSeek pro via llmio tier defaults.
    assert fake_ai["model"] == "deepseek/deepseek-v4-pro"
    assert fake_ai["limit"] == 9
    assert fake_ai["tools"] == [
        "ask_user",
        "ask_web_knowledge",
        "consult_expert",
        "delete_file",
        "edit_file",
        "explore",
        "insert_changelog_entry",
        "list_dir",
        "list_threads",
        "parallel_explore",
        "post_comment",
        "read_file",
        "read_ticket",
        "reply_to_thread",
        "report_issue",
        "run_command",
        "spawn_subtask",
        "write_file",
    ]
    assert fake_ai["name"] == "implement"


def test_explore_scout_prompt_forbids_whole_files():
    from robotsix_mill.agents.explore import _SYSTEM_PROMPT

    assert "NEVER paste whole files" in _SYSTEM_PROMPT
    assert "FILE:" not in _SYSTEM_PROMPT  # the old dump-file directive is gone

    # scope-discipline guardrails (ticket: explore-scope-guardrails)
    assert "at most 5 files" in _SYSTEM_PROMPT.lower()
    assert "do not trace full call chains" in _SYSTEM_PROMPT.lower()

    # external/installed-dependency paths are outside the sandbox and must
    # not be read or grepped; ask_web_knowledge (parent) is the alternative
    assert "site-packages" in _SYSTEM_PROMPT
    assert "ask_web_knowledge" in _SYSTEM_PROMPT


def test_test_agent_pass(tmp_path, monkeypatch):
    from robotsix_mill import sandbox

    s = _settings(tmp_path, test_command="pytest")
    monkeypatch.setattr(
        sandbox,
        "run",
        lambda cmd, *, repo_dir, settings, **kwargs: (0, "ok"),
    )
    passed, fb = testing.run_test_agent(settings=s, repo_dir=tmp_path)
    assert passed is True and "passed" in fb


def test_test_agent_no_tests_collected_passes(tmp_path, monkeypatch):
    """pytest rc=5 ('no tests ran') is NOT a failure — a freshly-scaffolded
    repo with an empty tests/ dir must not poison its baseline check."""
    from robotsix_mill import sandbox

    s = _settings(tmp_path, test_command="pytest")
    monkeypatch.setattr(
        sandbox,
        "run",
        lambda cmd, *, repo_dir, settings, **kwargs: (5, "no tests ran in 0.00s"),
    )
    passed, fb = testing.run_test_agent(settings=s, repo_dir=tmp_path)
    assert passed is True
    assert "no tests collected" in fb


def test_test_agent_rc5_with_real_failure_still_fails(tmp_path, monkeypatch):
    """rc=5 WITHOUT the pytest no-tests marker is not auto-passed."""
    from robotsix_mill import sandbox

    s = _settings(tmp_path, test_command="pytest")
    monkeypatch.setattr(
        sandbox,
        "run",
        lambda cmd, *, repo_dir, settings, **kwargs: (5, "INTERNALERROR boom"),
    )
    # No openrouter key → returns the raw-tail failure path (passed False).
    monkeypatch.setattr(
        "robotsix_mill.agents.testing.get_secrets",
        lambda: type("S", (), {"openrouter_api_key": ""})(),
    )
    passed, fb = testing.run_test_agent(settings=s, repo_dir=tmp_path)
    assert passed is False


def test_test_agent_retry_on_failure_flaky_first_run_passes(tmp_path, monkeypatch):
    """retry_on_failure: a red first run + green re-run is a PASS (flaky) —
    the baseline gate must not fabricate "pre-existing test failures on
    main" from one flaky test (live case: ticket a74b)."""
    from robotsix_mill import sandbox

    s = _settings(tmp_path, test_command="pytest")
    calls: list[int] = []

    def _flaky_run(cmd, *, repo_dir, settings, **kwargs):
        calls.append(1)
        return (1, "1 failed") if len(calls) == 1 else (0, "all green")

    monkeypatch.setattr(sandbox, "run", _flaky_run)
    passed, fb = testing.run_test_agent(
        settings=s, repo_dir=tmp_path, retry_on_failure=True
    )
    assert passed is True
    assert "flaky" in fb
    assert len(calls) == 2


def test_test_agent_retry_on_failure_still_red_distills_second_output(
    tmp_path, monkeypatch
):
    """Both runs red → failure path runs on the SECOND run's output."""
    from robotsix_mill import sandbox

    s = _settings(tmp_path, test_command="pytest")
    calls: list[int] = []

    def _red_run(cmd, *, repo_dir, settings, **kwargs):
        calls.append(1)
        return (1, f"failure output run {len(calls)}")

    monkeypatch.setattr(sandbox, "run", _red_run)
    monkeypatch.setattr(
        "robotsix_mill.agents.testing.get_secrets",
        lambda: type("S", (), {"openrouter_api_key": ""})(),
    )
    passed, fb = testing.run_test_agent(
        settings=s, repo_dir=tmp_path, retry_on_failure=True
    )
    assert passed is False
    assert "failure output run 2" in fb
    assert len(calls) == 2


def test_test_agent_no_retry_by_default(tmp_path, monkeypatch):
    """Without retry_on_failure the suite runs exactly once — the implement
    fix loop must not pay a double suite run on every red gate."""
    from robotsix_mill import sandbox

    s = _settings(tmp_path, test_command="pytest")
    calls: list[int] = []

    def _red_run(cmd, *, repo_dir, settings, **kwargs):
        calls.append(1)
        return (1, "red")

    monkeypatch.setattr(sandbox, "run", _red_run)
    monkeypatch.setattr(
        "robotsix_mill.agents.testing.get_secrets",
        lambda: type("S", (), {"openrouter_api_key": ""})(),
    )
    passed, fb = testing.run_test_agent(settings=s, repo_dir=tmp_path)
    assert passed is False
    assert len(calls) == 1


def test_test_agent_repo_file_command_wins(tmp_path, monkeypatch):
    """The repo's own ``.robotsix-mill/config.yaml`` ``test_command`` is the
    highest-precedence source: it overrides ``settings.test_command`` (the
    global fallback) and is the command actually handed to ``sandbox.run``.
    (``repo_config`` no longer carries a per-repo ``test_command``.)"""
    from robotsix_mill import sandbox

    cfg_dir = tmp_path / ".robotsix-mill"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text(
        'test_command: "repo-file-cmd"\n', encoding="utf-8"
    )

    s = _settings(tmp_path, test_command="settings-cmd")

    cap = {}

    def fake_run(cmd, *, repo_dir, settings, **kwargs):
        cap["cmd"] = cmd
        return (0, "ok")

    monkeypatch.setattr(sandbox, "run", fake_run)

    passed, fb = testing.run_test_agent(settings=s, repo_dir=tmp_path)
    assert passed is True
    assert cap["cmd"] == "repo-file-cmd"


def _repo_config(**overrides):
    from robotsix_mill.config import RepoConfig

    base = dict(
        repo_id="r",
        board_id="b",
        langfuse_project_name="p",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
    )
    base.update(overrides)
    return RepoConfig(**base)


def test_test_agent_forwards_repo_sandbox_image(tmp_path, monkeypatch):
    """``run_test_agent`` threads ``repo_config.sandbox_image`` into the
    underlying ``sandbox.run`` call; ``None`` when no config / unset."""
    from robotsix_mill import sandbox

    s = _settings(tmp_path, test_command="pytest -q")
    cap = {}

    def fake_run(cmd, *, repo_dir, settings, **kwargs):
        cap["sandbox_image"] = kwargs.get("sandbox_image")
        return (0, "ok")

    monkeypatch.setattr(sandbox, "run", fake_run)

    cfg = _repo_config(sandbox_image="ros:rolling-ros-base")
    testing.run_test_agent(settings=s, repo_dir=tmp_path, repo_config=cfg)
    assert cap["sandbox_image"] == "ros:rolling-ros-base"

    # repo_config=None → None.
    testing.run_test_agent(settings=s, repo_dir=tmp_path, repo_config=None)
    assert cap["sandbox_image"] is None

    # repo_config with sandbox_image unset → None.
    testing.run_test_agent(settings=s, repo_dir=tmp_path, repo_config=_repo_config())
    assert cap["sandbox_image"] is None


# --- smoke gate -----------------------------------------------------------


def test_smoke_agent_empty_command_short_circuits_pass(tmp_path, monkeypatch):
    """No smoke command set anywhere → PASS short-circuit (opt-in)."""
    from robotsix_mill import sandbox

    s = _settings(tmp_path, smoke_command="")

    def _boom(*a, **k):  # must NOT be called when no command is set
        raise AssertionError("sandbox.run must not run with no smoke command")

    monkeypatch.setattr(sandbox, "run", _boom)
    passed, fb = testing.run_smoke_agent(settings=s, repo_dir=tmp_path)
    assert passed is True
    assert "no smoke gate configured" in fb


def test_smoke_agent_pass(tmp_path, monkeypatch):
    from robotsix_mill import sandbox

    s = _settings(tmp_path, smoke_command="scripts/smoke.sh")
    monkeypatch.setattr(
        sandbox,
        "run",
        lambda cmd, *, repo_dir, settings, **kwargs: (0, "ok"),
    )
    passed, fb = testing.run_smoke_agent(settings=s, repo_dir=tmp_path)
    assert passed is True and "smoke passed" in fb


def test_smoke_agent_repo_file_command_wins(tmp_path, monkeypatch):
    """The repo's ``.robotsix-mill/config.yaml`` ``smoke_command`` overrides
    ``settings.smoke_command`` and is the command handed to ``sandbox.run``."""
    from robotsix_mill import sandbox

    cfg_dir = tmp_path / ".robotsix-mill"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text(
        'smoke_command: "repo-smoke-cmd"\n', encoding="utf-8"
    )

    s = _settings(tmp_path, smoke_command="settings-smoke-cmd")
    cap = {}

    def fake_run(cmd, *, repo_dir, settings, **kwargs):
        cap["cmd"] = cmd
        return (0, "ok")

    monkeypatch.setattr(sandbox, "run", fake_run)
    passed, fb = testing.run_smoke_agent(settings=s, repo_dir=tmp_path)
    assert passed is True
    assert cap["cmd"] == "repo-smoke-cmd"


def test_smoke_agent_forwards_repo_sandbox_image(tmp_path, monkeypatch):
    """``run_smoke_agent`` threads ``repo_config.sandbox_image`` into the
    underlying ``sandbox.run`` call; ``None`` when no config / unset."""
    from robotsix_mill import sandbox

    s = _settings(tmp_path, smoke_command="scripts/smoke.sh")
    cap = {}

    def fake_run(cmd, *, repo_dir, settings, **kwargs):
        cap["sandbox_image"] = kwargs.get("sandbox_image")
        return (0, "ok")

    monkeypatch.setattr(sandbox, "run", fake_run)

    cfg = _repo_config(sandbox_image="ros:rolling-ros-base")
    testing.run_smoke_agent(settings=s, repo_dir=tmp_path, repo_config=cfg)
    assert cap["sandbox_image"] == "ros:rolling-ros-base"

    testing.run_smoke_agent(settings=s, repo_dir=tmp_path, repo_config=None)
    assert cap["sandbox_image"] is None


def test_smoke_agent_sandbox_unavailable(tmp_path, monkeypatch):
    from robotsix_mill import sandbox

    s = _settings(tmp_path, smoke_command="scripts/smoke.sh")

    def _raise(*a, **k):
        raise sandbox.SandboxError("docker down")

    monkeypatch.setattr(sandbox, "run", _raise)
    passed, fb = testing.run_smoke_agent(settings=s, repo_dir=tmp_path)
    assert passed is False
    assert fb.startswith("sandbox unavailable")


def test_smoke_paths_match_empty_list_is_unconditional():
    assert testing.smoke_paths_match(["src/foo/bar.py"], []) is True
    assert testing.smoke_paths_match([], []) is True


def test_smoke_paths_match_glob_match():
    changed = ["src/robotsix_mill/runtime/board_html.py"]
    assert testing.smoke_paths_match(changed, ["src/robotsix_mill/runtime/**"]) is True
    # Shallow extension glob.
    css = ["src/robotsix_mill/runtime/static/board.css"]
    assert (
        testing.smoke_paths_match(css, ["src/robotsix_mill/runtime/static/*.css"])
        is True
    )


def test_smoke_paths_match_non_match():
    changed = ["src/robotsix_mill/stages/implement.py"]
    assert testing.smoke_paths_match(changed, ["src/robotsix_mill/runtime/**"]) is False


def test_test_agent_fail_distills_via_cheap_model(tmp_path, monkeypatch):
    from robotsix_mill import sandbox

    s = _settings(
        tmp_path,
        test_command="pytest",
    )
    monkeypatch.setattr(
        sandbox,
        "run",
        lambda cmd, *, repo_dir, settings, **kwargs: (
            1,
            "E   assert 1 == 2\n" * 50,
        ),
    )
    cap = {}

    class FakeModel:
        def __init__(self, name, **kw):
            cap["model"] = name

    class FakeAgent:
        def __init__(self, **kw):
            cap["name"] = kw.get("name")
            cap["tools"] = sorted(t.__name__ for t in (kw.get("tools") or []))

        def run_sync(self, prompt, *, usage_limits=None, **kw):
            cap["got_output"] = "assert 1 == 2" in prompt
            return type("R", (), {"output": "fix the assertion in foo.py"})()

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(
        bmod,
        "new_deepseek_model",
        lambda model_name, level: (FakeModel(model_name), object()),
    )

    passed, fb = testing.run_test_agent(settings=s, repo_dir=tmp_path)
    assert passed is False
    assert fb == "fix the assertion in foo.py"  # distilled, not raw log
    # run_tests.yaml declares level 2 → DeepSeek pro via llmio tier defaults.
    assert cap["model"] == "deepseek/deepseek-v4-pro" and cap["got_output"]
    assert cap["name"] == "run_tests"

    # AC4: run_tests agent has read-only diagnostic tools
    assert "read_file" in cap["tools"]
    assert "list_dir" in cap["tools"]
    assert "run_command" in cap["tools"]
    assert "explore" in cap["tools"]
    assert "report_issue" in cap["tools"]
    assert "write_file" not in cap["tools"]
    assert "edit_file" not in cap["tools"]
    assert "delete_file" not in cap["tools"]


def test_test_agent_no_command_is_pass(tmp_path):
    s = _settings(tmp_path, test_command="")
    passed, fb = testing.run_test_agent(settings=s, repo_dir=tmp_path)
    assert passed is True


def test_test_agent_env_error_rc127_dash_signature(tmp_path, monkeypatch):
    """rc=127 + dash 'sh: 1: <bin>: not found' → deterministic ENV-ERROR
    diagnosis naming the binary, WITHOUT invoking the distill LLM."""
    from robotsix_mill import sandbox
    from robotsix_mill.agents import retry

    s = _settings(tmp_path, test_command="yamllint --strict .")
    monkeypatch.setattr(
        sandbox,
        "run",
        lambda cmd, *, repo_dir, settings, **kwargs: (
            127,
            "sh: 1: yamllint: not found",
        ),
    )

    def _boom(*a, **k):
        raise AssertionError("distill LLM must not run for an ENV-ERROR")

    monkeypatch.setattr(retry, "run_agent", _boom)

    passed, fb = testing.run_test_agent(settings=s, repo_dir=tmp_path)
    assert passed is False
    assert fb.startswith(testing.ENV_ERROR_PREFIX)
    assert "yamllint" in fb


def test_test_agent_env_error_bash_command_not_found(tmp_path, monkeypatch):
    """bash '<bin>: command not found' signature (even rc≠127) → ENV-ERROR
    naming the binary, no distill LLM."""
    from robotsix_mill import sandbox
    from robotsix_mill.agents import retry

    s = _settings(tmp_path, test_command="shellcheck script.sh")
    monkeypatch.setattr(
        sandbox,
        "run",
        lambda cmd, *, repo_dir, settings, **kwargs: (
            1,
            "shellcheck: command not found",
        ),
    )

    def _boom(*a, **k):
        raise AssertionError("distill LLM must not run for an ENV-ERROR")

    monkeypatch.setattr(retry, "run_agent", _boom)

    passed, fb = testing.run_test_agent(settings=s, repo_dir=tmp_path)
    assert passed is False
    assert fb.startswith(testing.ENV_ERROR_PREFIX)
    assert "shellcheck" in fb


def test_test_agent_env_error_rc126_noexec_permission_denied(tmp_path, monkeypatch):
    """rc=126 + Permission-denied on a $HOME/.local/bin path → ENV-ERROR
    (a pip --user console script blocked by a noexec tmpfs), no distill LLM."""
    from robotsix_mill import sandbox
    from robotsix_mill.agents import retry

    s = _settings(tmp_path, test_command="yamllint --strict .")
    monkeypatch.setattr(
        sandbox,
        "run",
        lambda cmd, *, repo_dir, settings, **kwargs: (
            126,
            "sh: 1: /tmp/.local/bin/yamllint: Permission denied",
        ),
    )

    def _boom(*a, **k):
        raise AssertionError("distill LLM must not run for an ENV-ERROR")

    monkeypatch.setattr(retry, "run_agent", _boom)

    passed, fb = testing.run_test_agent(settings=s, repo_dir=tmp_path)
    assert passed is False
    assert fb.startswith(testing.ENV_ERROR_PREFIX)
    assert "/tmp/.local/bin/yamllint" in fb


def test_test_agent_rc126_unrelated_path_still_distills(tmp_path, monkeypatch):
    """rc=126 Permission-denied on a NON-HOME path (a repo script bug) must
    NOT match the noexec signature — it flows to the distill agent."""
    from robotsix_mill import sandbox

    s = _settings(tmp_path, test_command="./run.sh")
    monkeypatch.setattr(
        sandbox,
        "run",
        lambda cmd, *, repo_dir, settings, **kwargs: (
            126,
            "sh: 1: ./run.sh: Permission denied",
        ),
    )

    class FakeModel:
        def __init__(self, *a, **k):
            pass

    class FakeAgent:
        def __init__(self, *a, **k):
            pass

        def run_sync(self, *a, **k):
            return type("R", (), {"output": "distilled: bad perms"})()

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(
        bmod,
        "new_deepseek_model",
        lambda model_name, level: (FakeModel(model_name), object()),
    )

    passed, fb = testing.run_test_agent(settings=s, repo_dir=tmp_path)
    assert passed is False
    assert not fb.startswith(testing.ENV_ERROR_PREFIX)


def test_test_agent_normal_failure_still_distills(tmp_path, monkeypatch):
    """A normal assertion failure (rc=1, no command-not-found signature)
    must still flow to the distill agent — NOT the ENV-ERROR path."""
    from robotsix_mill import sandbox

    s = _settings(tmp_path, test_command="pytest")
    monkeypatch.setattr(
        sandbox,
        "run",
        lambda cmd, *, repo_dir, settings, **kwargs: (1, "AssertionError: 1 != 2"),
    )

    class FakeModel:
        def __init__(self, *a, **k):
            pass

    class FakeAgent:
        def __init__(self, *a, **k):
            pass

        def run_sync(self, *a, **k):
            return type("R", (), {"output": "distilled: assertion failed"})()

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(
        bmod,
        "new_deepseek_model",
        lambda model_name, level: (FakeModel(model_name), object()),
    )

    passed, fb = testing.run_test_agent(settings=s, repo_dir=tmp_path)
    assert passed is False
    assert not fb.startswith(testing.ENV_ERROR_PREFIX)
    assert "distilled" in fb


def test_build_agent_forwards_name(tmp_path, monkeypatch):
    """AC1: build_agent(..., name='test-agent') passes name= to Agent."""
    from robotsix_mill.agents import base as base_mod

    cap = {}

    class FakeModel:
        def __init__(self, name, **kw):
            pass

    class FakeAgent:
        def __init__(self, **kw):
            cap["name"] = kw.get("name")

        def run_sync(self, prompt, *, usage_limits=None, **kw):
            return type("R", (), {"output": "ok"})()

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(
        bmod,
        "new_deepseek_model",
        lambda model_name, level: (FakeModel(model_name), object()),
    )

    s = _settings(tmp_path)
    base_mod.build_agent(
        s,
        system_prompt="test",
        name="test-agent",
    )
    assert cap["name"] == "test-agent"


def test_build_agent_does_not_inject_tool_prose_into_prompt(tmp_path, monkeypatch):
    """The agent's system_prompt is the YAML body verbatim — no prose
    tool list is appended. pydantic-ai forwards each closure's
    signature + docstring as the model API's structured ``tools``
    array; a Markdown copy in the prompt would be pure duplication.

    Replaces the previous AC3 test that asserted owned tools appeared
    in the prompt but unowned ones didn't — the contract changed when
    we deduped the tool surface."""
    from robotsix_mill.agents.base import build_agent
    from robotsix_mill.agents.tool_registry import ToolInfo, ToolRegistry

    s = _settings(tmp_path)

    ToolRegistry.register(
        ToolInfo(
            name="write_file",
            description="Write a file.",
            category="fs",
            parameters={"path": "str", "content": "str"},
        )
    )

    def dummy_tool():
        """A dummy tool."""
        pass

    cap = {}

    class FakeModel:
        def __init__(self, name, **kw):
            pass

    class FakeAgent:
        def __init__(self, **kw):
            cap["system_prompt"] = kw.get("system_prompt", "")

        def run_sync(self, prompt, *, usage_limits=None, **kw):
            return type("R", (), {"output": "ok"})()

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(
        bmod,
        "new_deepseek_model",
        lambda model_name, level: (FakeModel(model_name), object()),
    )

    agent = build_agent(
        s,
        system_prompt="test prompt",
        tools=[dummy_tool],
    )
    agent.close()

    # The prompt is the YAML body verbatim — no prose tool table.
    assert "## Available tools" not in cap["system_prompt"]
    # No tool names leak into the prompt body.
    assert "dummy_tool" not in cap["system_prompt"]
    assert "write_file" not in cap["system_prompt"]


def test_build_agent_without_name_is_compatible(tmp_path, monkeypatch):
    """AC2: build_agent(...) without name= still works; Agent receives
    no name kwarg (or None)."""
    from robotsix_mill.agents import base as base_mod

    cap = {}

    class FakeModel:
        def __init__(self, name, **kw):
            pass

    class FakeAgent:
        def __init__(self, **kw):
            cap["name"] = kw.get("name")

        def run_sync(self, prompt, *, usage_limits=None, **kw):
            return type("R", (), {"output": "ok"})()

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(
        bmod,
        "new_deepseek_model",
        lambda model_name, level: (FakeModel(model_name), object()),
    )

    s = _settings(tmp_path)
    base_mod.build_agent(
        s,
        system_prompt="test",
    )
    assert cap["name"] is None


def test_audit_agent_tool_set(tmp_path, monkeypatch):
    """AC: audit agent gets explore, list_dir, read_file, run_command,
    and ask_web_knowledge — the single gateway to web lookups."""
    from robotsix_mill.agents import auditing

    cap = {}

    class FakeModel:
        def __init__(self, name, **kw):
            pass

    class FakeAgent:
        def __init__(self, **kw):
            cap["tools"] = sorted(t.__name__ for t in (kw.get("tools") or []))

        def run_sync(self, prompt, *, usage_limits=None, **kw):
            from robotsix_mill.agents.auditing import AuditResult

            return type(
                "R",
                (),
                {
                    "output": AuditResult(
                        draft_ticket_titles=[],
                        draft_ticket_bodies=[],
                        gap_ids=[],
                        updated_memory="",
                    )
                },
            )()

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(
        bmod,
        "new_deepseek_model",
        lambda model_name, level: (FakeModel(model_name), object()),
    )

    s = _settings(tmp_path)
    auditing.run_audit_agent(settings=s, repo_dir=tmp_path, memory="")

    assert cap["tools"] == [
        "ask_user",
        "ask_web_knowledge",
        "audit_workflow_callers_tool",
        "detect_duplication",
        "explore",
        "list_dir",
        "parallel_explore",
        "read_file",
        "read_ticket",
        "run_command",
        "validate_artifact",
        "write_file",
    ]


def test_validation_result_decide_proceed():
    """A passing gate routes to proceed regardless of iteration count."""
    vr = ValidationResult.decide(
        passed=True,
        iterations=1,
        max_iters=8,
        feedback="",
    )
    assert vr.passed is True
    assert vr.next_action == "proceed"
    assert vr.failure_summary == ""
    assert vr.iterations_used == 1


def test_validation_result_decide_retry():
    """A failing gate with attempts remaining routes to retry and
    carries the diagnosis as failure_summary."""
    vr = ValidationResult.decide(
        passed=False,
        iterations=1,
        max_iters=8,
        feedback="boom in test_x",
    )
    assert vr.passed is False
    assert vr.next_action == "retry"
    assert vr.failure_summary == "boom in test_x"
    assert vr.iterations_used == 1


def test_validation_result_decide_escalate():
    """A failing gate on the last allowed attempt routes to escalate —
    no LLM involvement, the bound is enforced here."""
    vr = ValidationResult.decide(
        passed=False,
        iterations=3,
        max_iters=3,
        feedback="still broken",
    )
    assert vr.next_action == "escalate"
    assert vr.passed is False
    assert vr.failure_summary == "still broken"
    assert vr.iterations_used == 3


def test_test_agent_distill_injects_file_map_scope(tmp_path, monkeypatch):
    """When artifacts/file_map.json exists alongside repo_dir, the distill
    sub-agent's user message includes a 'Declared file scope' block listing
    the in-scope file paths (soft hint)."""
    from robotsix_mill import sandbox

    s = _settings(tmp_path, test_command="pytest")

    # Write file_map.json at repo_dir.parent / "artifacts" / "file_map.json"
    artifacts_dir = tmp_path.parent / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    import json as _json

    artifacts_dir.joinpath("file_map.json").write_text(
        _json.dumps(
            [
                {"file": "tests/cli/test_config.py"},
                {"file": "src/robotsix_mill/config.py"},
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        sandbox,
        "run",
        lambda cmd, *, repo_dir, settings, **kwargs: (
            1,
            "E   assert 1 == 2\n" * 50,
        ),
    )
    cap = {}

    class FakeModel:
        def __init__(self, name, **kw):
            cap["model"] = name

    class FakeAgent:
        def __init__(self, **kw):
            cap["name"] = kw.get("name")

        def run_sync(self, prompt, *, usage_limits=None, **kw):
            cap["prompt"] = prompt
            return type("R", (), {"output": "fix the assertion"})()

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(
        bmod,
        "new_deepseek_model",
        lambda model_name, level: (FakeModel(model_name), object()),
    )

    try:
        passed, fb = testing.run_test_agent(settings=s, repo_dir=tmp_path)
        assert passed is False
        assert fb == "fix the assertion"

        prompt = cap.get("prompt", "")
        assert "Declared file scope (prefer fixes within these files):" in prompt
        assert "  - tests/cli/test_config.py" in prompt
        assert "  - src/robotsix_mill/config.py" in prompt
    finally:
        # Clean up to avoid leaking into the next test (tmp_path.parent is
        # shared across tests in the same session).
        fp = artifacts_dir / "file_map.json"
        if fp.exists():
            fp.unlink()
        if artifacts_dir.exists():
            artifacts_dir.rmdir()


def test_test_agent_distill_no_file_map_unaffected(tmp_path, monkeypatch):
    """When artifacts/file_map.json does NOT exist, the user message sent
    to the distill sub-agent is unchanged — no scope block appended."""
    from robotsix_mill import sandbox

    # Ensure no leftover artifacts from other tests.
    artifacts_dir = tmp_path.parent / "artifacts"
    fp = artifacts_dir / "file_map.json"
    if fp.exists():
        fp.unlink()

    s = _settings(tmp_path, test_command="pytest")

    monkeypatch.setattr(
        sandbox,
        "run",
        lambda cmd, *, repo_dir, settings, **kwargs: (
            1,
            "E   assert 1 == 2\n" * 50,
        ),
    )
    cap = {}

    class FakeModel:
        def __init__(self, name, **kw):
            cap["model"] = name

    class FakeAgent:
        def __init__(self, **kw):
            pass

        def run_sync(self, prompt, *, usage_limits=None, **kw):
            cap["prompt"] = prompt
            return type("R", (), {"output": "fix the assertion"})()

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(
        bmod,
        "new_deepseek_model",
        lambda model_name, level: (FakeModel(model_name), object()),
    )

    passed, fb = testing.run_test_agent(settings=s, repo_dir=tmp_path)
    assert passed is False
    assert fb == "fix the assertion"

    prompt = cap.get("prompt", "")
    assert "Declared file scope" not in prompt


def test_test_agent_distill_explicit_file_map_override(tmp_path, monkeypatch):
    """Passing file_map= explicitly bypasses auto-discovery; the explicit
    list appears in the scope block regardless of artifacts/file_map.json."""
    from robotsix_mill import sandbox

    s = _settings(tmp_path, test_command="pytest")

    # Write a DIFFERENT file_map.json (should be ignored when explicit passed)
    artifacts_dir = tmp_path.parent / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    import json as _json

    artifacts_dir.joinpath("file_map.json").write_text(
        _json.dumps([{"file": "should/be/ignored.py"}]),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        sandbox,
        "run",
        lambda cmd, *, repo_dir, settings, **kwargs: (
            1,
            "E   assert 1 == 2\n" * 50,
        ),
    )
    cap = {}

    class FakeModel:
        def __init__(self, name, **kw):
            pass

    class FakeAgent:
        def __init__(self, **kw):
            pass

        def run_sync(self, prompt, *, usage_limits=None, **kw):
            cap["prompt"] = prompt
            return type("R", (), {"output": "fix the assertion"})()

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(
        bmod,
        "new_deepseek_model",
        lambda model_name, level: (FakeModel(model_name), object()),
    )

    try:
        explicit_map = ["only/this/file.py"]
        passed, fb = testing.run_test_agent(
            settings=s,
            repo_dir=tmp_path,
            file_map=explicit_map,
        )
        assert passed is False

        prompt = cap.get("prompt", "")
        assert "Declared file scope (prefer fixes within these files):" in prompt
        assert "  - only/this/file.py" in prompt
        assert "should/be/ignored.py" not in prompt
    finally:
        fp = artifacts_dir / "file_map.json"
        if fp.exists():
            fp.unlink()
        if artifacts_dir.exists():
            artifacts_dir.rmdir()
