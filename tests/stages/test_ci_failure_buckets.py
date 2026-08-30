"""Tests for the deterministic CI-failure bucketing and the events the
ci-fix stage emits from it."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from robotsix_mill.agents.runners.diagnostic_events import list_diagnostic_events
from robotsix_mill.config import Settings
from robotsix_mill.stages.ci_failure_buckets import (
    BUCKETS,
    DEFAULT_PREVENTION_RULES,
    classify_ci_failure,
)

_TESTS_CHECK = [{"name": "ci / tests", "conclusion": "failure"}]


def _summary(log: str, name: str = "ci / tests") -> str:
    return f"## ❌ {name}\n\n**Job logs:**\n```\n{log}\n```\n"


@pytest.mark.parametrize(
    ("failing", "log", "bucket"),
    [
        (
            _TESTS_CHECK,
            (
                "Run uv run ruff format --check src tests\n"
                "Would reformat: src/robotsix_mill/stages/ci_fix.py\n"
                "1 file would be reformatted, 412 files already formatted\n"
                "##[error]Process completed with exit code 1."
            ),
            "ruff-format",
        ),
        (
            _TESTS_CHECK,
            (
                "Run uv run ruff check src tests\n"
                "src/robotsix_mill/agents/x.py:3:8: F401 [*] `os` imported but unused\n"
                "Found 1 error.\n[*] 1 fixable with the `--fix` option."
            ),
            "ruff-lint",
        ),
        (
            _TESTS_CHECK,
            (
                "Run uv run mypy src/ --strict\n"
                "src/robotsix_mill/core/x.py:12: error: Incompatible return value type "
                '(got "str | None", expected "str")  [return-value]\n'
                "Found 1 error in 1 file (checked 400 source files)"
            ),
            "mypy",
        ),
        (
            _TESTS_CHECK,
            (
                "=================================== FAILURES ===================================\n"
                "___________________ test_merge_gate_blocks_when_red ___________________\n"
                "    assert outcome.state == State.BLOCKED\n"
                "E   AssertionError: assert <State.IMPLEMENT_COMPLETE> == <State.BLOCKED>\n"
                "FAILED tests/stages/test_merge.py::test_merge_gate_blocks_when_red\n"
                "1 failed, 2340 passed in 92.11s"
            ),
            "pytest-failure",
        ),
        (
            _TESTS_CHECK,
            (
                "==================================== ERRORS ====================================\n"
                "____________ ERROR collecting tests/agents/test_new_thing.py ____________\n"
                "ImportError while importing test module 'tests/agents/test_new_thing.py'.\n"
                "E   ModuleNotFoundError: No module named 'robotsix_mill.agents.new_thing'\n"
                "!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!"
            ),
            "pytest-collection/import",
        ),
        (
            _TESTS_CHECK,
            (
                "Run uv run robotsix-modules check-registration docs/modules.yaml --root .\n"
                "Unregistered file: src/robotsix_mill/agents/runners/new_runner.py\n"
                "##[error]Process completed with exit code 1."
            ),
            "modules-yaml-unregistered",
        ),
        (
            _TESTS_CHECK,
            (
                "Run uv run vulture src/ vulture_whitelist.py\n"
                "src/robotsix_mill/agents/x.py:44: unused function '_helper' (60% confidence)"
            ),
            "vulture",
        ),
        (
            _TESTS_CHECK,
            (
                "Run uv run deptry .\n"
                "pyproject.toml: DEP002 'shtab' defined as a dependency but not used"
            ),
            "deptry",
        ),
        (
            [{"name": "CodeQL", "conclusion": "failure"}],
            "1 new alert: py/clear-text-logging-sensitive-data",
            "codeql",
        ),
        (
            [{"name": "security / trivy", "conclusion": "failure"}],
            "python-3.14-slim (debian 12.5)\nCVE-2024-12345 HIGH openssl",
            "trivy",
        ),
        (
            _TESTS_CHECK,
            (
                "Run uv sync --frozen\n"
                "error: Failed to fetch: https://pypi.org/simple/pydantic/\n"
                "Caused by: Temporary failure in name resolution"
            ),
            "flaky-network",
        ),
        (
            _TESTS_CHECK,
            "Run make weird-step\nSomething odd happened, no tool named",
            "unknown",
        ),
    ],
    ids=lambda v: v if isinstance(v, str) and v in BUCKETS else None,
)
def test_bucket_derivation(failing, log, bucket):
    klass = classify_ci_failure(failing, _summary(log, failing[0]["name"]))
    assert klass.bucket == bucket
    assert klass.prevention_rule == DEFAULT_PREVENTION_RULES[bucket]
    assert klass.root_cause  # never empty, even for unknown


def test_classification_is_deterministic_and_uses_check_name_alone():
    # No log at all: the check name is enough for tool-named checks.
    a = classify_ci_failure([{"name": "lint / mypy"}], "")
    b = classify_ci_failure([{"name": "lint / mypy"}], "")
    assert a == b
    assert a.bucket == "mypy"
    assert a.root_cause == "mypy: lint / mypy"


def test_root_cause_picks_the_error_line_not_the_scaffolding():
    klass = classify_ci_failure(
        _TESTS_CHECK,
        _summary(
            "[error] src/x.py:12: error: Name 'foo' is not defined  [name-defined]"
        ),
    )
    assert klass.bucket == "mypy"
    assert klass.root_cause.startswith("src/x.py:12: error: Name 'foo'")
    assert "##" not in klass.root_cause


def test_every_bucket_has_a_default_rule_entry():
    assert set(DEFAULT_PREVENTION_RULES) == BUCKETS
    assert all(DEFAULT_PREVENTION_RULES[b] for b in BUCKETS - {"unknown"})


# ---------------------------------------------------------------------------
# ci_fix stage emission
# ---------------------------------------------------------------------------


def _ctx(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"))
    repo_config = SimpleNamespace(board_id="board-a")
    return SimpleNamespace(settings=settings, repo_config=repo_config), settings


def test_ci_fix_emits_bucketed_failure_and_resolved_events(tmp_path):
    from robotsix_mill.agents.ci_fixing import CiFixResult
    from robotsix_mill.stages import ci_fix as ci_fix_mod

    ctx, settings = _ctx(tmp_path)
    ticket = SimpleNamespace(id="t-1", board_id="board-a")
    summary = _summary("Would reformat: src/a.py\n1 file would be reformatted")

    ci_fix_mod._emit_ci_failure_event(ticket, ctx, _TESTS_CHECK, summary)
    ci_fix_mod._emit_ci_fix_resolved_event(
        ticket,
        ctx,
        _TESTS_CHECK,
        summary,
        CiFixResult(
            status="DONE",
            summary="Ran ruff format on src/a.py and pushed.",
            pattern_approach="Run ruff format before committing.",
        ),
    )

    failures = list_diagnostic_events(settings, "board-a", category="CI_FAILURE")
    resolved = list_diagnostic_events(settings, "board-a", category="CI_FIX_RESOLVED")
    assert len(failures) == 1 and len(resolved) == 1
    assert failures[0].bucket == resolved[0].bucket == "ruff-format"
    assert failures[0].normalized_key == resolved[0].normalized_key
    assert failures[0].prevention_rule == DEFAULT_PREVENTION_RULES["ruff-format"]
    assert resolved[0].root_cause == "Ran ruff format on src/a.py and pushed."
    assert resolved[0].prevention_rule == "Run ruff format before committing."
