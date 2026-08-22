"""Shared fixtures for tests/agents/."""

import pytest

from robotsix_mill.agents import dedup, refining
from robotsix_mill.agents.refining import (
    ChildSpec,
    RefineResult,
    TriageResult,
)
from robotsix_mill.stages import StageContext


def _single(spec: str, file_map=None) -> RefineResult:
    """Shorthand for a single-scope refine result."""
    return RefineResult(split=False, spec_markdown=spec, file_map=file_map)


def _split(*children: dict, file_map=None) -> RefineResult:
    """Shorthand for a split refine result."""
    return RefineResult(
        split=True,
        children=[
            ChildSpec(
                title=c["title"],
                spec_markdown=c["spec_markdown"],
                depends_on=c.get("depends_on", []),
            )
            for c in children
        ],
        file_map=file_map,
    )


def _install_refine_spy(
    monkeypatch,
    spec="## Problem\nx\n## Acceptance criteria\n- [ ] works\n",
):
    """Install a ``run_refine_agent`` spy and return a dict whose
    ``["called"]`` flips to ``True`` once the refine agent runs.

    Lets the dedup-target-validation tests assert that refine proceeds
    (rather than the dedup guard short-circuiting to DONE) without
    re-declaring the full keyword signature in every test.
    """
    state = {"called": False}

    def spy(
        *,
        settings,
        title,
        draft,
        repo_dir=None,
        reviewer_comments=None,
        memory="",
        epic_context="",
        extra_roots=None,
        message_history=None,
        board_id="",
        **kwargs,
    ):
        state["called"] = True
        return _single(spec)

    monkeypatch.setattr(refining, "run_refine_agent", spy)
    return state


@pytest.fixture(autouse=True)
def _dedup_clean(monkeypatch):
    """All pre-existing tests expect the dedup guard to be a no-op
    (novel draft).  Dedup-specific tests override this fixture."""
    monkeypatch.setattr(
        dedup,
        "run_dedup_check",
        lambda **_: {"duplicate_of": None, "already_done": None, "reason": "no match"},
    )


# Saved before the autouse fixture replaces it — tests that need the real
# triage_refine (e.g. prompt-construction tests) restore it from this ref.
_original_triage_refine = refining.triage_refine


@pytest.fixture(autouse=True)
def _triage_refine_ok(monkeypatch):
    """All pre-existing tests expect triage to pass through to refine.
    Tests that need a different triage outcome override this fixture."""
    monkeypatch.setattr(
        refining,
        "triage_refine",
        lambda *a, **kw: TriageResult(decision="REFINE", reason="test"),
    )


@pytest.fixture
def ctx(settings, service, repo_config):
    return StageContext(settings=settings, service=service, repo_config=repo_config)
