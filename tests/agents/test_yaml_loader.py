"""Tests for the YAML agent-definition loader."""

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from pydantic import ValidationError

from robotsix_mill.agents.yaml_loader import (
    AgentDefinition,
    load_agent_definition,
    load_periodic_agent_definition,
)


# ── helpers ──────────────────────────────────────────────────────────


def _write_yaml(tmp_path: Path, content: str) -> Path:
    """Write *content* to a temporary YAML file and return its path."""
    p = tmp_path / "test_agent.yaml"
    p.write_text(content, encoding="utf-8")
    return p


# ── load_agent_definition — valid inputs ─────────────────────────────


def test_valid_all_fields(tmp_path):
    """All fields populated → correct AgentDefinition."""
    p = _write_yaml(
        tmp_path,
        """\
name: test-agent
description: A test agent.
category: pipeline
level: 2
system_prompt: You are a test agent.
tools:
  - explore
  - read_file
web_knowledge: true
report_issue: false
output_type: TestResult
retries: 3
module: testing
skills:
  - foo
  - bar
""",
    )
    ad = load_agent_definition(p)
    assert ad.name == "test-agent"
    assert ad.description == "A test agent."
    assert ad.category == "pipeline"
    assert ad.level == 2
    assert ad.system_prompt == "You are a test agent."
    assert ad.tools == ["explore", "read_file"]
    assert ad.web_knowledge is True
    assert ad.report_issue is False
    assert ad.output_type == "TestResult"
    assert ad.retries == 3
    assert ad.module == "testing"
    assert ad.skills == ["foo", "bar"]


def test_minimal_valid_yaml(tmp_path):
    """Only required fields → defaults applied for optionals."""
    p = _write_yaml(
        tmp_path,
        """\
name: minimal
level: 1
system_prompt: Do one thing well.
""",
    )
    ad = load_agent_definition(p)
    assert ad.name == "minimal"
    assert ad.level == 1
    assert ad.system_prompt == "Do one thing well."
    assert ad.description is None
    assert ad.category is None
    assert ad.tools == []
    assert ad.web_knowledge is False
    assert ad.report_issue is True
    assert ad.output_type is None
    assert ad.retries == 2
    assert ad.module is None
    assert ad.skills == []


def test_level_out_of_range_rejected(tmp_path):
    """level must be 1, 2, or 3 — out-of-range values are rejected."""
    p = _write_yaml(
        tmp_path,
        """\
name: bad-level
level: 4
system_prompt: test
""",
    )
    with pytest.raises(ValidationError):
        load_agent_definition(p)


def test_empty_tools_and_skills_get_defaults(tmp_path):
    """Explicit empty lists → still default []."""
    p = _write_yaml(
        tmp_path,
        """\
name: empty-lists
level: 1
system_prompt: test
tools: []
skills: []
""",
    )
    ad = load_agent_definition(p)
    assert ad.tools == []
    assert ad.skills == []


# ── load_agent_definition — error cases ──────────────────────────────


def test_missing_required_name(tmp_path):
    """Missing 'name' → ValidationError."""
    p = _write_yaml(
        tmp_path,
        """\
level: 1
system_prompt: test
""",
    )
    with pytest.raises(Exception) as exc_info:  # ValidationError
        load_agent_definition(p)
    err_str = str(exc_info.value)
    assert "name" in err_str.lower() or "Field required" in err_str


def test_missing_required_level(tmp_path):
    """Missing 'level' → ValidationError."""
    p = _write_yaml(
        tmp_path,
        """\
name: no-level
system_prompt: test
""",
    )
    with pytest.raises(Exception) as exc_info:
        load_agent_definition(p)
    err_str = str(exc_info.value)
    assert "level" in err_str.lower() or "Field required" in err_str


def test_missing_required_system_prompt(tmp_path):
    """Missing 'system_prompt' → ValidationError."""
    p = _write_yaml(
        tmp_path,
        """\
name: no-prompt
level: 1
""",
    )
    with pytest.raises(Exception) as exc_info:
        load_agent_definition(p)
    err_str = str(exc_info.value)
    assert "system_prompt" in err_str.lower() or "Field required" in err_str


