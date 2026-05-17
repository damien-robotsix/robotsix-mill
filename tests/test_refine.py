import hashlib

import pytest

from robotsix_mill.agents import refining
from robotsix_mill.core.states import State
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.refine import RefineStage
from robotsix_mill.runtime.worker import process_ticket


@pytest.fixture
def ctx(settings, service):
    return StageContext(settings=settings, service=service)


def test_empty_draft_blocks(ctx, service):
    t = service.create("x", "   ")
    out = RefineStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "empty draft" in out.note


def test_no_api_key_blocks(ctx, service, monkeypatch):
    def boom(*, settings, title, draft):
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    monkeypatch.setattr(refining, "run_refine_agent", boom)
    out = RefineStage().run(service.create("x", "do a thing"), ctx)
    assert out.next_state is State.BLOCKED
    assert "OPENROUTER_API_KEY" in out.note


def test_success_rewrites_description(ctx, service, monkeypatch):
    spec = "## Problem\nx\n## Acceptance criteria\n- [ ] works\n"
    monkeypatch.setattr(
        refining, "run_refine_agent", lambda **_: spec
    )
    t = service.create("Add X", "make x happen")

    out = RefineStage().run(t, ctx)

    assert out.next_state is State.READY
    ws = service.workspace(t)
    assert ws.read_description() == spec
    assert (ws.artifacts_dir / "draft-original.md").read_text() == "make x happen"
    # DB pointer kept in sync with the rewritten file
    expected = hashlib.sha256(spec.encode("utf-8")).hexdigest()
    assert service.get(t.id).content_hash == expected


def test_empty_spec_blocks(ctx, service, monkeypatch):
    monkeypatch.setattr(refining, "run_refine_agent", lambda **_: "  \n ")
    out = RefineStage().run(service.create("x", "draft"), ctx)
    assert out.next_state is State.BLOCKED


async def test_chains_draft_to_implement(ctx, service, monkeypatch):
    """Full wiring: emit -> refine -> ready -> implement. Implement is
    real but no FORGE_REMOTE_URL, so the chain halts at BLOCKED there —
    proving draft never needs a manual transition."""
    monkeypatch.setattr(
        refining, "run_refine_agent", lambda **_: "## Problem\nspec\n"
    )
    t = service.create("Add X", "rough idea")

    await process_ticket(t.id, ctx)

    reloaded = service.get(t.id)
    assert reloaded.state is State.BLOCKED
    states = [e.state for e in service.history(t.id)]
    assert State.READY in states  # refine ran and advanced it
    assert "FORGE_REMOTE_URL" in service.history(t.id)[-1].note
