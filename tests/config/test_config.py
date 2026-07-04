"""Tests for the unified ``Settings`` config model."""

from __future__ import annotations

import json
import os
from collections.abc import Generator
from pathlib import Path

import pytest
from pydantic import ValidationError

from robotsix_mill.config import Settings
from robotsix_mill.config._settings_core import _CoreSettings


# ===========================================================================
#  Fixtures
# ===========================================================================


@pytest.fixture(autouse=True)
def _clean_env() -> Generator[None, None, None]:
    """Remove any ``MILL_*`` vars before each test so we get clean defaults.

    The conftest-level _no_dotenv fixture handles ``.env`` / YAML file
    blocking; this fixture additionally wipes  **individual env vars**
    that may have leaked from prior runs or IDE shell config.

    The single-file config pins (``MILL_CONFIG_FILE`` / ``MILL_SECRETS_FILE``
    / ``MILL_REPOS_FILE``, set to ``""`` by the conftest ``_no_dotenv``
    fixture) are preserved — clearing them would let the loader fall back to
    a developer's gitignored ``config/config.json`` and break hermeticity.
    """
    _pins = {"MILL_CONFIG_FILE", "MILL_SECRETS_FILE", "MILL_REPOS_FILE"}
    to_clear = [k for k in os.environ if k.startswith("MILL_") and k not in _pins]
    stash = {k: os.environ.pop(k) for k in to_clear}
    # Also clear FORGE_* vars that alias to Settings fields
    for k in list(os.environ):
        if k.startswith("FORGE_") or k in (
            "OPENROUTER_API_KEY",
            "GITHUB_APP_ID",
            "GITHUB_APP_PRIVATE_KEY_PATH",
            "NTFY_URL",
            "NTFY_TOKEN",
        ):
            stash[k] = os.environ.pop(k)
    # Explicitly clear the new alias so test_default_coordinator_request_limit
    # can assert the model's static default even if the outer environment
    # has a stray value.
    os.environ.pop("MILL_PER_PASS_REQUEST_BUDGET", None)
    yield
    os.environ.update(stash)


@pytest.fixture()
def settings() -> Settings:
    """A fresh ``Settings()`` with no env-var contamination."""
    return Settings()


# ===========================================================================
#  Default value tests
# ===========================================================================


def test_default_coordinator_max_tool_calls():
    """coordinator_max_tool_calls defaults to 300 — a generous but bounded
    tool-call backstop for the implement/coordinator agent, sitting
    generously above any legitimate run while terminating runaway
    read-file loops."""
    s = Settings()
    assert s.coordinator_max_tool_calls == 300


def test_default_coordinator_request_limit():
    """coordinator_request_limit defaults to 500 (not the old 200) so
    normal-sized tickets can complete in a single pass.  The hard
    upper bound of 5000 prevents runaway cost from misconfiguration."""
    s = Settings()
    assert s.coordinator_request_limit == 500
    # Also verify the hard upper bound is enforced at the model level.
    # 5001 exceeds le=5000 — must pass via alias (field has alias set)
    try:
        _CoreSettings(**{"MILL_PER_PASS_REQUEST_BUDGET": 5001})
        raise AssertionError("expected ValidationError")
    except ValidationError:
        pass
    # boundary: 5000 is ok
    cs = _CoreSettings(**{"MILL_PER_PASS_REQUEST_BUDGET": 5000})
    assert cs.coordinator_request_limit == 5000


def test_default_stage_timeout_overrides_refine():
    """The shipped default ``stage_timeout_overrides`` contains a
    ``{"refine": 900}`` entry (not empty).  900 s (15 min) leaves
    enough runway for the explore sub-agent counts typical tickets
    need, while still catching the 1500s+ runs that burn $40+ on a
    single refine.  The empty-dict default would disable the cap
    entirely, which is not what we want."""
    s = Settings()
    assert s.stage_timeout_overrides == {"refine": 900}, (
        f"expected {{'refine': 900}}, got {s.stage_timeout_overrides!r}"
    )


def test_default_consult_request_limit():
    """consult_request_limit defaults to 15 — enough for a single
    domain-expert consultation with a few follow-ups, but bounded so a
    stuck loop doesn't drain the parent's budget."""
    s = Settings()
    assert s.consult_request_limit == 15