def test_wrong_type_web_knowledge(tmp_path):
    """web_knowledge: [1, 2, 3] (list) → ValidationError."""
    p = _write_yaml(
        tmp_path,
        """\
name: bad-web-knowledge
level: 1
system_prompt: test
web_knowledge:
  - 1
  - 2
  - 3
""",
    )
    with pytest.raises(Exception) as exc_info:
        load_agent_definition(p)
    err_str = str(exc_info.value)
    assert "web_knowledge" in err_str.lower() or "bool" in err_str.lower()


def test_wrong_type_retries(tmp_path):
    """retries: 'two' (str) → ValidationError."""
    p = _write_yaml(
        tmp_path,
        """\
name: bad-retries
level: 1
system_prompt: test
retries: "two"
""",
    )
    with pytest.raises(Exception) as exc_info:
        load_agent_definition(p)
    err_str = str(exc_info.value)
    assert "retries" in err_str.lower() or "int" in err_str.lower()


def test_malformed_yaml_syntax(tmp_path):
    """Invalid YAML → yaml.YAMLError."""
    p = _write_yaml(
        tmp_path,
        """\
name: bad-yaml
level: 1
\tsystem_prompt: tab-indented  # tabs are illegal in YAML
""",
    )
    with pytest.raises(yaml.YAMLError):
        load_agent_definition(p)


def test_file_not_found():
    """Non-existent path → FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        load_agent_definition(Path("/nonexistent/agent.yaml"))


# ── independence from agent runtime ──────────────────────────────────


def test_loader_independent_of_agent_runtime():
    """Importing yaml_loader does NOT import build_agent, Settings,
    pydantic_ai, or any OpenRouter/provider code."""

    # Run in a subprocess for clean isolation.
    code = """
import sys
from pathlib import Path

# Add src to path.
sys.path.insert(0, "src")

from robotsix_mill.agents.yaml_loader import load_agent_definition, AgentDefinition

# The forbidden modules must NOT be in sys.modules after our import.
forbidden = {
    "pydantic_ai",
    "robotsix_mill.agents.base",
    "robotsix_mill.config",
}
loaded = forbidden & set(sys.modules.keys())
if loaded:
    sys.exit(f"FORBIDDEN IMPORTS: {loaded}")
print("OK")
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "OK" in result.stdout


def test_real_refine_yaml_parses():
    """Smoke-test: the existing agent_definitions/refine.yaml parses
    without error (minus env-var resolution, which requires a mock)."""
    p = Path("agent_definitions/refine.yaml")
    if not p.exists():
        pytest.skip("agent_definitions/refine.yaml not found")

    # Mock the env var so resolution succeeds.
    import os as _os

    _os.environ.setdefault("MILL_REFINE_MODEL", "test/model")

    try:
        ad = load_agent_definition(p)
        assert ad.name == "refine"
        assert ad.category == "pipeline"
        assert ad.web_knowledge is True
        assert ad.report_issue is True
        assert ad.output_type == "RefineResult"
        assert ad.retries == 2
        assert ad.module == "refining"
        assert isinstance(ad.tools, list)
        assert isinstance(ad.skills, list)
    finally:
        # Don't leak the env var if it wasn't set before.
        if "MILL_REFINE_MODEL" not in _os.environ:
            _os.environ.pop("MILL_REFINE_MODEL", None)


def test_real_triage_yaml_parses():
    """Smoke-test: the existing agent_definitions/triage.yaml parses and
    documents both the explore and read_file verification tools."""
    p = Path("agent_definitions/triage.yaml")
    if not p.exists():
        pytest.skip("agent_definitions/triage.yaml not found")

    import os as _os

    had_model = "MILL_TRIAGE_MODEL" in _os.environ
    _os.environ.setdefault("MILL_TRIAGE_MODEL", "test/model")

    try:
        ad = load_agent_definition(p)
        assert ad.name == "triage"
        assert ad.tools == []
        assert ad.output_type == "TriageResult"
        assert ad.retries == 2
        assert ad.module == "refining"
        assert "## Tool: `explore`" in ad.system_prompt
        assert "## Tool: `read_file`" in ad.system_prompt
        assert "`read_file(path)`" in ad.system_prompt
        assert "Verify the specific path with a direct `read_file" in (ad.system_prompt)
    finally:
        if not had_model:
            _os.environ.pop("MILL_TRIAGE_MODEL", None)


