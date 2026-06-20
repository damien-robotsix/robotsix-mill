"""Tests for the already-done verifier (acceptance criteria 1–6)."""

from pathlib import Path

from robotsix_mill.agents import refining
from robotsix_mill.agents.refining import AlreadyDoneResult, RefineResult
from robotsix_mill.config import Settings


# A memory ledger with one ``no_change_needed`` entry whose topic is
# Jaccard-similar to "consolidate body parsing" — the ticket title used
# in the verifier tests below.  The entry is dated today so it passes
# the 90-day lookback.
_VERIFIER_MEMORY = (
    "## Refine run 2026-06-20 — consolidate inline HTTP body parsing\n"
    "- **Observation**: three call sites exist\n"
    "**Outcome**: `no_change_needed` — already consolidated in PR #42\n"
)


def _simple_agent():
    """Return a mock agent that returns a trivial RefineResult."""

    class FakeResult:
        output = RefineResult(spec_markdown="ok")
        response = type("R", (), {"finish_reason": "stop"})()

        def all_messages_json(self):
            return b"[]"

        def new_messages_json(self):
            return b"[]"

    class FakeAgent:
        def run_sync(self, prompt, *, message_history=None, usage_limits=None):
            return FakeResult()

    return FakeAgent()


def test_jaccard_match_alone_does_not_yield_no_change_needed(
    settings, tmp_path, monkeypatch
):
    """Acceptance criterion 5: a Jaccard match alone does NOT yield
    ``no_change_needed`` without verifier confirmation.  The verifier
    stub returns ``already_done=False`` — the refine agent must fall
    through to the normal path."""
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    # Stub the verifier to reject the candidate.
    monkeypatch.setattr(
        refining,
        "verify_already_done",
        lambda **_: AlreadyDoneResult(
            already_done=False,
            rationale="git grep shows _parse_request_body still in _batch_mixin.py:124",
        ),
    )

    # Also stub build_agent / run_agent so the full refine path
    # doesn't need a real LLM.
    monkeypatch.setattr(
        base_module, "build_agent_from_definition", lambda *a, **k: _simple_agent()
    )
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    result = refining.run_refine_agent(
        settings=settings,
        title="consolidate inline HTTP body parsing",
        draft="Consolidate inline body parsing at _batch_mixin.py:124",
        memory=_VERIFIER_MEMORY,
        repo_dir=repo_dir,
    )

    # Must NOT short-circuit to no_change_needed — verifier said no.
    assert not result.no_change_needed
    # The full refine agent ran and produced a spec.
    assert result.spec_markdown is not None