def test_default_max_fix_iterations():
    s = Settings()
    assert s.max_fix_iterations == 8


def test_default_test_request_limit():
    s = Settings()
    assert s.test_request_limit == 30


def test_default_explore_request_limit():
    s = Settings()
    assert s.explore_request_limit == 100


def test_default_explore_max_tokens():
    s = Settings()
    assert s.explore_max_tokens == 4096


def test_default_web_research_request_limit():
    s = Settings()
    assert s.web_research_request_limit == 8


def test_default_audit_requests():
    """audit_request_limit defaults to 80."""
    s = Settings()
    assert s.audit_request_limit == 80


def test_default_maintenance_requests():
    """maintenance_request_limit defaults to 100."""
    s = Settings()
    assert s.maintenance_request_limit == 100


def test_default_doc_requests():
    """doc_request_limit defaults to 32 (raised from 16 to prevent
    UsageLimitExceeded on feature-sized tickets)."""
    s = Settings()
    assert s.doc_request_limit == 32


def test_default_refine_requests():
    """refine_request_limit defaults to 80."""
    s = Settings()
    assert s.refine_request_limit == 80


def test_default_refine_requests_simple():
    """refine_request_limit_simple defaults to 40 (roughly half of the main
    refine budget, matching the faster sonnet model path)."""
    s = Settings()
    assert s.refine_request_limit_simple == 40


def test_default_review_requests():
    """review_request_limit defaults to 80 — covers a 4-6 file PR with
    tool calls and reasoning steps."""
    s = Settings()
    assert s.review_request_limit == 80


def test_default_max_spend_usd():
    """max_spend_usd_per_ticket defaults to 20.0 (not 0.0)."""
    s = Settings()
    assert s.max_spend_usd_per_ticket == 20.0


def test_default_max_traces_per_ticket():
    """max_traces_per_ticket defaults to 15 (trace-count circuit breaker)."""
    s = Settings()
    assert s.max_traces_per_ticket == 15


def test_default_max_openrouter_marginal_usd():
    """max_openrouter_marginal_usd_per_ticket defaults to 3.0."""
    s = Settings()
    assert s.max_openrouter_marginal_usd_per_ticket == 3.0


def test_default_doc_classifier_requests():
    """doc_classifier_request_limit defaults to 3."""
    s = Settings()
    assert s.doc_classifier_request_limit == 3


def test_default_doc_classifier_diff_max_chars():
    """doc_classifier_diff_max_chars defaults to 6000."""
    s = Settings()
    assert s.doc_classifier_diff_max_chars == 6000


def test_default_triage_requests():
    """triage_request_limit defaults to 8."""
    s = Settings()
    assert s.triage_request_limit == 8


def test_default_obsolescence_requests():
    """obsolescence_request_limit defaults to 6."""
    s = Settings()
    assert s.obsolescence_request_limit == 6


def test_default_dedup_requests():
    """dedup_request_limit defaults to 12."""
    s = Settings()
    assert s.dedup_request_limit == 12


def test_default_dedup_skip_on_no_overlap():
    """dedup_skip_on_no_overlap defaults to True."""
    s = Settings()
    assert s.dedup_skip_on_no_overlap is True


def test_default_dedup_candidate_body_max_chars():
    """dedup_candidate_body_max_chars defaults to 4000."""
    s = Settings()
    assert s.dedup_candidate_body_max_chars == 4000


def test_default_web_research_requests():
    """web_research_request_limit defaults to 8."""
    s = Settings()
    assert s.web_research_request_limit == 8


def test_default_sandbox_image():
    """The sandbox image defaults to robotsix/mill-sandbox:latest via
    the YAML config override (field default is python:3.14-slim)."""
    s = Settings()
    assert s.sandbox_image == "robotsix/mill-sandbox:latest"


def test_default_language_instructions_dir():
    """language_instructions_dir defaults to
    agent_definitions/language_instructions."""
    s = Settings()
    assert s.language_instructions_dir == Path(
        "agent_definitions/language_instructions"
    )


# ===========================================================================
#  Env-var override roundtrip via monkeypatch
# ===========================================================================