def test_real_implement_yaml_has_output_style_brevity_guidance():
    """The real implement.yaml's system_prompt must carry the
    output-style brevity guidance that discourages running commentary.

    Regression guard for the verbosity cost-outlier fix: the implement
    agent's prose between tool calls was a large share of output tokens,
    so the prompt now tells it to keep inter-tool narration terse. If
    that section is removed or reworded away, this test fails.
    """
    p = Path("agent_definitions/implement.yaml")
    if not p.exists():
        pytest.skip("agent_definitions/implement.yaml not found")

    # Mock the env var so resolution succeeds (model: "${MILL_MODEL}").
    import os as _os

    had_model = "MILL_MODEL" in _os.environ
    _os.environ.setdefault("MILL_MODEL", "test/model")

    try:
        ad = load_agent_definition(p)
        assert ad.name == "implement"
        assert "## Output style" in ad.system_prompt
        assert "running commentary" in ad.system_prompt
    finally:
        # Don't leak the env var if it wasn't set before.
        if not had_model:
            _os.environ.pop("MILL_MODEL", None)


def test_real_review_yaml_has_conciseness_directive():
    """The real review.yaml must carry a conciseness directive.

    Guards the trace-review cost fix: trace a7672211 showed the review
    agent emitting a ~2,304-char prose narrative before an APPROVE verdict
    (15,119 output tokens for 202 input tokens, $0.50 for one approve).
    The directive tells the model to be concise and put findings in the
    structured ReviewVerdict fields instead of a free-form essay. If a
    future edit removes it, this test FAILS so the regression is visible.
    """
    p = Path("agent_definitions/review.yaml")
    if not p.exists():
        pytest.skip("agent_definitions/review.yaml not found")

    # Mock the env var so resolution succeeds.
    import os as _os

    had_model = "MILL_REVIEW_MODEL" in _os.environ
    _os.environ.setdefault("MILL_REVIEW_MODEL", "test/model")

    try:
        ad = load_agent_definition(p)
        assert ad.name == "review"
        assert ad.output_type == "ReviewVerdict"

        prompt_lower = ad.system_prompt.lower()
        assert "be concise" in prompt_lower
        assert (
            "restate" in prompt_lower or "summarize the implementation" in prompt_lower
        )
    finally:
        # Don't leak the env var if it wasn't set before.
        if not had_model:
            _os.environ.pop("MILL_REVIEW_MODEL", None)


def test_real_review_yaml_has_max_tokens_cap():
    """The real review.yaml must declare a max_tokens output cap.

    Guards a trace-review cost fix: source trace
    a2df28bd0ec58c341c679a1530e439dc / generation 61e63af1a47121e3 showed
    the review agent emitting 16,789 output tokens from only 298 input
    tokens ($0.32, 3.0x the median) — inline self-talk and diff
    re-summarization instead of a structured ReviewVerdict. The
    conciseness directive alone (guarded by
    test_real_review_yaml_has_conciseness_directive) proved insufficient,
    so a deterministic max_tokens cap backstops runaway generations. If a
    future edit removes or unsets the cap, this test FAILS.
    """
    p = Path("agent_definitions/review.yaml")
    if not p.exists():
        pytest.skip("agent_definitions/review.yaml not found")

    # Mock the env var so resolution succeeds.
    import os as _os

    had_model = "MILL_REVIEW_MODEL" in _os.environ
    _os.environ.setdefault("MILL_REVIEW_MODEL", "test/model")

    try:
        ad = load_agent_definition(p)
        assert ad.name == "review"
        assert ad.max_tokens is not None
        assert ad.max_tokens == 32768
        # Sanity: present and within a tunable-but-bounded range that
        # stays above legitimate verdicts (including reasoning overhead)
        # and below the observed runaway.
        assert 16384 <= ad.max_tokens <= 65536
    finally:
        # Don't leak the env var if it wasn't set before.
        if not had_model:
            _os.environ.pop("MILL_REVIEW_MODEL", None)


