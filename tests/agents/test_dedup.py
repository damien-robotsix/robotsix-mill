"""run_dedup_check (dd73). Shipped with no tests and a broken
pydantic-ai call (request_limit passed as a bare run_sync kwarg ->
UserError every time -> dedup silently dead). These lock the contract.
"""

from robotsix_mill.agents import dedup
from robotsix_mill.agents.dedup import DedupResult, rank_candidates_by_similarity
from robotsix_mill.core.models import Ticket


class _Result:
    def __init__(self, output):
        self.output = output


class _FakeAgent:
    def __init__(self):
        self.calls = []

    def run_sync(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        return _Result(DedupResult(duplicate_of="T-1", already_done=None, reason="dup"))


def _patch_agent(monkeypatch, agent):
    monkeypatch.setattr("robotsix_mill.agents.base.build_agent", lambda *a, **k: agent)


def test_passes_usage_limits_not_bare_request_limit(settings, monkeypatch):
    """Regression: must use usage_limits=UsageLimits(...), never a bare
    request_limit= kwarg (the latter raised UserError on every call)."""
    from pydantic_ai.usage import UsageLimits

    agent = _FakeAgent()
    _patch_agent(monkeypatch, agent)

    out = dedup.run_dedup_check(
        settings=settings,
        draft_title="t",
        draft_body="b",
        candidates_json="[]",
    )
    assert out["duplicate_of"] == "T-1"  # real result, not the failure path
    assert len(agent.calls) == 1
    _, kwargs = agent.calls[0]
    assert "request_limit" not in kwargs
    assert isinstance(kwargs.get("usage_limits"), UsageLimits)


def test_graceful_on_agent_error(settings, monkeypatch):
    """Any exception → null verdict + 'dedup check failed' (never
    blocks refine), but it must be the EXCEPTION path, not the bug."""

    class _Boom:
        def run_sync(self, *a, **k):
            raise RuntimeError("model down")

    _patch_agent(monkeypatch, _Boom())
    out = dedup.run_dedup_check(
        settings=settings,
        draft_title="t",
        draft_body="b",
        candidates_json="[]",
    )
    assert out == {
        "duplicate_of": None,
        "already_done": None,
        "reason": "dedup check failed",
    }


def test_non_dedup_result_output_is_handled(settings, monkeypatch):
    class _Weird:
        def run_sync(self, *a, **k):
            return _Result("not a DedupResult")

    _patch_agent(monkeypatch, _Weird())
    out = dedup.run_dedup_check(
        settings=settings,
        draft_title="t",
        draft_body="b",
        candidates_json="[]",
    )
    assert out["duplicate_of"] is None
    assert "unexpected type" in out["reason"]


def test_fs_tools_passed_when_repo_dir_provided(settings, monkeypatch):
    """When repo_dir is set, build_agent receives read_file+list_dir
    fs tools; when None, no tools are passed."""

    # Controlled fs mocks with __name__
    def _read_file(*a, **k):
        pass

    _read_file.__name__ = "read_file"

    def _list_dir(*a, **k):
        pass

    _list_dir.__name__ = "list_dir"

    def _write_file(*a, **k):
        pass

    _write_file.__name__ = "write_file"

    fs_mocks = [_read_file, _write_file, _list_dir]

    monkeypatch.setattr(
        "robotsix_mill.agents.fs_tools.build_fs_tools",
        lambda root, s: fs_mocks,
    )

    captured_tools: list | None = None

    def _capture_agent(*a, tools=None, **k):
        nonlocal captured_tools
        captured_tools = tools
        return _FakeAgent()

    monkeypatch.setattr("robotsix_mill.agents.base.build_agent", _capture_agent)

    # Case 1: repo_dir provided → fs tools should be filtered in
    from pathlib import Path

    dedup.run_dedup_check(
        settings=settings,
        draft_title="t",
        draft_body="b",
        candidates_json="[]",
        repo_dir=Path("/fake/repo"),
    )
    assert captured_tools is not None
    tool_names = {t.__name__ for t in captured_tools}
    assert tool_names == {"read_file", "list_dir"}

    # Case 2: repo_dir=None → no fs tools
    captured_tools = None
    dedup.run_dedup_check(
        settings=settings,
        draft_title="t",
        draft_body="b",
        candidates_json="[]",
        repo_dir=None,
    )
    assert captured_tools == []


# ---------------------------------------------------------------------------
# rank_candidates_by_similarity unit tests
# ---------------------------------------------------------------------------


def _t(id: str, title: str) -> Ticket:
    """Minimal Ticket for ranking tests — only id/title needed."""
    return Ticket(id=id, title=title, workspace_path="")


def test_ranking_exact_title_match_ranks_highest():
    """Candidate with identical title to draft scores highest."""
    candidates = [
        _t("a", "Add dark mode toggle"),
        _t("b", "Fix login timeout bug"),
        _t("c", "Add dark mode toggle"),  # exact match
        _t("d", "Refactor database layer"),
        _t("e", "Update README with new badges"),
        _t("f", "Implement rate limiting middleware"),
        _t("g", "Add CSV export feature"),
        _t("h", "CI pipeline improvements"),
        _t("i", "Add healthcheck endpoint"),
    ]
    result = rank_candidates_by_similarity(
        draft_title="Add dark mode toggle",
        draft_body="We need a dark mode toggle in the settings panel.",
        candidates=candidates,
        max_candidates=5,
    )
    # Both exact-title-match candidates must be in the top results.
    top_ids = {t.id for t in result}
    assert "a" in top_ids
    assert "c" in top_ids


def test_ranking_no_overlap_scores_zero():
    """Candidate with completely disjoint title gets score 0.0."""
    candidates = [
        _t("a", "Add dark mode toggle"),
        _t("b", "xyzzy"),
    ]
    # The ranking function doesn't expose raw scores, but we can verify
    # the overlapping candidate is selected and the disjoint one isn't.
    result = rank_candidates_by_similarity(
        draft_title="Add dark mode toggle",
        draft_body="We need a dark mode toggle in the settings panel.",
        candidates=candidates,
        max_candidates=1,
    )
    assert len(result) == 1
    # "Add dark mode toggle" should rank higher than "xyzzy"
    assert result[0].id == "a"


def test_ranking_below_threshold_returns_all():
    """When len(candidates) ≤ max_candidates, all returned unchanged."""
    candidates = [
        _t("a", "Add dark mode"),
        _t("b", "Fix login"),
    ]
    result = rank_candidates_by_similarity(
        draft_title="Some draft",
        draft_body="body text",
        candidates=candidates,
        max_candidates=5,
    )
    assert len(result) == 2
    assert {t.id for t in result} == {"a", "b"}


def test_ranking_above_threshold_returns_top_n():
    """When candidates exceed max, return exactly N sorted by descending score."""
    candidates = [
        _t("a", "Fix login timeout bug"),
        _t("b", "Add dark mode toggle"),
        _t("c", "Refactor database layer"),
        _t("d", "Update README badges"),
        _t("e", "Rate limiting middleware"),
        _t("f", "CSV export feature"),
        _t("g", "CI pipeline improvements"),
        _t("h", "Add healthcheck endpoint"),
        _t("i", "Add user avatar field"),
        _t("j", "Implement search functionality"),
    ]
    result = rank_candidates_by_similarity(
        draft_title="Add dark mode toggle",
        draft_body="We need a dark mode toggle in the settings panel.",
        candidates=candidates,
        max_candidates=5,
    )
    assert len(result) == 5
    # The best match (exact title) should be first.
    assert result[0].id == "b"


def test_ranking_empty_draft_tokens_returns_first_n():
    """When draft produces no tokens (all ≤ 2 chars), returns first N candidates."""
    candidates = [
        _t("a", "Fix login timeout bug"),
        _t("b", "Add dark mode toggle"),
        _t("c", "Refactor database layer"),
        _t("d", "Update README badges"),
        _t("e", "Rate limiting middleware"),
        _t("f", "CSV export feature"),
        _t("g", "CI pipeline improvements"),
        _t("h", "Add healthcheck endpoint"),
        _t("i", "Add user avatar field"),
        _t("j", "Implement search functionality"),
    ]
    result = rank_candidates_by_similarity(
        draft_title="a",
        draft_body="b c",  # all tokens ≤ 2 chars → filtered out
        candidates=candidates,
        max_candidates=3,
    )
    assert len(result) == 3
    # Should return first N as-is (no meaningful ranking)
    assert result[0].id == "a"
    assert result[1].id == "b"
    assert result[2].id == "c"


def test_ranking_zero_token_candidate_scores_zero():
    """Candidate with zero tokens (all ≤ 2 chars title) gets score 0.0."""
    candidates = [
        _t("a", "a b"),  # all tokens ≤ 2 chars → score 0.0
        _t("b", "Add dark mode toggle"),
        _t("c", "Refactor database layer"),
        _t("d", "Update README badges"),
        _t("e", "Rate limiting middleware"),
        _t("f", "CSV export feature"),
        _t("g", "CI pipeline improvements"),
        _t("h", "Add healthcheck endpoint"),
        _t("i", "Add user avatar field"),
        _t("j", "Implement search functionality"),
    ]
    result = rank_candidates_by_similarity(
        draft_title="Add dark mode toggle",
        draft_body="We need a dark mode toggle.",
        candidates=candidates,
        max_candidates=3,
    )
    # The zero-token candidate scores 0.0 and must not appear in the
    # top 3 when there are at least 3 candidates with positive scores.
    assert "a" not in {t.id for t in result}
    # The best match still comes first.
    assert result[0].id == "b"
