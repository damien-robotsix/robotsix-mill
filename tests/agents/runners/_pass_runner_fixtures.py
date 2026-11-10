"""Shared fakes and helpers for the agent-pass runner tests."""

from robotsix_mill.config import Settings


class _FakeAgentResult:
    """Returned by mock agent callables — matches the interface that
    run_agent_pass accesses: .updated_memory, .draft_titles, .draft_bodies."""

    def __init__(
        self,
        updated_memory,
        draft_titles,
        draft_bodies,
        gap_ids=None,
        summary="",
    ):
        self.updated_memory = updated_memory
        self.draft_titles = draft_titles
        self.draft_bodies = draft_bodies
        self.summary = summary
        if gap_ids is not None:
            self.gap_ids = gap_ids


def _make_settings(tmp_path, **overrides):
    """Create Settings with MILL_DATA_DIR pointed at tmp_path."""
    overrides.setdefault("data_dir", str(tmp_path / "data"))
    return Settings(**overrides)


# ------------------------------------------------------------------ helpers


def _make_agent(
    updated_memory="new memory",
    draft_titles=None,
    draft_bodies=None,
):
    """Return a callable that returns a _FakeAgentResult with the given data."""
    if draft_titles is None:
        draft_titles = []
    if draft_bodies is None:
        draft_bodies = []

    def agent_fn(*, settings, memory, recent_proposals="", verified_proposals=""):
        return _FakeAgentResult(
            updated_memory=updated_memory,
            draft_titles=draft_titles,
            draft_bodies=draft_bodies,
        )

    return agent_fn