# Representative worst-case size of the retrospect runtime memory ledger.
# This tracks the observed ~34K-char per-board ledger surfaced to the agent
# at runtime — NOT the checked-in seed docs/retrospect-memory.md (~946 bytes),
# which is far too small to be representative. A full PATH-3 re-emit returns
# this entire document in `updated_memory`, so the configured max_tokens must
# be large enough to fit it plus the other structured fields.
REPRESENTATIVE_LEDGER_CHARS = 34_000


def test_real_retrospect_yaml_max_tokens_fits_full_ledger_reemit():
    """The real retrospect.yaml's max_tokens must comfortably fit a full
    PATH-3 re-emit of the worst-case runtime memory ledger.

    Offline regression guard: a PATH-3 run returns the entire ~34K-char
    ledger in `updated_memory` plus `findings`/`conclusion`/draft fields and
    the JSON envelope. If max_tokens is lowered below that worst case, the
    model raises "Model token limit (N) exceeded before any response was
    generated" and the post-merge retrospect hard-fails. This test would FAIL
    if max_tokens were reduced back toward 8192 or below the estimate.
    """
    p = Path("agent_definitions/retrospect.yaml")
    if not p.exists():
        pytest.skip("agent_definitions/retrospect.yaml not found")

    # Mock the env var so resolution succeeds.
    import os as _os

    _os.environ.setdefault("MILL_RETROSPECT_MODEL", "test/model")

    try:
        ad = load_agent_definition(p)
        assert ad.max_tokens is not None

        # Conservative chars-per-token heuristic. There is no token-counting
        # helper in the repo (no tiktoken), so this is an intentional,
        # documented approximation: dense Markdown with snake_case
        # identifiers, file paths, and code packs ~3 chars/token.
        CHARS_PER_TOKEN = 3
        # Fixed margin for the structured findings/conclusion/draft fields
        # plus the JSON envelope wrapping the RetrospectResult.
        STRUCTURED_FIELDS_MARGIN = 2_000
        estimated = (
            REPRESENTATIVE_LEDGER_CHARS // CHARS_PER_TOKEN + STRUCTURED_FIELDS_MARGIN
        )

        assert ad.max_tokens >= estimated, (
            f"retrospect max_tokens={ad.max_tokens} is below the conservative "
            f"estimate {estimated} for a full PATH-3 re-emit of the "
            f"~{REPRESENTATIVE_LEDGER_CHARS}-char worst-case runtime ledger "
            f"plus structured fields. The cap can no longer fit a full "
            f"re-emit and must be raised — or file a follow-up for dynamic "
            f"per-run sizing / `## Historical`-compaction of the prompt."
        )
    finally:
        # Don't leak the env var if it wasn't set before.
        if "MILL_RETROSPECT_MODEL" not in _os.environ:
            _os.environ.pop("MILL_RETROSPECT_MODEL", None)


# ── AgentDefinition pydantic model ────────────────────────────────────


def test_agent_definition_model_validation():
    """Direct model_validate with a dict → correct instance."""
    ad = AgentDefinition.model_validate(
        {
            "name": "test",
            "level": 1,
            "system_prompt": "You are helpful.",
        }
    )
    assert ad.name == "test"
    assert ad.web_knowledge is False
    assert ad.retries == 2
    assert ad.tools == []


def test_agent_definition_extra_fields_rejected():
    """Unknown keys → ValidationError."""
    with pytest.raises(ValidationError):
        AgentDefinition.model_validate(
            {
                "name": "test",
                "level": 1,
                "system_prompt": "ok",
                "unknown_field": "nope",
            }
        )


def test_inject_agent_md_defaults_to_true():
    """inject_agent_md defaults to True when not specified."""
    ad = AgentDefinition.model_validate(
        {
            "name": "test",
            "level": 1,
            "system_prompt": "You are helpful.",
        }
    )
    assert ad.inject_agent_md is True


def test_inject_agent_md_can_be_false():
    """inject_agent_md can be explicitly set to False."""
    ad = AgentDefinition.model_validate(
        {
            "name": "test",
            "level": 1,
            "system_prompt": "You are helpful.",
            "inject_agent_md": False,
        }
    )
    assert ad.inject_agent_md is False


# ── Structural verification: all agent_definitions/*.yaml ─────────────