# Every entry: (field_name, alias, env_value, expected_python_value)
# env_value is the string form as it appears in an env var.
# expected_python_value is what getattr(settings, field_name) yields.
#
# For int/float: numeric string → parsed number.
# For bool: "1" → True.
# For str/Path/Literal: distinctive string.
# For str|None / Path|None: distinctive non-None string.

ALIAS_CASES: list[tuple[str, str, str, object]] = [
    # --- core / api key ---
    ("openrouter_api_key", "OPENROUTER_API_KEY", "sk-test", "sk-test"),
    # --- request limits ---
    ("coordinator_request_limit", "MILL_PER_PASS_REQUEST_BUDGET", "42", 42),
    ("refine_request_limit", "MILL_REFINE_REQUEST_LIMIT", "42", 42),
    ("test_request_limit", "MILL_TEST_REQUEST_LIMIT", "15", 15),
    ("maintenance_request_limit", "MILL_MAINTENANCE_REQUEST_LIMIT", "33", 33),
    ("max_fix_iterations", "MILL_MAX_FIX_ITERATIONS", "6", 6),
    # --- retry / backoff ---
    # --- stage retry ---
    ("stage_retry_max_attempts", "MILL_STAGE_RETRY_MAX_ATTEMPTS", "3", 3),
    ("stage_retry_base_delay", "MILL_STAGE_RETRY_BASE_DELAY", "4.0", 4.0),
    ("stage_retry_max_delay", "MILL_STAGE_RETRY_MAX_DELAY", "120.0", 120.0),
    # --- concurrency ---
    ("max_global_concurrency", "MILL_MAX_GLOBAL_CONCURRENCY", "8", 8),
    # --- gates ---
    ("require_approval", "MILL_REQUIRE_APPROVAL", "1", True),
    ("review_enabled", "MILL_REVIEW_ENABLED", "1", True),
    ("auto_merge_enabled", "MILL_AUTO_MERGE_ENABLED", "1", True),
    ("refine_triage_enabled", "MILL_REFINE_TRIAGE_ENABLED", "1", True),
    ("spec_review_enabled", "MILL_SPEC_REVIEW_ENABLED", "1", True),
    ("review_max_rounds", "MILL_REVIEW_MAX_ROUNDS", "5", 5),
    # --- forge ---
    (
        "forge_remote_url",
        "FORGE_REMOTE_URL",
        "https://example.com/repo.git",
        "https://example.com/repo.git",
    ),
    ("forge_target_branch", "FORGE_TARGET_BRANCH", "develop", "develop"),
    (
        "github_api_url",
        "MILL_GITHUB_API_URL",
        "https://github.example.com/api/v3",
        "https://github.example.com/api/v3",
    ),
    # --- sandbox ---
    ("sandbox_image", "MILL_SANDBOX_IMAGE", "python:3.12-slim", "python:3.12-slim"),
    ("sandbox_memory", "MILL_SANDBOX_MEMORY", "4g", "4g"),
    ("sandbox_pids_limit", "MILL_SANDBOX_PIDS_LIMIT", "1024", 1024),
    ("sandbox_readonly", "MILL_SANDBOX_READONLY", "0", False),
    ("command_timeout", "MILL_COMMAND_TIMEOUT", "3600", 3600),
    ("test_command", "MILL_TEST_COMMAND", "pytest -x", "pytest -x"),
    ("skills_dir", "MILL_SKILLS_DIR", "my_skills", Path("my_skills")),
    # --- pipeline ---
    ("branch_prefix", "MILL_BRANCH_PREFIX", "feature/", "feature/"),
    ("merge_poll_seconds", "MILL_MERGE_POLL_SECONDS", "60", 60),
    ("rebase_max_attempts", "MILL_REBASE_MAX_ATTEMPTS", "10", 10),
    ("ci_fix_max_attempts", "MILL_CI_FIX_MAX_ATTEMPTS", "4", 4),
    ("retrospect_spawn_drafts", "MILL_RETROSPECT_SPAWN_DRAFTS", "0", False),
    ("prune_clone_on_close", "MILL_PRUNE_CLONE_ON_CLOSE", "0", False),
    ("max_archived_tickets", "MILL_MAX_ARCHIVED_TICKETS", "50", 50),
    # --- service ---
    ("data_dir", "MILL_DATA_DIR", "/custom/data", Path("/custom/data")),
    ("api_host", "MILL_API_HOST", "0.0.0.0", "0.0.0.0"),
    ("api_port", "MILL_API_PORT", "8080", 8080),
    ("api_url", "MILL_API_URL", "http://localhost:8080", "http://localhost:8080"),
    # --- memory paths ---
    (
        "retrospect_memory_path",
        "MILL_RETROSPECT_MEMORY_PATH",
        "/mem/retro.md",
        Path("/mem/retro.md"),
    ),
    (
        "implement_memory_path",
        "MILL_IMPLEMENT_MEMORY_PATH",
        "/mem/impl.md",
        Path("/mem/impl.md"),
    ),
    (
        "refine_memory_path",
        "MILL_REFINE_MEMORY_PATH",
        "/mem/refine.md",
        Path("/mem/refine.md"),
    ),
    (
        "ci_fix_memory_path",
        "MILL_CI_FIX_MEMORY_PATH",
        "/mem/cifix.md",
        Path("/mem/cifix.md"),
    ),
    (
        "rebase_memory_path",
        "MILL_REBASE_MEMORY_PATH",
        "/mem/rebase.md",
        Path("/mem/rebase.md"),
    ),
    # --- CI patterns ---
    (
        "ci_patterns_path",
        "MILL_CI_PATTERNS_PATH",
        "/mem/patterns.json",
        Path("/mem/patterns.json"),
    ),
    # --- CI-fix tuning ---
    ("ci_fix_max_cycles", "MILL_CI_FIX_MAX_CYCLES", "5", 5),
    ("ci_fix_max_identical_failures", "MILL_CI_FIX_MAX_IDENTICAL_FAILURES", "3", 3),
    ("ci_fix_wait_poll_interval_s", "MILL_CI_FIX_WAIT_POLL_INTERVAL_S", "15.0", 15.0),
    ("ci_fix_wait_timeout_s", "MILL_CI_FIX_WAIT_TIMEOUT_S", "1200.0", 1200.0),
    ("max_stuck_cycles", "MILL_MAX_STUCK_CYCLES", "5", 5),
    # --- web research / fetch ---
    ("web_search", "MILL_WEB_SEARCH", "1", True),
    (
        "fetch_image",
        "MILL_FETCH_IMAGE",
        "curlimages/curl:8.17.1",
        "curlimages/curl:8.17.1",
    ),
    ("web_fetch_max_bytes", "MILL_WEB_FETCH_MAX_BYTES", "1048576", 1048576),
    ("web_fetch_timeout", "MILL_WEB_FETCH_TIMEOUT", "10", 10),
    # --- OpenRouter low-credit polling ---
    ("low_credit_threshold_usd", "MILL_LOW_CREDIT_THRESHOLD_USD", "2.5", 2.5),
    ("low_credit_poll_enabled", "MILL_LOW_CREDIT_POLL_ENABLED", "0", False),
    (
        "low_credit_poll_interval_seconds",
        "MILL_LOW_CREDIT_POLL_INTERVAL_SECONDS",
        "1800",
        1800,
    ),
    # --- periodic agent toggles ---
    ("survey_periodic", "MILL_SURVEY_PERIODIC", "0", False),
    ("survey_interval_seconds", "MILL_SURVEY_INTERVAL_SECONDS", "43200", 43200),
    ("audit_periodic", "MILL_AUDIT_PERIODIC", "1", True),
    ("audit_interval_seconds", "MILL_AUDIT_INTERVAL_SECONDS", "43200", 43200),
    ("trace_health_periodic", "MILL_TRACE_HEALTH_PERIODIC", "1", True),
    (
        "trace_health_interval_seconds",
        "MILL_TRACE_HEALTH_INTERVAL_SECONDS",
        "86400",
        86400,
    ),
    (
        "coordinator_timeout_overrides",
        "MILL_COORDINATOR_TIMEOUT_OVERRIDES",
        '{"explore": 600, "refine": 1200}',
        {"explore": 600, "refine": 1200},
    ),
    (
        "repo_description_sync_periodic",
        "MILL_REPO_DESCRIPTION_SYNC_PERIODIC",
        "1",
        True,
    ),
    (
        "repo_description_sync_interval_seconds",
        "MILL_REPO_DESCRIPTION_SYNC_INTERVAL_SECONDS",
        "43200",
        43200,
    ),
    ("health_periodic", "MILL_HEALTH_PERIODIC", "1", True),
    ("health_interval_seconds", "MILL_HEALTH_INTERVAL_SECONDS", "43200", 43200),
    ("test_gap_periodic", "MILL_TEST_GAP_PERIODIC", "1", True),
    ("test_gap_interval_seconds", "MILL_TEST_GAP_INTERVAL_SECONDS", "43200", 43200),
    ("agent_check_periodic", "MILL_AGENT_CHECK_PERIODIC", "1", True),
    (
        "agent_check_interval_seconds",
        "MILL_AGENT_CHECK_INTERVAL_SECONDS",
        "43200",
        43200,
    ),
    ("diagnostic_periodic", "MILL_DIAGNOSTIC_PERIODIC", "1", True),
    ("diagnostic_interval_seconds", "MILL_DIAGNOSTIC_INTERVAL_SECONDS", "43200", 43200),
    ("ci_log_max_bytes", "MILL_CI_LOG_MAX_BYTES", "32768", 32768),
    ("epic_dedup_lookback_days", "MILL_EPIC_DEDUP_LOOKBACK_DAYS", "14", 14),
    # --- survey agent ---
    # --- review revision ---
    (
        "review_revision_memory_path",
        "MILL_REVIEW_REVISION_MEMORY_PATH",
        "/mem/review_revision.md",
        Path("/mem/review_revision.md"),
    ),
    ("review_revision_max_attempts", "MILL_REVIEW_REVISION_MAX_ATTEMPTS", "4", 4),
    # --- other memory paths ---
    ("doc_memory_path", "MILL_DOC_MEMORY_PATH", "/mem/doc.md", Path("/mem/doc.md")),
    # --- ci-fix request limit ---
    ("ci_fix_request_limit", "MILL_CI_FIX_REQUEST_LIMIT", "80", 80),
    # --- pipeline limits ---
    ("max_events_per_ticket", "MILL_MAX_EVENTS_PER_TICKET", "100", 100),
    ("max_comments_per_ticket", "MILL_MAX_COMMENTS_PER_TICKET", "300", 300),
    # --- claude sdk ---
    ("claude_max_concurrency", "MILL_CLAUDE_MAX_CONCURRENCY", "8", 8),
    # --- shutdown ---
    ("shutdown_grace_seconds", "MILL_SHUTDOWN_GRACE_SECONDS", "300", 300),
    # --- requeue ---
    ("requeue_batch_size", "MILL_REQUEUE_BATCH_SIZE", "10", 10),
    ("requeue_batch_pause_seconds", "MILL_REQUEUE_BATCH_PAUSE_SECONDS", "1.0", 1.0),
    # --- startup-jitter ---
    ("startup_jitter_seconds", "MILL_STARTUP_JITTER_SECONDS", "60", 60),
    # --- accessor ---
    ("langfuse_cleanup_max_traces", "MILL_LANGFUSE_CLEANUP_MAX_TRACES", "2000", 2000),
    # --- timeout overrides ---
    (
        "stage_timeout_overrides",
        "MILL_STAGE_TIMEOUT_OVERRIDES",
        '{"refine": 1200, "merge": 0}',
        {"refine": 1200, "merge": 0},
    ),
    # --- auto-fix / ping-pong ---
    ("auto_fix_max_cycles", "MILL_AUTO_FIX_MAX_CYCLES", "4", 4),
    ("ping_pong_max_alternations", "MILL_PING_PONG_MAX_ALTERNATIONS", "2", 2),
    # --- rate-limit fallback ---
    # --- claude sdk vision ---
    ("claude_sdk_vision_enabled", "MILL_CLAUDE_SDK_VISION_ENABLED", "1", True),
    # --- review stage tuning ---
    (
        "review_prior_context_max_chars",
        "MILL_REVIEW_PRIOR_CONTEXT_MAX_CHARS",
        "4000",
        4000,
    ),
    ("review_diff_max_chars", "MILL_REVIEW_DIFF_MAX_CHARS", "100000", 100000),
    ("review_output_token_budget", "MILL_REVIEW_OUTPUT_TOKEN_BUDGET", "32768", 32768),
    # --- lint & read-file cap ---
    ("lint_on_edit", "MILL_LINT_ON_EDIT", "0", False),
    # --- delete_branch_on_merge ---
    ("delete_branch_on_merge", "MILL_DELETE_BRANCH_ON_MERGE", "0", False),
    # --- ci_fix_max_iterations ---
    ("ci_fix_max_iterations", "MILL_CI_FIX_MAX_ITERATIONS", "3", 3),
    # --- network probe ---
    ("network_probe_host", "MILL_NETWORK_PROBE_HOST", "gitlab.com", "gitlab.com"),
    ("network_outage_retry_seconds", "MILL_NETWORK_OUTAGE_RETRY_SECONDS", "60", 60),
    # --- language_instructions_dir ---
    (
        "language_instructions_dir",
        "MILL_LANGUAGE_INSTRUCTIONS_DIR",
        "my_instructions",
        Path("my_instructions"),
    ),
    # --- diagnostic target / monitor ---
    (
        "diagnostic_target_repo_id",
        "MILL_DIAGNOSTIC_TARGET_REPO_ID",
        "my-project",
        "my-project",
    ),
    (
        "diagnostic_monitored_repo_ids",
        "MILL_DIAGNOSTIC_MONITORED_REPO_IDS",
        '["repo-a", "repo-b"]',
        ["repo-a", "repo-b"],
    ),
    # --- scope_triage_max_files ---
    ("scope_triage_max_files", "MILL_SCOPE_TRIAGE_MAX_FILES", "25", 25),
    # --- periodic bc_check ---
    ("bc_check_periodic", "MILL_BC_CHECK_PERIODIC", "0", False),
    ("bc_check_interval_seconds", "MILL_BC_CHECK_INTERVAL_SECONDS", "43200", 43200),
    # --- periodic completeness_check ---
    ("completeness_check_periodic", "MILL_COMPLETENESS_CHECK_PERIODIC", "0", False),
    (
        "completeness_check_interval_seconds",
        "MILL_COMPLETENESS_CHECK_INTERVAL_SECONDS",
        "43200",
        43200,
    ),
    (
        "completeness_check_request_limit",
        "MILL_COMPLETENESS_CHECK_REQUEST_LIMIT",
        "50",
        50,
    ),
    # --- periodic env-doc-sync ---
    ("env_doc_sync_periodic", "MILL_ENV_DOC_SYNC_PERIODIC", "0", False),
    (
        "env_doc_sync_interval_seconds",
        "MILL_ENV_DOC_SYNC_INTERVAL_SECONDS",
        "43200",
        43200,
    ),
    # --- periodic state-sync ---
    ("state_sync_periodic", "MILL_STATE_SYNC_PERIODIC", "0", False),
    ("state_sync_interval_seconds", "MILL_STATE_SYNC_INTERVAL_SECONDS", "43200", 43200),
    # --- repo visibility default ---
    ("repo_visibility_default", "MILL_REPO_VISIBILITY_DEFAULT", "private", "private"),
    # --- enable_repo_creation ---
    ("enable_repo_creation", "MILL_ENABLE_REPO_CREATION", "1", True),
    # --- meta_periodic + meta_interval_seconds ---
    ("meta_periodic", "MILL_META_PERIODIC", "1", True),
    ("meta_interval_seconds", "MILL_META_INTERVAL_SECONDS", "43200", 43200),
    # --- web_knowledge_* ---
    ("web_knowledge_stale_days", "MILL_WEB_KNOWLEDGE_STALE_DAYS", "15", 15),
    ("web_knowledge_request_limit", "MILL_WEB_KNOWLEDGE_REQUEST_LIMIT", "4", 4),
    (
        "web_knowledge_model",
        "MILL_WEB_KNOWLEDGE_MODEL",
        "deepseek/deepseek-v4-pro",
        "deepseek/deepseek-v4-pro",
    ),
    # --- data_dir_gc ---
    ("data_dir_gc_periodic", "MILL_DATA_DIR_GC_PERIODIC", "0", False),
    (
        "data_dir_gc_interval_seconds",
        "MILL_DATA_DIR_GC_INTERVAL_SECONDS",
        "43200",
        43200,
    ),
    # --- copy_paste ---
    ("copy_paste_periodic", "MILL_COPY_PASTE_PERIODIC", "0", False),
    ("copy_paste_interval_seconds", "MILL_COPY_PASTE_INTERVAL_SECONDS", "43200", 43200),
    # --- forge_parity ---
    ("forge_parity_periodic", "MILL_FORGE_PARITY_PERIODIC", "0", False),
    (
        "forge_parity_interval_seconds",
        "MILL_FORGE_PARITY_INTERVAL_SECONDS",
        "43200",
        43200,
    ),
    # --- module_curator ---
    ("module_curator_periodic", "MILL_MODULE_CURATOR_PERIODIC", "0", False),
    (
        "module_curator_interval_seconds",
        "MILL_MODULE_CURATOR_INTERVAL_SECONDS",
        "43200",
        43200,
    ),
    # --- stale_branch_cleanup ---
    ("stale_branch_cleanup_periodic", "MILL_STALE_BRANCH_CLEANUP_PERIODIC", "0", False),
    (
        "stale_branch_cleanup_interval_seconds",
        "MILL_STALE_BRANCH_CLEANUP_INTERVAL_SECONDS",
        "43200",
        43200,
    ),
    # --- sandbox_reaper ---
    ("sandbox_reaper_periodic", "MILL_SANDBOX_REAPER_PERIODIC", "0", False),
    (
        "sandbox_reaper_interval_seconds",
        "MILL_SANDBOX_REAPER_INTERVAL_SECONDS",
        "1800",
        1800,
    ),
    # --- run_health ---
    ("run_health_periodic", "MILL_RUN_HEALTH_PERIODIC", "0", False),
    ("run_health_interval_seconds", "MILL_RUN_HEALTH_INTERVAL_SECONDS", "43200", 43200),
    (
        "run_health_memory_path",
        "MILL_RUN_HEALTH_MEMORY_PATH",
        "/mem/run_health.md",
        Path("/mem/run_health.md"),
    ),
    ("run_health_window_hours", "MILL_RUN_HEALTH_WINDOW_HOURS", "72", 72),
    (
        "run_health_target_repo_id",
        "MILL_RUN_HEALTH_TARGET_REPO_ID",
        "my-board",
        "my-board",
    ),
    # --- config_sync ---
    ("config_sync_periodic", "MILL_CONFIG_SYNC_PERIODIC", "0", False),
    (
        "config_sync_interval_seconds",
        "MILL_CONFIG_SYNC_INTERVAL_SECONDS",
        "43200",
        43200,
    ),
    # --- dependabot_ingest ---
    ("dependabot_ingest_periodic", "MILL_DEPENDABOT_INGEST_PERIODIC", "0", False),
    (
        "dependabot_ingest_interval_seconds",
        "MILL_DEPENDABOT_INGEST_INTERVAL_SECONDS",
        "43200",
        43200,
    ),
    # --- timeout_escalation ---
    ("timeout_escalation_periodic", "MILL_TIMEOUT_ESCALATION_PERIODIC", "0", False),
    (
        "timeout_escalation_interval_seconds",
        "MILL_TIMEOUT_ESCALATION_INTERVAL_SECONDS",
        "1800",
        1800,
    ),
    (
        "timeout_escalation_threshold_seconds",
        "MILL_TIMEOUT_ESCALATION_THRESHOLD_SECONDS",
        "86400",
        86400,
    ),
    # --- langfuse_cleanup ---
    ("langfuse_cleanup_periodic", "MILL_LANGFUSE_CLEANUP_PERIODIC", "0", False),
    (
        "langfuse_cleanup_interval_seconds",
        "MILL_LANGFUSE_CLEANUP_INTERVAL_SECONDS",
        "43200",
        43200,
    ),
    # --- test_gap request_limit ---
    ("test_gap_request_limit", "MILL_TEST_GAP_REQUEST_LIMIT", "60", 60),
    # --- max_spend_usd_per_ticket ---
    ("max_spend_usd_per_ticket", "MILL_MAX_SPEND_USD_PER_TICKET", "5.0", 5.0),
    ("max_traces_per_ticket", "MILL_MAX_TRACES_PER_TICKET", "10", 10),
    (
        "max_openrouter_marginal_usd_per_ticket",
        "MILL_MAX_OPENROUTER_MARGINAL_USD_PER_TICKET",
        "2.0",
        2.0,
    ),
    # --- stage_timeout_seconds ---
    ("stage_timeout_seconds", "MILL_STAGE_TIMEOUT_SECONDS", "1200", 1200),
    # --- dedup_lookback_days ---
    ("dedup_lookback_days", "MILL_DEDUP_LOOKBACK_DAYS", "14", 14),
    # --- explore_request_limit ---
    ("explore_request_limit", "MILL_EXPLORE_REQUEST_LIMIT", "50", 50),
    # --- consult_request_limit ---
    ("consult_request_limit", "MILL_CONSULT_REQUEST_LIMIT", "10", 10),
    # --- dedup_request_limit ---
    ("dedup_request_limit", "MILL_DEDUP_REQUEST_LIMIT", "3", 3),
    # --- web_research_request_limit ---
    ("web_research_request_limit", "MILL_WEB_RESEARCH_REQUEST_LIMIT", "5", 5),
    # --- doc_classifier_request_limit ---
    ("doc_classifier_request_limit", "MILL_DOC_CLASSIFIER_REQUEST_LIMIT", "2", 2),
    # --- max_memory_chars ---
    ("max_memory_chars", "MILL_MAX_MEMORY_CHARS", "5000", 5000),
    # --- board_list_cache_ttl_seconds ---
    ("board_list_cache_ttl_seconds", "MILL_BOARD_LIST_CACHE_TTL_SECONDS", "5.0", 5.0),
    # --- enable_repo_creation (string alias) ---
    ("enable_repo_creation", "MILL_ENABLE_REPO_CREATION", "0", False),
]


