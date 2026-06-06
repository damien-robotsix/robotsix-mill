"""Regression test for the meta-agent's ``build_agent`` wiring.

The existing ``tests/test_meta_runner.py`` mocks ``run_meta_agent``
wholesale, so the *real* ``build_agent(...)`` call inside
``run_meta_agent`` was never exercised by the suite.  That blind spot
let a kwarg-name bug ship: the call passed ``web=False`` while
``build_agent`` only accepts ``web_knowledge`` — so the meta-agent threw
``TypeError`` at construction on *every* run and never filed a draft.

This test routes the meta-agent's kwargs through the *real*
``build_agent`` signature (via ``Signature.bind``) so any future
kwarg-name drift fails loudly, while stubbing the tool builders and the
model run so it stays hermetic and offline.
"""

from __future__ import annotations

import inspect
import subprocess

from robotsix_mill.agents import base as base_mod
from robotsix_mill.agents import explore as explore_mod
from robotsix_mill.agents import fs_tools as fs_tools_mod
from robotsix_mill.meta.agent import SYSTEM_PROMPT, MetaAgentResult, run_meta_agent
from robotsix_mill.config import Settings


def test_prompt_covers_adoption_check_contract():
    """The live system prompt (loaded from meta.yaml at import) must teach
    the ADOPTION-CHECK dimension: verify that DONE/CLOSED proposals were
    actually adopted, distinguishing extraction (consumer must depend on /
    import the lib) from alignment, and file `migrate` follow-ups."""
    p = SYSTEM_PROMPT.lower()
    assert "adopt" in p
    assert "done" in p
    assert "closed" in p
    assert "consume" in p
    assert "import" in p
    assert "migrate" in p


class _StubResult:
    def __init__(self) -> None:
        self.output = MetaAgentResult(updated_memory="ledger")


class _StubAgent:
    def run_sync(self, prompt):  # noqa: ANN001 — test stub
        return _StubResult()


def test_meta_build_agent_kwargs_match_real_signature(tmp_path, monkeypatch):
    """``run_meta_agent`` must call ``build_agent`` with kwargs the real
    signature accepts.  An invalid kwarg (e.g. ``web=`` vs
    ``web_knowledge=``) raises ``TypeError`` here, exactly as in prod."""
    real_sig = inspect.signature(base_mod.build_agent)
    captured: dict = {}

    def _spy_build_agent(settings, **kwargs):
        # Reproduces the prod failure mode: bind against the *real*
        # signature so an unknown/renamed kwarg raises TypeError.
        real_sig.bind(settings, **kwargs)
        captured.update(kwargs)
        return _StubAgent()

    # Patch on the base module — meta.py does a late
    # ``from .base import build_agent`` inside run_meta_agent.
    monkeypatch.setattr(base_mod, "build_agent", _spy_build_agent)
    monkeypatch.setattr(base_mod, "_safe_close", lambda _agent: None)
    # Keep tool construction hermetic and offline.
    monkeypatch.setattr(
        explore_mod, "make_repo_scoped_explore_tool", lambda *a, **k: lambda **kw: None
    )
    monkeypatch.setattr(fs_tools_mod, "build_fs_tools", lambda *a, **k: [])

    settings = Settings(data_dir=str(tmp_path / "data"))
    result = run_meta_agent(
        settings=settings,
        memory="",
        recent_proposals="",
        repo_clones={"repo-a": tmp_path},
    )

    assert isinstance(result, MetaAgentResult)
    assert result.updated_memory == "ledger"
    # The fix: build_agent must have received web_knowledge, never web.
    assert "web_knowledge" in captured
    assert "web" not in captured


class _CapturingAgent:
    """Agent stub that records the prompt it was run with."""

    def __init__(self) -> None:
        self.prompt: str | None = None

    def run_sync(self, prompt):  # noqa: ANN001 — test stub
        self.prompt = prompt
        return _StubResult()


def test_prompt_injects_outstanding_todos_section(tmp_path, monkeypatch):
    """``run_meta_agent`` must inject the deterministic ``outstanding-todos``
    section with the scanned markers into the prompt."""
    clone = tmp_path / "repo"
    clone.mkdir()
    subprocess.run(["git", "init", "-q", str(clone)], check=True)
    (clone / "mod.py").write_text("x = 1  # TODO: wire this up\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(clone), "add", "-A"], check=True)

    captured = _CapturingAgent()
    monkeypatch.setattr(base_mod, "build_agent", lambda *a, **k: captured)
    monkeypatch.setattr(base_mod, "_safe_close", lambda _agent: None)
    monkeypatch.setattr(
        explore_mod, "make_repo_scoped_explore_tool", lambda *a, **k: lambda **kw: None
    )
    monkeypatch.setattr(fs_tools_mod, "build_fs_tools", lambda *a, **k: [])

    settings = Settings(data_dir=str(tmp_path / "data"))
    run_meta_agent(
        settings=settings,
        memory="",
        recent_proposals="",
        repo_clones={"repo-a": clone},
    )

    assert captured.prompt is not None
    # The section fence (rendered by ``section("outstanding-todos", ...)``).
    assert "````outstanding-todos" in captured.prompt
    # The scanned marker is present — discovery is no longer the model's job.
    assert "[TODO]" in captured.prompt
    assert "TODO: wire this up" in captured.prompt


def test_empty_repo_clones_early_return(tmp_path, monkeypatch):
    """With no clones, ``run_meta_agent`` returns early (echoing memory) and
    never builds the agent or runs a scan."""

    def _boom(*a, **k):
        raise AssertionError("build_agent must not be called for empty clones")

    monkeypatch.setattr(base_mod, "build_agent", _boom)

    settings = Settings(data_dir=str(tmp_path / "data"))
    result = run_meta_agent(
        settings=settings,
        memory="prior ledger",
        recent_proposals="",
        repo_clones={},
    )

    assert isinstance(result, MetaAgentResult)
    assert result.updated_memory == "prior ledger"
    assert result.extraction_drafts == []
    assert result.alignment_drafts == []
    assert result.todo_drafts == []