# Known-valid tool names (mirrors ToolRegistry + fs_tools).
_VALID_TOOL_NAMES = frozenset(
    {
        "clone_repo",
        "explore",
        "parallel_explore",
        "read_file",
        "write_file",
        "edit_file",
        "delete_file",
        "list_dir",
        "run_command",
        "langfuse_session_summary",
        "langfuse_list_traces",
        "langfuse_trace_detail",
        "langfuse_session_cost",
        "langfuse_inspect_trace",
        "inspect_cost",
        "query_app_logs",
        "create_repo",
        "fetch_ci_logs",
        "fork_repo",
        "post_findings",
        "git_fetch",
        "git_remote_sha",
        "git_push_with_lease",
        "git_branch_ancestry",
        "wait_for_ci",
    }
)

# Known-valid categories.
_VALID_CATEGORIES = frozenset(
    {"pipeline", "periodic", "sub_agent", "interactive", "sandboxed"}
)

# Mapping from ${VAR} → Settings field alias (from config.py).
_ENV_VAR_TO_SETTINGS_ALIAS: dict[str, str] = {
    "MILL_MODEL": "MILL_MODEL",
    "MILL_EXPLORE_MODEL": "MILL_EXPLORE_MODEL",
    "MILL_TEST_MODEL": "MILL_TEST_MODEL",
    "MILL_REFINE_MODEL": "MILL_REFINE_MODEL",
    "MILL_ANSWER_MODEL": "MILL_ANSWER_MODEL",
    "MILL_ASK_TO_TICKET_MODEL": "MILL_ASK_TO_TICKET_MODEL",
    "MILL_RETROSPECT_MODEL": "MILL_RETROSPECT_MODEL",
    "MILL_AUDIT_MODEL": "MILL_AUDIT_MODEL",
    "MILL_DEDUP_MODEL": "MILL_DEDUP_MODEL",
    "MILL_OBSOLESCENCE_MODEL": "MILL_OBSOLESCENCE_MODEL",
    "MILL_TRIAGE_MODEL": "MILL_TRIAGE_MODEL",
    "MILL_WEB_RESEARCH_MODEL": "MILL_WEB_RESEARCH_MODEL",
    "MILL_AUTO_APPROVE_MODEL": "MILL_AUTO_APPROVE_MODEL",
    "MILL_REVIEW_MODEL": "MILL_REVIEW_MODEL",
    "MILL_SCOPE_TRIAGE_MODEL": "MILL_SCOPE_TRIAGE_MODEL",
    "MILL_COMPLETENESS_CHECK_MODEL": "MILL_COMPLETENESS_CHECK_MODEL",
    "MILL_DOC_MODEL": "MILL_DOC_MODEL",
    "MILL_DOC_CLASSIFIER_MODEL": "MILL_DOC_CLASSIFIER_MODEL",
    "MILL_TRACE_INSPECTOR_MODEL": "MILL_TRACE_INSPECTOR_MODEL",
    "MILL_TEST_GAP_MODEL": "MILL_TEST_GAP_MODEL",
    "MILL_AGENT_CHECK_MODEL": "MILL_AGENT_CHECK_MODEL",
    "MILL_HEALTH_MODEL": "MILL_HEALTH_MODEL",
    "MILL_SURVEY_MODEL": "MILL_SURVEY_MODEL",
    "MILL_BC_CHECK_MODEL": "MILL_BC_CHECK_MODEL",
    "MILL_CONFIG_SYNC_MODEL": "MILL_CONFIG_SYNC_MODEL",
    "MILL_REVIEW_REVISION_MODEL": "MILL_REVIEW_REVISION_MODEL",
}


def _all_yaml_files():
    """Return every .yaml file path under agent_definitions/."""
    ad = Path("agent_definitions")
    if not ad.is_dir():
        return []
    return sorted(ad.glob("*.yaml"))


def _all_definitions():
    """Load every YAML file and return list of (path, AgentDefinition)."""
    result = []
    for p in _all_yaml_files():
        result.append((p, load_agent_definition(p)))
    return result