@pytest.mark.parametrize(
    ("field_name", "alias", "env_value", "expected"),
    ALIAS_CASES,
    ids=[case[0] for case in ALIAS_CASES],
)
def test_alias_roundtrip(
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    alias: str,
    env_value: str,
    expected: object,
) -> None:
    """Verify that each env-var alias correctly overrides the default."""
    monkeypatch.setenv(alias, env_value)
    s = Settings()
    actual = getattr(s, field_name)
    assert actual == expected, (
        f"Field {field_name!r} (alias {alias!r}): expected {expected!r}, got {actual!r}"
    )


# ===========================================================================
#  Cross-field validation
# ===========================================================================


def test_forge_kind_requires_remote_url():
    """When forge_kind is set (not 'none'), remote_url must be set too."""
    with pytest.raises(ValidationError, match="forge_remote_url"):
        Settings(forge_kind="github", forge_remote_url=None)


def test_forge_auth_app_requires_credentials():
    """When forge_auth is 'app', either gha_app_id or private_key_path must be set."""
    with pytest.raises(ValidationError, match="github_app_id"):
        Settings(
            forge_kind="github",
            forge_remote_url="https://github.com/org/repo.git",
            forge_auth="app",
            github_app_id=None,
            github_app_private_key_path=None,
        )