def test_verifier_confirmation_yields_no_change_needed(settings, tmp_path, monkeypatch):
    """Acceptance criterion 2: when the verifier returns
    ``already_done=True``, ``run_refine_agent`` returns
    ``no_change_needed=True`` with the verifier's rationale."""
    verifier_rationale = (
        "git grep _parse_request_body -- _batch_mixin.py -> 0 hits; "
        "the consolidation is done."
    )
    monkeypatch.setattr(
        refining,
        "verify_already_done",
        lambda **_: AlreadyDoneResult(
            already_done=True,
            rationale=verifier_rationale,
        ),
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    result = refining.run_refine_agent(
        settings=settings,
        title="consolidate inline HTTP body parsing",
        draft="Consolidate inline body parsing at _batch_mixin.py:124",
        memory=_VERIFIER_MEMORY,
        repo_dir=repo_dir,
    )

    assert result.no_change_needed
    assert result.no_change_rationale == verifier_rationale


def test_no_repo_dir_never_short_circuits_on_memory_alone(
    settings, tmp_path, monkeypatch
):
    """Acceptance criterion 4: when ``repo_dir is None``, no candidate
    triggers a verifier call and ``run_refine_agent`` falls through to
    the normal path (never short-circuits to ``no_change_needed=True``
    on memory alone)."""
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    verifier_called = []

    def spy_verifier(**kwargs):
        verifier_called.append(True)
        return AlreadyDoneResult(already_done=True, rationale="should not be used")

    monkeypatch.setattr(refining, "verify_already_done", spy_verifier)

    # Stub the full refine path so it doesn't need a real LLM.
    monkeypatch.setattr(
        base_module, "build_agent_from_definition", lambda *a, **k: _simple_agent()
    )
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    result = refining.run_refine_agent(
        settings=settings,
        title="consolidate inline HTTP body parsing",
        draft="Consolidate inline body parsing at _batch_mixin.py:124",
        memory=_VERIFIER_MEMORY,
        repo_dir=None,  # No clone available
    )

    # Verifier was never called.
    assert verifier_called == []
    # Must NOT short-circuit.
    assert not result.no_change_needed
    # Full refine ran and produced a spec.
    assert result.spec_markdown is not None


def test_verify_already_done_level_and_module():
    """Acceptance criterion 3: ``verify_already_done`` is loaded from
    ``agent_definitions/already_done_check.yaml``, has ``level == 1``,
    ``module: refining``, and ``output_type: AlreadyDoneResult``."""
    import yaml as _yaml

    path = (
        Path(__file__).parent.parent.parent
        / "agent_definitions"
        / "already_done_check.yaml"
    )
    definition = _yaml.safe_load(path.read_text())

    assert definition["level"] == 1
    assert definition["module"] == "refining"
    assert definition["output_type"] == "AlreadyDoneResult"


def test_already_done_request_limit_settable_via_config():
    """Acceptance criterion 6: ``already_done_request_limit`` defaults
    to 8 and is settable via the ``core.limits.already_done_requests``
    alias."""
    from robotsix_mill.config.loader import flatten_yaml_config

    # Default.
    s = Settings(data_dir="/tmp/test_already_done_default")
    assert s.already_done_request_limit == 8

    # Via alias.
    yaml_config = {"core": {"limits": {"already_done_requests": 12}}}
    kwargs = flatten_yaml_config(yaml_config)
    s2 = Settings(data_dir="/tmp/test_already_done_alias", **kwargs)
    assert s2.already_done_request_limit == 12


def test_check_memory_for_no_change_still_works_as_prefilter():
    """Acceptance criterion 5: ``_check_memory_for_no_change`` retains
    its existing Jaccard/lookback behavior as a pre-filter.  A memory
    entry with a similar topic within 90 days returns a rationale
    string."""
    # Similar topic -> match.
    result = refining._check_memory_for_no_change(
        title="consolidate inline HTTP body parsing",
        draft="Consolidate inline body parsing at _batch_mixin.py:124",
        memory=_VERIFIER_MEMORY,
    )
    assert result is not None
    assert "already consolidated" in result

    # Unrelated topic -> no match.
    result2 = refining._check_memory_for_no_change(
        title="add quantum encryption to database layer",
        draft="Implement post-quantum cryptography for the storage engine",
        memory=_VERIFIER_MEMORY,
    )
    assert result2 is None

    # Empty memory -> no match.
    result3 = refining._check_memory_for_no_change(
        title="anything",
        draft="anything",
        memory="",
    )
    assert result3 is None


def test_verifier_called_with_candidate_rationale(settings, tmp_path, monkeypatch):
    """The verifier receives the candidate rationale from the memory
    pre-filter."""
    captured_kwargs: list[dict] = []

    def spy_verifier(**kwargs):
        captured_kwargs.append(kwargs)
        return AlreadyDoneResult(
            already_done=False,
            rationale="symbols still present",
        )

    monkeypatch.setattr(refining, "verify_already_done", spy_verifier)

    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    monkeypatch.setattr(
        base_module, "build_agent_from_definition", lambda *a, **k: _simple_agent()
    )
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    refining.run_refine_agent(
        settings=settings,
        title="consolidate inline HTTP body parsing",
        draft="Consolidate inline body parsing at _batch_mixin.py:124",
        memory=_VERIFIER_MEMORY,
        repo_dir=repo_dir,
    )

    assert len(captured_kwargs) == 1
    assert captured_kwargs[0]["candidate_rationale"] == "already consolidated in PR #42"
    assert captured_kwargs[0]["title"] == "consolidate inline HTTP body parsing"
    assert captured_kwargs[0]["repo_dir"] == repo_dir


def test_verifier_not_called_without_memory_match(settings, tmp_path, monkeypatch):
    """When no memory entry matches, the verifier is never invoked."""
    import robotsix_mill.agents.base as base_module
    import robotsix_mill.agents.retry as retry_module

    verifier_called = []

    def spy_verifier(**kwargs):
        verifier_called.append(True)
        return AlreadyDoneResult(already_done=True, rationale="x")

    monkeypatch.setattr(refining, "verify_already_done", spy_verifier)
    monkeypatch.setattr(
        base_module, "build_agent_from_definition", lambda *a, **k: _simple_agent()
    )
    monkeypatch.setattr(
        retry_module,
        "run_agent",
        lambda agent, make_run, *, what="model call", sleep=None: make_run(agent),
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    refining.run_refine_agent(
        settings=settings,
        title="add quantum encryption",
        draft="Implement post-quantum cryptography",
        memory="",  # Empty memory - no match possible
        repo_dir=repo_dir,
    )

    assert verifier_called == []