def test_all_yaml_files_parse(monkeypatch):
    """Every .yaml in agent_definitions/ loads without error."""
    # Mock all known env vars so ${VAR} resolution succeeds.
    for var in _ENV_VAR_TO_SETTINGS_ALIAS:
        monkeypatch.setenv(var, "mock/model")

    yaml_files = _all_yaml_files()
    assert yaml_files, "No YAML files found in agent_definitions/"

    for yf in yaml_files:
        ad = load_agent_definition(yf)
        assert ad.name, f"{yf.name} has empty name"


def test_all_real_yamls_parse(monkeypatch):
    """Alias for test_all_yaml_files_parse — validates every YAML
    in agent_definitions/ parses successfully.  Exists under this
    name to satisfy the acceptance-criteria checklist."""
    test_all_yaml_files_parse(monkeypatch)


def test_all_yaml_names_unique(monkeypatch):
    """No two YAML files share the same name."""
    for var in _ENV_VAR_TO_SETTINGS_ALIAS:
        monkeypatch.setenv(var, "mock/model")

    defs = _all_definitions()
    names = [ad.name for _, ad in defs]
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, f"Duplicate agent names: {dupes}"


def test_module_field_points_to_real_file(monkeypatch):
    """For each YAML with module set, the Python module exists.
    For each without module, derive from name and check."""
    for var in _ENV_VAR_TO_SETTINGS_ALIAS:
        monkeypatch.setenv(var, "mock/model")

    agents_dir = Path("src/robotsix_mill/agents")

    for yf, ad in _all_definitions():
        if ad.module:
            module_path = agents_dir / f"{ad.module}.py"
            assert module_path.is_file(), (
                f"{yf.name}: module '{ad.module}' → {module_path} does not exist"
            )
        else:
            # Derive from name by convention.
            module_path = agents_dir / f"{ad.name}.py"
            assert module_path.is_file(), (
                f"{yf.name}: no module field set and derived path "
                f"{module_path} does not exist"
            )


def test_output_type_exists_in_module(monkeypatch):
    """For each YAML with output_type set, the class exists in the module."""
    import importlib

    for var in _ENV_VAR_TO_SETTINGS_ALIAS:
        monkeypatch.setenv(var, "mock/model")

    for yf, ad in _all_definitions():
        if not ad.output_type:
            continue
        module_name = ad.module or ad.name
        mod = importlib.import_module(f"robotsix_mill.agents.{module_name}")
        cls = getattr(mod, ad.output_type, None)
        assert cls is not None, (
            f"{yf.name}: output_type '{ad.output_type}' not found "
            f"in robotsix_mill.agents.{module_name}"
        )


def test_tool_names_are_valid(monkeypatch):
    """Every tool name in each YAML's tools list is known-valid."""
    for var in _ENV_VAR_TO_SETTINGS_ALIAS:
        monkeypatch.setenv(var, "mock/model")

    for yf, ad in _all_definitions():
        for tool in ad.tools:
            assert tool in _VALID_TOOL_NAMES, (
                f"{yf.name}: unknown tool '{tool}'. Known: {sorted(_VALID_TOOL_NAMES)}"
            )


def test_category_is_valid(monkeypatch):
    """Every YAML with category set has a valid value."""
    for var in _ENV_VAR_TO_SETTINGS_ALIAS:
        monkeypatch.setenv(var, "mock/model")

    for yf, ad in _all_definitions():
        if ad.category is not None:
            assert ad.category in _VALID_CATEGORIES, (
                f"{yf.name}: invalid category '{ad.category}'. "
                f"Valid: {sorted(_VALID_CATEGORIES)}"
            )


def test_report_issue_consistency(monkeypatch):
    """Agents with output_type set SHOULD have report_issue: false.

    Documented exceptions: refine (output_type=RefineResult but
    report_issue=true because RefineResult is a spec, not a ticket).

    This is a soft check — violations emit a ``UserWarning`` (visible
    in test output) rather than failing the suite.
    """
    import warnings

    KNOWN_EXCEPTIONS = {
        "refine",
        "implement",
        "ci_fix",
        "rebase",
        "dedup",
        "obsolescence",
        "review_revision",
    }

    for var in _ENV_VAR_TO_SETTINGS_ALIAS:
        monkeypatch.setenv(var, "mock/model")

    for yf, ad in _all_definitions():
        if ad.output_type and ad.report_issue and ad.name not in KNOWN_EXCEPTIONS:
            warnings.warn(
                f"{yf.name}: has output_type={ad.output_type!r} AND "
                f"report_issue=true. Agents with structured output should "
                f"typically set report_issue: false to avoid double-drafting. "
                f"If this is intentional, add '{ad.name}' to KNOWN_EXCEPTIONS.",
                stacklevel=2,
            )