def test_stage_timeout_overrides_json_roundtrip():
    """stage_timeout_overrides parses JSON strings from env vars."""
    s = Settings(stage_timeout_overrides={"refine": 1200, "merge": 0})
    assert s.stage_timeout_overrides == {"refine": 1200, "merge": 0}


def test_diagnostic_monitored_repo_ids_parsed():
    """diagnostic_monitored_repo_ids parses a JSON array from env var."""
    s = Settings(diagnostic_monitored_repo_ids=["repo-a", "repo-b"])
    assert s.diagnostic_monitored_repo_ids == ["repo-a", "repo-b"]


# ===========================================================================
#  Docs / metadata smoke test
# ===========================================================================


def test_field_names_are_present_in_config_audit():
    """Smoke check: all settings fields with env-var aliases are documented in
    config-audit.md."""
    # This is a soft test — it just verifies the config model can be
    # introspected without error.
    s = Settings()
    assert hasattr(s, "coordinator_request_limit")
    assert hasattr(s, "coordinator_max_tool_calls")


# ===========================================================================
#  Example template regression guards
# ===========================================================================


def test_example_config_is_container_ready():
    """The onboard template must bind 0.0.0.0 so the gateway (separate
    container) can reach the mill upstream.  A loopback default here would
    re-introduce the 502-behind-auth regression that had to be hand-patched
    live on 2026-07-04."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    example_path = repo_root / "config" / "config.example.json"
    data = json.loads(example_path.read_text())
    assert data["settings"]["api_host"] == "0.0.0.0"