def test_interval_seconds_round_trips(tmp_path):
    """The new ``interval_seconds`` field on AgentDefinition parses
    from YAML and is exposed on the model."""
    yaml_body = (
        "name: demo\n"
        "level: 1\n"
        'system_prompt: "x"\n'
        "interval_seconds: 3600\n"
        "enabled: false\n"
    )
    p = _write_yaml(tmp_path, yaml_body)
    ad = load_agent_definition(p)
    assert ad.interval_seconds == 3600
    assert ad.enabled is False


def test_interval_human_readable_backfills_interval_seconds(tmp_path):
    """``interval: 1d`` parses and exposes ``interval_seconds == 86400``
    so downstream readers (worker.py) are unchanged."""
    p = _write_yaml(
        tmp_path,
        'name: demo\nlevel: 1\nsystem_prompt: "x"\ninterval: 1d\n',
    )
    ad = load_agent_definition(p)
    assert ad.interval == "1d"
    assert ad.interval_seconds == 86400


def test_interval_and_interval_seconds_mutually_exclusive(tmp_path):
    """Setting both ``interval`` and ``interval_seconds`` → ValidationError."""
    p = _write_yaml(
        tmp_path,
        'name: demo\nlevel: 1\nsystem_prompt: "x"\n'
        "interval: 1d\ninterval_seconds: 86400\n",
    )
    with pytest.raises(ValidationError):  # pydantic.ValidationError
        load_agent_definition(p)


def test_interval_malformed_raises_validation_error(tmp_path):
    """A malformed ``interval`` string surfaces as a ValidationError."""
    p = _write_yaml(
        tmp_path,
        'name: demo\nlevel: 1\nsystem_prompt: "x"\ninterval: 1x\n',
    )
    with pytest.raises(ValidationError):  # pydantic.ValidationError
        load_agent_definition(p)


def test_interval_seconds_defaults_to_none(tmp_path):
    """Omitted ``interval_seconds`` / ``enabled`` keep ``None`` so the
    worker falls back to the corresponding Settings field."""
    p = _write_yaml(
        tmp_path,
        'name: demo\nlevel: 1\nsystem_prompt: "x"\n',
    )
    ad = load_agent_definition(p)
    assert ad.interval_seconds is None
    assert ad.enabled is None


def test_load_periodic_agent_definition_falls_back_to_builtin():
    """Without a repo_dir override, the loader returns the built-in
    periodic YAML — sanity check that audit.yaml is discoverable."""
    ad = load_periodic_agent_definition("audit")
    assert ad.name == "audit"
    assert ad.category == "periodic"
    # Audit shipped with the schedule fields filled in.
    assert ad.interval_seconds == 86400
    assert ad.enabled is True


def test_load_periodic_agent_definition_repo_override_wins(tmp_path):
    """A clone-side ``<repo_dir>/.robotsix-mill/agents/audit.yaml``
    fully replaces the built-in definition."""
    agents_dir = tmp_path / ".robotsix-mill" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "audit.yaml").write_text(
        "name: audit\n"
        "level: 1\n"
        'system_prompt: "per-repo audit"\n'
        "interval_seconds: 43200\n"
        "enabled: true\n",
        encoding="utf-8",
    )
    ad = load_periodic_agent_definition("audit", repo_dir=tmp_path)
    assert ad.interval_seconds == 43200
    assert ad.system_prompt == "per-repo audit"


def test_every_real_yaml_declares_a_valid_level(monkeypatch):
    """Every agent_definitions/*.yaml declares a capability ``level`` in the
    1..3 range (the env-var ``${MILL_*_MODEL}`` substitution path is gone)."""
    for var in _ENV_VAR_TO_SETTINGS_ALIAS:
        monkeypatch.setenv(var, "mock/model")

    for yf, ad in _all_definitions():
        assert ad.level in (1, 2, 3), f"{yf.name}: level {ad.level} out of range"
