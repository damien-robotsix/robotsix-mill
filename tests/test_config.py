"""Unit tests for src/robotsix_mill/config.py — Settings defaults, env-var
aliases, type coercion, computed properties, YAML loading, Secrets model,
semantic validators, and edge cases."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from robotsix_mill.config import Settings, load_settings


# ---------------------------------------------------------------------------
# 1. Default values
# ---------------------------------------------------------------------------

def test_default_model_settings():
    """Representative model-name defaults."""
    s = Settings()
    assert s.model == "deepseek/deepseek-v4-pro"
    assert s.triage_model == "openai/gpt-4o-mini"
    assert s.explore_model == "deepseek/deepseek-v4-flash"
    assert s.test_model == "deepseek/deepseek-v4-pro"
    assert s.auto_approve_model == "openai/gpt-4o-mini"


def test_default_numeric_limits():
    """Representative integer / float defaults."""
    s = Settings()
    assert s.max_concurrency == 4
    assert s.transient_retries == 4
    assert s.command_timeout == 900
    assert s.max_fix_iterations == 8
    assert s.model_request_timeout == 900.0
    assert s.transient_backoff_cap == 30.0


def test_doc_request_limit_default():
    """MILL_DOC_REQUEST_LIMIT defaults to 8."""
    s = Settings()
    assert s.doc_request_limit == 8


def test_doc_request_limit_env(monkeypatch):
    """MILL_DOC_REQUEST_LIMIT=8 flows into the Settings field."""
    monkeypatch.setenv("MILL_DOC_REQUEST_LIMIT", "8")
    s = Settings()
    assert s.doc_request_limit == 8


def test_default_booleans():
    """Representative boolean defaults."""
    s = Settings()
    assert s.web_search is True
    assert s.require_approval is True
    assert s.sandbox_readonly is True
    assert s.auto_approve_enabled is False
    assert s.review_enabled is False


def test_default_paths(monkeypatch):
    """data_dir default and a couple of Path-typed fields.

    The container may have MILL_DATA_DIR set in its environment;
    explicitly clear it so we get the real default."""
    monkeypatch.delenv("MILL_DATA_DIR", raising=False)
    monkeypatch.delenv("MILL_SKILLS_DIR", raising=False)
    s = Settings()
    assert s.data_dir == Path(".mill-data")
    assert s.skills_dir == Path("skills")


def test_default_empty_and_none():
    """Representative empty-string, None, and sentinel defaults."""
    s = Settings()
    assert s.forge_kind == "none"
    assert s.openrouter_api_key is None
    assert s.rate_limit_fallback_model == ""
    assert s.forge_remote_url is None
    assert s.langfuse_base_url is None


def test_default_max_spend_sentinel():
    """max_spend_usd_per_ticket defaults to 0.0 (disabled cap)."""
    s = Settings()
    assert s.max_spend_usd_per_ticket == 0.0


# ---------------------------------------------------------------------------
# 2. Env-var alias resolution — exhaustive parametrized test
# ---------------------------------------------------------------------------

# (field_name, alias, env_value, expected_python_value)
# env_value is the string form of the value as it would appear in an env var.
# expected_python_value is what getattr(settings, field_name) should equal
# after monkeypatch.setenv(alias, env_value).
#
# For int/float fields we pass a numeric string and expect the parsed number.
# For bool fields we pass "1" and expect True.
# For str/Path/Literal fields we just use a distinctive string value.
# For str|None / Path|None fields we use a distinctive non-None string.

ALIAS_CASES: list[tuple[str, str, str, object]] = [
    # --- core / model ---
    ("openrouter_api_key", "OPENROUTER_API_KEY", "sk-test", "sk-test"),
    ("model", "MILL_MODEL", "test/model", "test/model"),
    ("explore_model", "MILL_EXPLORE_MODEL", "test/explore", "test/explore"),
    ("test_model", "MILL_TEST_MODEL", "test/tester", "test/tester"),
    ("refine_model", "MILL_REFINE_MODEL", "test/refine", "test/refine"),
    ("answer_model", "MILL_ANSWER_MODEL", "test/answer", "test/answer"),
    ("retrospect_model", "MILL_RETROSPECT_MODEL", "test/retro", "test/retro"),
    ("audit_model", "MILL_AUDIT_MODEL", "test/audit", "test/audit"),
    ("dedup_model", "MILL_DEDUP_MODEL", "test/dedup", "test/dedup"),
    ("triage_model", "MILL_TRIAGE_MODEL", "test/triage", "test/triage"),
    # --- request limits ---
    ("coordinator_request_limit", "MILL_COORDINATOR_REQUEST_LIMIT", "42", 42),
    ("test_request_limit", "MILL_TEST_REQUEST_LIMIT", "15", 15),
    ("max_fix_iterations", "MILL_MAX_FIX_ITERATIONS", "6", 6),
    ("model_request_timeout", "MILL_MODEL_REQUEST_TIMEOUT", "123.5", 123.5),
    ("max_concurrency", "MILL_MAX_CONCURRENCY", "2", 2),
    # --- retry / backoff ---
    ("transient_retries", "MILL_TRANSIENT_RETRIES", "7", 7),
    ("transient_backoff_base", "MILL_TRANSIENT_BACKOFF_BASE", "5.0", 5.0),
    ("transient_backoff_cap", "MILL_TRANSIENT_BACKOFF_CAP", "60.0", 60.0),
    ("rate_limit_backoff_base", "MILL_RATE_LIMIT_BACKOFF_BASE", "10.0", 10.0),
    ("rate_limit_backoff_cap", "MILL_RATE_LIMIT_BACKOFF_CAP", "300.0", 300.0),
    ("rate_limit_fallback_retries", "MILL_RATE_LIMIT_FALLBACK_RETRIES", "5", 5),
    ("rate_limit_fallback_model", "MILL_RATE_LIMIT_FALLBACK_MODEL", "fb/model", "fb/model"),
    ("explore_request_limit", "MILL_EXPLORE_REQUEST_LIMIT", "99", 99),
    ("dedup_request_limit", "MILL_DEDUP_REQUEST_LIMIT", "3", 3),
    # --- memory / reference files ---
    ("max_memory_chars", "MILL_MAX_MEMORY_CHARS", "16000", 16000),
    ("reference_files_max_count", "MILL_REFERENCE_FILES_MAX_COUNT", "10", 10),
    ("reference_files_max_total_lines", "MILL_REFERENCE_FILES_MAX_TOTAL_LINES", "5000", 5000),
    ("dedup_lookback_days", "MILL_DEDUP_LOOKBACK_DAYS", "14", 14),
    ("dedup_lookback_commits", "MILL_DEDUP_LOOKBACK_COMMITS", "30", 30),
    # --- paths ---
    ("data_dir", "MILL_DATA_DIR", "/custom/data", Path("/custom/data")),
    # --- API ---
    ("api_host", "MILL_API_HOST", "0.0.0.0", "0.0.0.0"),
    ("api_port", "MILL_API_PORT", "9090", 9090),
    ("api_url", "MILL_API_URL", "http://example.com:9999", "http://example.com:9999"),
    # --- forge ---
    ("forge_kind", "FORGE_KIND", "none", "none"),
    ("forge_remote_url", "FORGE_REMOTE_URL", "https://git.example.com/repo", "https://git.example.com/repo"),
    ("forge_token", "FORGE_TOKEN", "glpat-xxxx", "glpat-xxxx"),
    ("forge_target_branch", "FORGE_TARGET_BRANCH", "develop", "develop"),
    ("forge_auth", "FORGE_AUTH", "token", "token"),
    ("github_app_id", "GITHUB_APP_ID", "123456", "123456"),
    ("github_app_private_key", "GITHUB_APP_PRIVATE_KEY", "pk-----", "pk-----"),
    ("github_app_private_key_path", "GITHUB_APP_PRIVATE_KEY_PATH", "/keys/app.pem", "/keys/app.pem"),
    ("github_api_url", "MILL_GITHUB_API_URL", "https://github.myco.com", "https://github.myco.com"),
    ("gitlab_api_url", "MILL_GITLAB_API_URL", "https://gitlab.myco.com/api/v4", "https://gitlab.myco.com/api/v4"),
    # --- implement ---
    ("test_command", "MILL_TEST_COMMAND", "pytest -x", "pytest -x"),
    ("branch_prefix", "MILL_BRANCH_PREFIX", "auto/", "auto/"),
    ("command_timeout", "MILL_COMMAND_TIMEOUT", "600", 600),
    ("max_stuck_cycles", "MILL_MAX_STUCK_CYCLES", "5", 5),
    ("max_spend_usd_per_ticket", "MILL_MAX_SPEND_USD_PER_TICKET", "1.5", 1.5),
    # --- sandbox ---
    ("sandbox_image", "MILL_SANDBOX_IMAGE", "python:3.12", "python:3.12"),
    ("sandbox_memory", "MILL_SANDBOX_MEMORY", "4g", "4g"),
    ("sandbox_pids_limit", "MILL_SANDBOX_PIDS_LIMIT", "256", 256),
    ("sandbox_readonly", "MILL_SANDBOX_READONLY", "0", False),
    ("data_volume", "MILL_DATA_VOLUME", "custom_volume", "custom_volume"),
    ("sandbox_data_mount", "MILL_SANDBOX_DATA_MOUNT", "/host/data", "/host/data"),
    # --- web ---
    ("web_search", "MILL_WEB_SEARCH", "0", False),
    ("web_research_model", "MILL_WEB_RESEARCH_MODEL", "web/model", "web/model"),
    ("web_research_request_limit", "MILL_WEB_RESEARCH_REQUEST_LIMIT", "4", 4),
    ("fetch_image", "MILL_FETCH_IMAGE", "curlimages/curl:latest", "curlimages/curl:latest"),
    ("web_fetch_max_bytes", "MILL_WEB_FETCH_MAX_BYTES", "500000", 500000),
    ("web_fetch_timeout", "MILL_WEB_FETCH_TIMEOUT", "15", 15),
    ("skills_dir", "MILL_SKILLS_DIR", "/skills", Path("/skills")),
    # --- approval ---
    ("require_approval", "MILL_REQUIRE_APPROVAL", "false", False),
    ("auto_approve_enabled", "MILL_AUTO_APPROVE_ENABLED", "1", True),
    ("auto_approve_model", "MILL_AUTO_APPROVE_MODEL", "aa/model", "aa/model"),
    # --- review ---
    ("review_enabled", "MILL_REVIEW_ENABLED", "true", True),
    ("auto_merge_enabled", "MILL_AUTO_MERGE_ENABLED", "1", True),
    ("refine_triage_enabled", "MILL_REFINE_TRIAGE_ENABLED", "0", False),
    ("spec_review_enabled", "MILL_SPEC_REVIEW_ENABLED", "true", True),
    ("review_model", "MILL_REVIEW_MODEL", "review/model", "review/model"),
    ("review_max_rounds", "MILL_REVIEW_MAX_ROUNDS", "1", 1),
    ("doc_model", "MILL_DOC_MODEL", "doc/model", "doc/model"),
    # --- retrospect ---
    ("retrospect_spawn_drafts", "MILL_RETROSPECT_SPAWN_DRAFTS", "0", False),
    ("retrospect_deep_analysis_frequency", "MILL_RETROSPECT_DEEP_ANALYSIS_FREQUENCY", "3", 3),
    ("trace_inspector_model", "MILL_TRACE_INSPECTOR_MODEL", "ti/model", "ti/model"),
    ("trace_inspector_memory_path", "MILL_TRACE_INSPECTOR_MEMORY_PATH", "/mem/ti.md", Path("/mem/ti.md")),
    ("retrospect_memory_path", "MILL_RETROSPECT_MEMORY_PATH", "/mem/retro.md", Path("/mem/retro.md")),
    ("merge_poll_seconds", "MILL_MERGE_POLL_SECONDS", "60", 60),
    ("prune_clone_on_close", "MILL_PRUNE_CLONE_ON_CLOSE", "false", False),
    ("max_archived_tickets", "MILL_MAX_ARCHIVED_TICKETS", "50", 50),
    # --- rebase / CI fix ---
    ("rebase_max_attempts", "MILL_REBASE_MAX_ATTEMPTS", "2", 2),
    ("ci_fix_max_attempts", "MILL_CI_FIX_MAX_ATTEMPTS", "4", 4),
    # --- CI monitor ---
    ("ci_monitor_periodic", "MILL_CI_MONITOR_PERIODIC", "1", True),
    ("ci_monitor_interval_seconds", "MILL_CI_MONITOR_INTERVAL_SECONDS", "3600", 3600),
    ("ci_log_max_bytes", "MILL_CI_LOG_MAX_BYTES", "32768", 32768),
    # --- audit ---
    ("audit_periodic", "MILL_AUDIT_PERIODIC", "true", True),
    ("audit_interval_seconds", "MILL_AUDIT_INTERVAL_SECONDS", "7200", 7200),
    ("audit_memory_path", "MILL_AUDIT_MEMORY_PATH", "/mem/audit.md", Path("/mem/audit.md")),
    # --- trace health ---
    ("trace_health_periodic", "MILL_TRACE_HEALTH_PERIODIC", "1", True),
    ("trace_health_interval_seconds", "MILL_TRACE_HEALTH_INTERVAL_SECONDS", "3600", 3600),
    # --- test gap ---
    ("test_gap_model", "MILL_TEST_GAP_MODEL", "tg/model", "tg/model"),
    ("test_gap_periodic", "MILL_TEST_GAP_PERIODIC", "1", True),
    ("test_gap_interval_seconds", "MILL_TEST_GAP_INTERVAL_SECONDS", "1800", 1800),
    ("test_gap_memory_path", "MILL_TEST_GAP_MEMORY_PATH", "/mem/tg.md", Path("/mem/tg.md")),
    # --- agent check ---
    ("agent_check_model", "MILL_AGENT_CHECK_MODEL", "ac/model", "ac/model"),
    ("agent_check_memory_path", "MILL_AGENT_CHECK_MEMORY_PATH", "/mem/ac.md", Path("/mem/ac.md")),
    ("agent_check_periodic", "MILL_AGENT_CHECK_PERIODIC", "1", True),
    ("agent_check_interval_seconds", "MILL_AGENT_CHECK_INTERVAL_SECONDS", "7200", 7200),
    # --- health ---
    ("health_model", "MILL_HEALTH_MODEL", "h/model", "h/model"),
    ("health_periodic", "MILL_HEALTH_PERIODIC", "true", True),
    ("health_interval_seconds", "MILL_HEALTH_INTERVAL_SECONDS", "43200", 43200),
    ("health_memory_path", "MILL_HEALTH_MEMORY_PATH", "/mem/health.md", Path("/mem/health.md")),
    # --- survey ---
    ("survey_model", "MILL_SURVEY_MODEL", "s/model", "s/model"),
    ("survey_memory_path", "MILL_SURVEY_MEMORY_PATH", "/mem/survey.md", Path("/mem/survey.md")),
    ("survey_periodic", "MILL_SURVEY_PERIODIC", "0", False),
    ("survey_interval_seconds", "MILL_SURVEY_INTERVAL_SECONDS", "3600", 3600),
    # --- action-agent memory paths ---
    ("implement_memory_path", "MILL_IMPLEMENT_MEMORY_PATH", "/mem/imp.md", Path("/mem/imp.md")),
    ("refine_memory_path", "MILL_REFINE_MEMORY_PATH", "/mem/ref.md", Path("/mem/ref.md")),
    ("ci_fix_memory_path", "MILL_CI_FIX_MEMORY_PATH", "/mem/cifix.md", Path("/mem/cifix.md")),
    ("rebase_memory_path", "MILL_REBASE_MEMORY_PATH", "/mem/rebase.md", Path("/mem/rebase.md")),
    # --- tracing ---
    ("langfuse_base_url", "LANGFUSE_BASE_URL", "https://lf.example.com", "https://lf.example.com"),
    ("langfuse_public_key", "LANGFUSE_PUBLIC_KEY", "pk-lf-test", "pk-lf-test"),
    ("langfuse_secret_key", "LANGFUSE_SECRET_KEY", "sk-lf-test", "sk-lf-test"),
    ("langfuse_project_id", "LANGFUSE_PROJECT_ID", "proj-123", "proj-123"),
    # --- notifications ---
    ("ntfy_url", "NTFY_URL", "https://ntfy.example.com", "https://ntfy.example.com"),
    ("ntfy_token", "NTFY_TOKEN", "tk-test", "tk-test"),
]


@pytest.mark.parametrize("field_name,alias,env_value,expected", ALIAS_CASES)
def test_alias_resolution(
    monkeypatch, field_name: str, alias: str, env_value: str, expected: object
):
    """Every Field(alias=...) resolves via its env var."""
    # Ensure a clean slate — the _no_dotenv autouse fixture already
    # clears the real vars, but make absolutely sure this alias is
    # unset before we set it ourselves.
    monkeypatch.delenv(alias, raising=False)
    monkeypatch.setenv(alias, env_value)
    s = Settings()
    actual = getattr(s, field_name)
    assert actual == expected, (
        f"{field_name}: expected {expected!r} (type={type(expected).__name__}), "
        f"got {actual!r} (type={type(actual).__name__}) "
        f"from env {alias}={env_value!r}"
    )


# ---------------------------------------------------------------------------
# 3. Type coercion
# ---------------------------------------------------------------------------

class TestTypeCoercion:
    """Pydantic Settings coerces string env vars to the field type."""

    def test_int_coercion(self, monkeypatch):
        monkeypatch.setenv("MILL_MAX_CONCURRENCY", "8")
        s = Settings()
        assert s.max_concurrency == 8
        assert isinstance(s.max_concurrency, int)

    def test_float_coercion(self, monkeypatch):
        monkeypatch.setenv("MILL_MODEL_REQUEST_TIMEOUT", "2.5")
        s = Settings()
        assert s.model_request_timeout == 2.5
        assert isinstance(s.model_request_timeout, float)

    @pytest.mark.parametrize("env_val,expected", [
        ("true", True),
        ("false", False),
        ("1", True),
        ("0", False),
        ("True", True),
        ("False", False),
    ])
    def test_bool_coercion(self, monkeypatch, env_val, expected):
        monkeypatch.setenv("MILL_REVIEW_ENABLED", env_val)
        s = Settings()
        assert s.review_enabled is expected

    def test_path_coercion(self, monkeypatch):
        monkeypatch.setenv("MILL_DATA_DIR", "/tmp/mill-test")
        s = Settings()
        assert s.data_dir == Path("/tmp/mill-test")
        assert isinstance(s.data_dir, Path)

    def test_literal_coercion(self, monkeypatch):
        monkeypatch.setenv("FORGE_KIND", "none")
        s = Settings()
        assert s.forge_kind == "none"

    def test_optional_str_from_env(self, monkeypatch):
        """str | None field populated from env is a plain str, not wrapped."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-abc")
        s = Settings()
        assert s.openrouter_api_key == "sk-abc"
        assert isinstance(s.openrouter_api_key, str)

    def test_optional_path_from_env(self, monkeypatch):
        """Path | None field populated from env."""
        monkeypatch.setenv("MILL_RETROSPECT_MEMORY_PATH", "/tmp/retro.md")
        s = Settings()
        assert s.retrospect_memory_path == Path("/tmp/retro.md")
        assert isinstance(s.retrospect_memory_path, Path)


# ---------------------------------------------------------------------------
# 4. Computed @property methods
# ---------------------------------------------------------------------------

class TestComputedProperties:
    """All @property methods on Settings."""

    # -- data-directory derivations --

    def test_db_path(self, tmp_path):
        s = Settings(MILL_DATA_DIR=str(tmp_path))
        assert s.db_path == tmp_path / "mill.db"

    def test_workspaces_dir(self, tmp_path):
        s = Settings(MILL_DATA_DIR=str(tmp_path))
        assert s.workspaces_dir == tmp_path / "workspaces"

    def test_db_url(self, tmp_path):
        s = Settings(MILL_DATA_DIR=str(tmp_path))
        assert s.db_url == f"sqlite:///{tmp_path / 'mill.db'}"

    # -- tracing_enabled --

    def test_tracing_enabled_true(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_BASE_URL", "https://lf.example.com")
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
        # Populate Secrets so get_secrets() returns matching values
        from robotsix_mill.config import Secrets, _reset_secrets
        import robotsix_mill.config as _cfg
        _reset_secrets()
        _cfg._secrets = Secrets(
            langfuse_base_url="https://lf.example.com",
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
        )
        s = Settings()
        assert s.tracing_enabled is True

    def test_tracing_enabled_false_missing_url(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
        # no LANGFUSE_BASE_URL
        from robotsix_mill.config import Secrets, _reset_secrets
        import robotsix_mill.config as _cfg
        _reset_secrets()
        _cfg._secrets = Secrets(
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
        )
        s = Settings()
        assert s.tracing_enabled is False

    def test_tracing_enabled_false_missing_public_key(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_BASE_URL", "https://lf.example.com")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
        from robotsix_mill.config import Secrets, _reset_secrets
        import robotsix_mill.config as _cfg
        _reset_secrets()
        _cfg._secrets = Secrets(
            langfuse_base_url="https://lf.example.com",
            langfuse_secret_key="sk-test",
        )
        s = Settings()
        assert s.tracing_enabled is False

    def test_tracing_enabled_false_empty_string(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_BASE_URL", "")
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
        from robotsix_mill.config import Secrets, _reset_secrets
        import robotsix_mill.config as _cfg
        _reset_secrets()
        _cfg._secrets = Secrets(
            langfuse_base_url="",
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
        )
        s = Settings()
        assert s.tracing_enabled is False

    # -- ci_monitor_memory_path --

    def test_ci_monitor_memory_path(self, tmp_path):
        s = Settings(MILL_DATA_DIR=str(tmp_path))
        assert s.ci_monitor_memory_path == tmp_path / "ci_monitor_state.json"

    # -- memory_file properties (14 total) --

    MEMORY_FILE_PROPERTIES = [
        ("retrospect_memory_file", "retrospect_memory_path", "retrospect_memory.md"),
        ("trace_inspector_memory_file", "trace_inspector_memory_path", "trace_inspector_memory.md"),
        ("audit_memory_file", "audit_memory_path", "audit_memory.md"),
        ("agent_check_memory_file", "agent_check_memory_path", "agent_check_memory.md"),
        ("health_memory_file", "health_memory_path", "health_memory.md"),
        ("test_gap_memory_file", "test_gap_memory_path", "test_gap_memory.md"),
        ("survey_memory_file", "survey_memory_path", "survey_memory.md"),
        ("implement_memory_file", "implement_memory_path", "implement_memory.md"),
        ("refine_memory_file", "refine_memory_path", "refine_memory.md"),
        ("ci_fix_memory_file", "ci_fix_memory_path", "ci_fix_memory.md"),
        ("rebase_memory_file", "rebase_memory_path", "rebase_memory.md"),
    ]

    @pytest.mark.parametrize(
        "prop_name,override_field,fallback_filename", MEMORY_FILE_PROPERTIES
    )
    def test_memory_file_fallback(self, tmp_path, prop_name, override_field, fallback_filename):
        """When the override path is None, the property derives from data_dir."""
        s = Settings(MILL_DATA_DIR=str(tmp_path))
        assert getattr(s, prop_name) == tmp_path / fallback_filename

    # Map Python field name → env-var alias for the memory_path override fields
    MEMORY_PATH_ALIASES: dict[str, str] = {
        "retrospect_memory_path": "MILL_RETROSPECT_MEMORY_PATH",
        "trace_inspector_memory_path": "MILL_TRACE_INSPECTOR_MEMORY_PATH",
        "audit_memory_path": "MILL_AUDIT_MEMORY_PATH",
        "agent_check_memory_path": "MILL_AGENT_CHECK_MEMORY_PATH",
        "health_memory_path": "MILL_HEALTH_MEMORY_PATH",
        "test_gap_memory_path": "MILL_TEST_GAP_MEMORY_PATH",
        "survey_memory_path": "MILL_SURVEY_MEMORY_PATH",
        "implement_memory_path": "MILL_IMPLEMENT_MEMORY_PATH",
        "refine_memory_path": "MILL_REFINE_MEMORY_PATH",
        "ci_fix_memory_path": "MILL_CI_FIX_MEMORY_PATH",
        "rebase_memory_path": "MILL_REBASE_MEMORY_PATH",
    }

    @pytest.mark.parametrize(
        "prop_name,override_field,fallback_filename", MEMORY_FILE_PROPERTIES
    )
    def test_memory_file_override(self, monkeypatch, tmp_path, prop_name, override_field, fallback_filename):
        """When the override path is set, it wins over the derived path."""
        custom = Path("/custom/memory.md")
        alias = self.MEMORY_PATH_ALIASES[override_field]
        monkeypatch.setenv(alias, str(custom))
        s = Settings(MILL_DATA_DIR=str(tmp_path))
        assert getattr(s, prop_name) == custom

    def test_memory_file_edge_case_empty_override(self, monkeypatch, tmp_path):
        """retrospect_memory_path='' is falsy but not None — property
        should still return the explicit value (empty Path), not the
        fallback."""
        monkeypatch.setenv("MILL_RETROSPECT_MEMORY_PATH", "")
        s = Settings(MILL_DATA_DIR=str(tmp_path))
        # An empty string is not None, so the property returns it as-is
        # (Path("") which equals Path(".")).
        assert s.retrospect_memory_file == Path("")


# ---------------------------------------------------------------------------
# 5. extra="ignore" behaviour
# ---------------------------------------------------------------------------

def test_extra_ignore(monkeypatch):
    """Unknown env vars don't cause ValidationError."""
    monkeypatch.setenv("UNKNOWN_SETTING", "foo")
    monkeypatch.setenv("ANOTHER_BOGUS_VAR", "bar")
    s = Settings()
    assert s is not None  # constructed successfully


# ---------------------------------------------------------------------------
# 6. env_file loading
# ---------------------------------------------------------------------------

def test_env_file_loading(tmp_path, monkeypatch):
    """A temp .env file is read and populates Settings fields."""
    env_file = tmp_path / ".env"
    env_file.write_text("MILL_MODEL=test-model\nMILL_MAX_CONCURRENCY=3\n")

    # Patch model_config to point at our temp file.  We restore it in
    # teardown by saving the original; the _no_dotenv fixture already
    # sets it to None, so we're effectively overriding that here.
    monkeypatch.setitem(Settings.model_config, "env_file", str(env_file))

    s = Settings()
    assert s.model == "test-model"
    assert s.max_concurrency == 3


def test_env_file_loading_via_kwarg(tmp_path):
    """Use the _env_file constructor kwarg (pydantic-settings ≥2.0) to
    point at a temp file without mutating model_config."""
    env_file = tmp_path / "test.env"
    env_file.write_text("MILL_TEST_MODEL=custom-test-model\n")

    s = Settings(_env_file=str(env_file))
    assert s.test_model == "custom-test-model"


# ---------------------------------------------------------------------------
# 7. load_settings() smoke test
# ---------------------------------------------------------------------------

def test_load_settings_returns_settings_instance():
    """Trivial: the module-level factory returns a Settings object."""
    s = load_settings()
    assert isinstance(s, Settings)


# ---------------------------------------------------------------------------
# 8. YAML config loading
# ---------------------------------------------------------------------------

class TestYamlLoading:
    """Tests for ``config_loader.load_yaml_config`` and
    ``config_loader.load_secrets_yaml``."""

    def test_load_yaml_config_returns_nested_dict(self):
        """``load_yaml_config()`` returns a nested dict matching the
        defaults YAML structure when no overlays are present."""
        from robotsix_mill.config_loader import load_yaml_config

        config = load_yaml_config()
        assert isinstance(config, dict)
        assert "core" in config
        assert "models" in config["core"]
        assert config["core"]["models"]["coordinator"] == "deepseek/deepseek-v4-pro"
        assert "service" in config
        assert config["service"]["data_dir"] == ".mill-data"

    def test_load_yaml_config_deep_merges_local_overlay(self, tmp_path, monkeypatch):
        """``load_yaml_config()`` deep-merges a local overlay YAML over
        defaults."""
        from robotsix_mill.config_loader import load_yaml_config

        # Write a temporary local overlay
        local_dir = tmp_path / "config"
        local_dir.mkdir()
        defaults = local_dir / "mill.defaults.yaml"
        # Copy the real defaults so the loader finds them
        import shutil
        shutil.copy("config/mill.defaults.yaml", defaults)
        local = local_dir / "mill.local.yaml"
        local.write_text("core:\n  limits:\n    max_concurrency: 99\n")

        # Patch the module-level path to point at our temp dir
        import robotsix_mill.config_loader as cl
        monkeypatch.setattr(cl, "_DEFAULTS_FILE", defaults)
        monkeypatch.setattr(cl, "_LOCAL_FILE", local)

        config = load_yaml_config()
        assert config["core"]["limits"]["max_concurrency"] == 99
        # Other values unchanged from defaults
        assert config["core"]["models"]["coordinator"] == "deepseek/deepseek-v4-pro"

    def test_load_yaml_config_missing_defaults_raises(self, monkeypatch):
        """``load_yaml_config()`` raises ``ConfigError`` when
        ``mill.defaults.yaml`` is missing."""
        from robotsix_mill.config_loader import ConfigError, load_yaml_config
        import robotsix_mill.config_loader as cl

        monkeypatch.setattr(cl, "_DEFAULTS_FILE", cl.Path("/nonexistent/defaults.yaml"))
        with pytest.raises(ConfigError, match="Required config file not found"):
            load_yaml_config()

    def test_load_secrets_yaml_returns_populated_dict(self, tmp_path):
        """``load_secrets_yaml()`` returns a populated dict when a valid
        secrets file exists."""
        from robotsix_mill.config_loader import load_secrets_yaml

        secrets_file = tmp_path / "secrets.yaml"
        secrets_file.write_text(
            "openrouter_api_key: sk-test\n"
            "forge_token: ghp_test\n"
            "ntfy_url: https://ntfy.example.com\n"
        )
        result = load_secrets_yaml(str(secrets_file))
        assert result == {
            "openrouter_api_key": "sk-test",
            "forge_token": "ghp_test",
            "ntfy_url": "https://ntfy.example.com",
        }

    def test_load_secrets_yaml_missing_returns_empty(self):
        """``load_secrets_yaml()`` returns an empty dict when the secrets
        file is missing (not an error)."""
        from robotsix_mill.config_loader import load_secrets_yaml

        result = load_secrets_yaml("/nonexistent/secrets.yaml")
        assert result == {}

    def test_load_secrets_yaml_malformed_raises(self, tmp_path):
        """``load_secrets_yaml()`` raises ``ConfigError`` on malformed YAML."""
        from robotsix_mill.config_loader import ConfigError, load_secrets_yaml

        secrets_file = tmp_path / "bad.yaml"
        secrets_file.write_text("{ invalid: yaml: : }")
        with pytest.raises(ConfigError, match="YAML parse error"):
            load_secrets_yaml(str(secrets_file))


# ---------------------------------------------------------------------------
# 9. Secrets model
# ---------------------------------------------------------------------------

class TestSecretsModel:
    """Tests for the ``Secrets`` class."""

    def test_constructed_from_yaml_populates_fields(self, tmp_path):
        """``Secrets()`` constructed from a temp YAML file populates all
        fields correctly."""
        from robotsix_mill.config import Secrets

        secrets_file = tmp_path / "secrets.yaml"
        secrets_file.write_text(
            "openrouter_api_key: sk-or-123\n"
            "forge_token: ghp_abc\n"
            "github_app_id: \"456\"\n"
            "github_app_private_key: pk-data\n"
            "langfuse_public_key: pk-lf\n"
            "langfuse_secret_key: sk-lf\n"
            "langfuse_base_url: https://lf.example.com\n"
            "langfuse_project_id: proj-1\n"
            "ntfy_url: https://ntfy.example.com\n"
            "ntfy_token: tk-ntfy\n"
        )
        s = Secrets(_secrets_file=str(secrets_file))
        assert s.openrouter_api_key == "sk-or-123"
        assert s.forge_token == "ghp_abc"
        assert s.github_app_id == "456"
        assert s.github_app_private_key == "pk-data"
        assert s.langfuse_public_key == "pk-lf"
        assert s.langfuse_secret_key == "sk-lf"
        assert s.langfuse_base_url == "https://lf.example.com"
        assert s.langfuse_project_id == "proj-1"
        assert s.ntfy_url == "https://ntfy.example.com"
        assert s.ntfy_token == "tk-ntfy"

    def test_repr_redacts_all_values(self):
        """``repr(secrets)`` redacts all values."""
        from robotsix_mill.config import Secrets

        s = Secrets(openrouter_api_key="sk-secret", forge_token="ghp_secret")
        r = repr(s)
        assert "sk-secret" not in r
        assert "ghp_secret" not in r
        assert "***" in r
        assert r.startswith("Secrets(")

    def test_model_dump_redacts_by_default(self):
        """``secrets.model_dump()`` redacts all values by default."""
        from robotsix_mill.config import Secrets

        s = Secrets(openrouter_api_key="sk-secret")
        d = s.model_dump()
        assert d["openrouter_api_key"] == "***"
        assert "sk-secret" not in str(d)

    def test_model_dump_unredacted_returns_actual_values(self):
        """``secrets.model_dump(redact=False)`` returns actual values."""
        from robotsix_mill.config import Secrets

        s = Secrets(openrouter_api_key="sk-secret", forge_token="ghp_secret")
        d = s.model_dump(redact=False)
        assert d["openrouter_api_key"] == "sk-secret"
        assert d["forge_token"] == "ghp_secret"

    def test_attribute_access_logs_at_debug(self, caplog):
        """Attribute access logs at DEBUG level with caller module."""
        import logging
        from robotsix_mill.config import Secrets

        s = Secrets(openrouter_api_key="sk-test")
        with caplog.at_level(logging.DEBUG, logger="robotsix_mill.config"):
            _ = s.openrouter_api_key
        assert "Secrets.openrouter_api_key accessed by" in caplog.text

    def test_explicit_kwargs_override_yaml(self, tmp_path):
        """Explicit constructor kwargs override YAML file values."""
        from robotsix_mill.config import Secrets

        secrets_file = tmp_path / "secrets.yaml"
        secrets_file.write_text("openrouter_api_key: sk-from-yaml\n")
        s = Secrets(
            _secrets_file=str(secrets_file),
            openrouter_api_key="sk-from-kwarg",
        )
        assert s.openrouter_api_key == "sk-from-kwarg"


# ---------------------------------------------------------------------------
# 10. Repos config — YAML loader, models, registry
# ---------------------------------------------------------------------------


class TestLoadReposYaml:
    """Tests for ``config_loader.load_repos_yaml``."""

    def test_missing_file_returns_empty(self):
        """Missing file → returns empty dict (not an error)."""
        from robotsix_mill.config_loader import load_repos_yaml

        result = load_repos_yaml("/nonexistent/repos.yaml")
        assert result == {}

    def test_malformed_yaml_raises(self, tmp_path):
        """Malformed YAML → raises ``ConfigError`` with file path in message."""
        from robotsix_mill.config_loader import ConfigError, load_repos_yaml

        bad_file = tmp_path / "bad.yaml"
        bad_file.write_text("{ invalid: yaml: : }")
        with pytest.raises(ConfigError, match="YAML parse error"):
            load_repos_yaml(str(bad_file))

    def test_valid_file_returns_dict(self, tmp_path):
        """Valid YAML → returns dict keyed by repo ID with nested
        ``board_id`` and ``langfuse`` sub-dict."""
        from robotsix_mill.config_loader import load_repos_yaml

        repos_file = tmp_path / "repos.yaml"
        repos_file.write_text(
            "repos:\n"
            "  my-repo:\n"
            "    board_id: my-board\n"
            "    langfuse:\n"
            "      project_name: my-project\n"
            "      public_key: pk-abc\n"
            "      secret_key: sk-xyz\n"
        )
        result = load_repos_yaml(str(repos_file))
        assert result == {
            "my-repo": {
                "board_id": "my-board",
                "langfuse": {
                    "project_name": "my-project",
                    "public_key": "pk-abc",
                    "secret_key": "sk-xyz",
                },
            },
        }

    def test_env_var_overrides_path(self, tmp_path, monkeypatch):
        """``MILL_REPOS_FILE`` env var overrides the default path."""
        from robotsix_mill.config_loader import load_repos_yaml

        repos_file = tmp_path / "custom.yaml"
        repos_file.write_text(
            "repos:\n"
            "  env-repo:\n"
            "    board_id: env-board\n"
            "    langfuse:\n"
            "      project_name: env-project\n"
            "      public_key: pk-env\n"
            "      secret_key: sk-env\n"
        )
        monkeypatch.setenv("MILL_REPOS_FILE", str(repos_file))
        result = load_repos_yaml()
        assert "env-repo" in result
        assert result["env-repo"]["board_id"] == "env-board"

    def test_env_var_empty_string_returns_empty(self, monkeypatch):
        """``MILL_REPOS_FILE=""`` returns an empty dict (test-suite mode)."""
        from robotsix_mill.config_loader import load_repos_yaml

        monkeypatch.setenv("MILL_REPOS_FILE", "")
        result = load_repos_yaml()
        assert result == {}

    def test_explicit_arg_overrides_env_var(self, tmp_path, monkeypatch):
        """Explicit ``file_path`` arg takes precedence over env var."""
        from robotsix_mill.config_loader import load_repos_yaml

        explicit_file = tmp_path / "explicit.yaml"
        explicit_file.write_text(
            "repos:\n"
            "  explicit-repo:\n"
            "    board_id: explicit-board\n"
            "    langfuse:\n"
            "      project_name: explicit-project\n"
            "      public_key: pk-exp\n"
            "      secret_key: sk-exp\n"
        )
        env_file = tmp_path / "env.yaml"
        env_file.write_text("repos:\n  env-repo:\n    board_id: wrong\n    langfuse:\n      project_name: w\n      public_key: w\n      secret_key: w\n")
        monkeypatch.setenv("MILL_REPOS_FILE", str(env_file))
        result = load_repos_yaml(str(explicit_file))
        assert "explicit-repo" in result
        assert "env-repo" not in result


class TestRepoConfig:
    """Tests for the ``RepoConfig`` model."""

    def test_valid_repo_config(self):
        """A valid ``RepoConfig`` is constructable with all fields."""
        from robotsix_mill.config import RepoConfig

        rc = RepoConfig(
            repo_id="my-repo",
            board_id="my-board",
            langfuse_project_name="my-project",
            langfuse_public_key="pk-lf",
            langfuse_secret_key="sk-lf",
        )
        assert rc.repo_id == "my-repo"
        assert rc.board_id == "my-board"
        assert rc.langfuse_project_name == "my-project"
        assert rc.langfuse_public_key == "pk-lf"
        assert rc.langfuse_secret_key == "sk-lf"
        assert rc.langfuse_base_url == "https://cloud.langfuse.com"

    def test_langfuse_base_url_default(self):
        """Omitting ``langfuse_base_url`` defaults to cloud."""
        from robotsix_mill.config import RepoConfig

        rc = RepoConfig(
            repo_id="r",
            board_id="b",
            langfuse_project_name="p",
            langfuse_public_key="pk",
            langfuse_secret_key="sk",
        )
        assert rc.langfuse_base_url == "https://cloud.langfuse.com"

    def test_langfuse_base_url_custom(self):
        """``langfuse_base_url`` can be overridden."""
        from robotsix_mill.config import RepoConfig

        rc = RepoConfig(
            repo_id="r",
            board_id="b",
            langfuse_project_name="p",
            langfuse_public_key="pk",
            langfuse_secret_key="sk",
            langfuse_base_url="https://lf.example.com",
        )
        assert rc.langfuse_base_url == "https://lf.example.com"

    def test_empty_repo_id_raises(self):
        """Empty ``repo_id`` raises ``ValidationError``."""
        from pydantic import ValidationError
        from robotsix_mill.config import RepoConfig

        with pytest.raises(ValidationError, match="repo_id"):
            RepoConfig(
                repo_id="",
                board_id="b",
                langfuse_project_name="p",
                langfuse_public_key="pk",
                langfuse_secret_key="sk",
            )

    def test_empty_board_id_raises(self):
        """Empty ``board_id`` raises ``ValidationError``."""
        from pydantic import ValidationError
        from robotsix_mill.config import RepoConfig

        with pytest.raises(ValidationError, match="board_id"):
            RepoConfig(
                repo_id="r",
                board_id="",
                langfuse_project_name="p",
                langfuse_public_key="pk",
                langfuse_secret_key="sk",
            )


class TestReposRegistry:
    """Tests for the ``ReposRegistry`` model."""

    def test_empty_registry(self):
        """``ReposRegistry`` with ``repos={}`` is valid."""
        from robotsix_mill.config import ReposRegistry

        rr = ReposRegistry(repos={})
        assert rr.repos == {}

    def test_key_mismatch_raises(self):
        """If ``RepoConfig.repo_id`` != dict key, ``ValueError`` is raised."""
        from pydantic import ValidationError
        from robotsix_mill.config import RepoConfig, ReposRegistry

        rc = RepoConfig(
            repo_id="wrong-id",
            board_id="b",
            langfuse_project_name="p",
            langfuse_public_key="pk",
            langfuse_secret_key="sk",
        )
        with pytest.raises(ValidationError, match="does not match"):
            ReposRegistry(repos={"correct-id": rc})


class TestLoadReposConfig:
    """Tests for ``load_repos_config``, ``get_repos_config``,
    ``get_repo_config``, and ``_reset_repos_config``."""

    def test_empty_registry_from_missing_file(self):
        """``load_repos_config()`` with no file returns an empty registry."""
        from robotsix_mill.config import load_repos_config, ReposRegistry

        rr = load_repos_config("")
        assert isinstance(rr, ReposRegistry)
        assert rr.repos == {}

    def test_valid_yaml_produces_registry(self, tmp_path):
        """``load_repos_config()`` from valid YAML returns populated registry."""
        from robotsix_mill.config import load_repos_config, RepoConfig, ReposRegistry

        repos_file = tmp_path / "repos.yaml"
        repos_file.write_text(
            "repos:\n"
            "  repo-a:\n"
            "    board_id: board-a\n"
            "    langfuse:\n"
            "      project_name: proj-a\n"
            "      public_key: pk-a\n"
            "      secret_key: sk-a\n"
        )
        rr = load_repos_config(str(repos_file))
        assert isinstance(rr, ReposRegistry)
        assert "repo-a" in rr.repos
        rc = rr.repos["repo-a"]
        assert isinstance(rc, RepoConfig)
        assert rc.repo_id == "repo-a"
        assert rc.board_id == "board-a"
        assert rc.langfuse_project_name == "proj-a"
        assert rc.langfuse_public_key == "pk-a"
        assert rc.langfuse_secret_key == "sk-a"
        assert rc.langfuse_base_url == "https://cloud.langfuse.com"

    def test_langfuse_base_url_default_in_loaded_config(self, tmp_path):
        """Omitting ``langfuse.base_url`` in YAML defaults to cloud."""
        from robotsix_mill.config import load_repos_config

        repos_file = tmp_path / "repos.yaml"
        repos_file.write_text(
            "repos:\n"
            "  r:\n"
            "    board_id: b\n"
            "    langfuse:\n"
            "      project_name: p\n"
            "      public_key: pk\n"
            "      secret_key: sk\n"
        )
        rr = load_repos_config(str(repos_file))
        assert rr.repos["r"].langfuse_base_url == "https://cloud.langfuse.com"

    def test_get_repo_config_valid_id(self, tmp_path):
        """``get_repo_config()`` with a valid ID returns the correct config."""
        from robotsix_mill.config import (
            _reset_repos_config,
            get_repo_config,
            load_repos_config,
        )

        repos_file = tmp_path / "repos.yaml"
        repos_file.write_text(
            "repos:\n"
            "  my-repo:\n"
            "    board_id: my-board\n"
            "    langfuse:\n"
            "      project_name: my-proj\n"
            "      public_key: pk\n"
            "      secret_key: sk\n"
        )
        _reset_repos_config()
        import robotsix_mill.config as _cfg
        _cfg._repos_config = load_repos_config(str(repos_file))

        rc = get_repo_config("my-repo")
        assert rc.repo_id == "my-repo"
        assert rc.board_id == "my-board"
        assert rc.langfuse_project_name == "my-proj"

    def test_get_repo_config_unknown_id(self):
        """``get_repo_config()`` with unknown ID raises ``ConfigError``."""
        from robotsix_mill.config_loader import ConfigError
        from robotsix_mill.config import (
            _reset_repos_config,
            get_repo_config,
        )

        _reset_repos_config()
        import robotsix_mill.config as _cfg
        from robotsix_mill.config import RepoConfig, ReposRegistry

        _cfg._repos_config = ReposRegistry(
            repos={
                "known-a": RepoConfig(
                    repo_id="known-a",
                    board_id="b",
                    langfuse_project_name="p",
                    langfuse_public_key="pk",
                    langfuse_secret_key="sk",
                ),
            }
        )
        with pytest.raises(ConfigError, match="Unknown repo: 'unknown'"):
            get_repo_config("unknown")

    def test_get_repos_config_singleton(self, tmp_path):
        """``get_repos_config()`` returns the same object on repeated calls."""
        from robotsix_mill.config import _reset_repos_config, get_repos_config

        _reset_repos_config()
        rr1 = get_repos_config()
        rr2 = get_repos_config()
        assert rr1 is rr2
        assert id(rr1) == id(rr2)

    def test_reset_repos_config_clears_cache(self, tmp_path):
        """After ``_reset_repos_config()``, next call constructs fresh instance."""
        from robotsix_mill.config import (
            _reset_repos_config,
            get_repos_config,
            load_repos_config,
        )

        repos_file = tmp_path / "repos.yaml"
        repos_file.write_text(
            "repos:\n"
            "  r:\n"
            "    board_id: b\n"
            "    langfuse:\n"
            "      project_name: p\n"
            "      public_key: pk\n"
            "      secret_key: sk\n"
        )
        _reset_repos_config()
        import robotsix_mill.config as _cfg
        _cfg._repos_config = load_repos_config(str(repos_file))

        rr1 = get_repos_config()
        _reset_repos_config()
        rr2 = get_repos_config()
        assert rr1 is not rr2
        # After reset, fresh load from default location (missing) → empty
        assert rr2.repos == {}


# ---------------------------------------------------------------------------
# 11. Semantic validators
# ---------------------------------------------------------------------------

class TestValidationValid:
    """Valid Settings constructions that pass all validators."""

    def test_default_settings_passes(self):
        """Default ``Settings()`` passes all validators (smoke test)."""
        s = Settings()
        assert s.max_concurrency == 4

    def test_max_concurrency_boundary_passes(self):
        """``max_concurrency=1`` (lower bound) passes."""
        s = Settings(MILL_MAX_CONCURRENCY=1)
        assert s.max_concurrency == 1

    def test_forge_auth_app_with_github_app_id_passes(self):
        """``forge_auth=app`` with ``github_app_id`` passes."""
        s = Settings(FORGE_AUTH="app", GITHUB_APP_ID="123")
        assert s.forge_auth == "app"

    def test_forge_kind_github_with_remote_url_passes(self):
        """``forge_kind=github`` with ``forge_remote_url`` passes."""
        s = Settings(
            FORGE_KIND="github",
            FORGE_REMOTE_URL="https://github.com/o/r.git",
        )
        assert s.forge_kind == "github"


class TestValidationInvalid:
    """Invalid Settings constructions that must raise ``ValidationError``."""

    def test_max_concurrency_zero_raises(self):
        """``max_concurrency=0`` raises ValidationError."""
        with pytest.raises(ValidationError, match="max_concurrency must be ≥ 1"):
            Settings(MILL_MAX_CONCURRENCY=0)

    def test_model_request_timeout_zero_raises(self):
        """``model_request_timeout=0`` raises ValidationError."""
        with pytest.raises(ValidationError, match="model_request_timeout must be > 0"):
            Settings(MILL_MODEL_REQUEST_TIMEOUT=0)

    def test_api_url_invalid_format_raises(self):
        """``api_url`` not starting with http(s) raises ValidationError."""
        with pytest.raises(ValidationError, match="api_url must be an HTTP"):
            Settings(MILL_API_URL="not-a-url")

    def test_github_api_url_invalid_format_raises(self):
        """``github_api_url`` not starting with http(s) raises."""
        with pytest.raises(ValidationError, match="github_api_url must be an HTTP"):
            Settings(MILL_GITHUB_API_URL="ftp://bad")

    def test_gitlab_api_url_invalid_format_raises(self):
        """``gitlab_api_url`` not starting with http(s) raises."""
        with pytest.raises(ValidationError, match="gitlab_api_url must be an HTTP"):
            Settings(MILL_GITLAB_API_URL="ftp://bad")

    def test_trace_health_interval_too_low_raises(self):
        """``trace_health_interval_seconds=60`` raises ValidationError."""
        with pytest.raises(
            ValidationError, match="trace_health_interval_seconds must be ≥ 3600"
        ):
            Settings(MILL_TRACE_HEALTH_INTERVAL_SECONDS=60)

    def test_forge_auth_app_without_credentials_raises(self):
        """``forge_auth=app`` without credentials raises."""
        with pytest.raises(ValidationError, match="FORGE_AUTH=app requires"):
            Settings(FORGE_AUTH="app")

    def test_forge_kind_github_without_remote_url_raises(self):
        """``forge_kind=github`` without ``forge_remote_url`` raises."""
        with pytest.raises(
            ValidationError, match="forge_kind=github requires forge_remote_url"
        ):
            Settings(FORGE_KIND="github")

    def test_fallback_model_without_retries_raises(self):
        """``rate_limit_fallback_model`` set but retries=0 raises."""
        with pytest.raises(
            ValidationError,
            match="rate_limit_fallback_retries must be ≥ 1",
        ):
            Settings(
                MILL_RATE_LIMIT_FALLBACK_MODEL="gpt-4o",
                MILL_RATE_LIMIT_FALLBACK_RETRIES=0,
            )

    def test_review_enabled_without_review_model_raises(self):
        """``review_enabled=True`` with empty ``review_model`` raises."""
        with pytest.raises(
            ValidationError, match="review_model must be non-empty"
        ):
            Settings(MILL_REVIEW_ENABLED="true", MILL_REVIEW_MODEL="")

    def test_explore_request_limit_zero_raises(self):
        """``explore_request_limit=0`` raises ValidationError."""
        with pytest.raises(
            ValidationError, match="explore_request_limit must be ≥ 1"
        ):
            Settings(MILL_EXPLORE_REQUEST_LIMIT=0)


# ---------------------------------------------------------------------------
# 11. Integration: load_settings / load_secrets factories
# ---------------------------------------------------------------------------

class TestFactories:
    """Integration tests for ``load_settings()`` and ``load_secrets()``."""

    def test_load_settings_returns_settings_with_yaml_defaults(
        self, tmp_path, monkeypatch
    ):
        """``load_settings()`` applies YAML values when no other source
        sets them."""
        import robotsix_mill.config_loader as cl

        # Write a temporary defaults file with a non-standard value
        local_dir = tmp_path / "config"
        local_dir.mkdir()
        defaults = local_dir / "mill.defaults.yaml"
        import shutil

        shutil.copy("config/mill.defaults.yaml", defaults)
        defaults.write_text(
            defaults.read_text().replace(
                "max_concurrency: 4", "max_concurrency: 42"
            )
        )
        monkeypatch.setattr(cl, "_DEFAULTS_FILE", defaults)
        monkeypatch.setattr(cl, "_LOCAL_FILE", cl.Path("/nonexistent/mill.local.yaml"))

        from robotsix_mill.config import load_settings

        s = load_settings()
        assert s.max_concurrency == 42

    def test_load_secrets_returns_secrets_object(self):
        """``load_secrets()`` returns a ``Secrets`` object."""
        from robotsix_mill.config import load_secrets, Secrets

        s = load_secrets()
        assert isinstance(s, Secrets)

    def test_load_settings_env_override_yaml(self, tmp_path, monkeypatch):
        """Env vars override YAML defaults in ``load_settings()``."""
        import robotsix_mill.config_loader as cl

        # Local overlay sets max_concurrency=42, but env var says 77
        local_dir = tmp_path / "config"
        local_dir.mkdir()
        defaults = local_dir / "mill.defaults.yaml"
        import shutil

        shutil.copy("config/mill.defaults.yaml", defaults)
        local = local_dir / "mill.local.yaml"
        local.write_text("core:\n  limits:\n    max_concurrency: 42\n")
        monkeypatch.setattr(cl, "_DEFAULTS_FILE", defaults)
        monkeypatch.setattr(cl, "_LOCAL_FILE", local)
        monkeypatch.setenv("MILL_MAX_CONCURRENCY", "77")

        from robotsix_mill.config import load_settings

        s = load_settings()
        assert s.max_concurrency == 77  # env var wins, not YAML's 42

    def test_settings_init_applies_yaml_fallback(
        self, tmp_path, monkeypatch
    ):
        """``Settings()`` applies YAML when field is at default;
        constructor kwargs override YAML."""
        import robotsix_mill.config_loader as cl

        local_dir = tmp_path / "config"
        local_dir.mkdir()
        defaults = local_dir / "mill.defaults.yaml"
        import shutil

        shutil.copy("config/mill.defaults.yaml", defaults)
        defaults.write_text(
            defaults.read_text().replace(
                "max_concurrency: 4", "max_concurrency: 42"
            )
        )
        monkeypatch.setattr(cl, "_DEFAULTS_FILE", defaults)
        monkeypatch.setattr(cl, "_LOCAL_FILE", cl.Path("/nonexistent/mill.local.yaml"))

        from robotsix_mill.config import Settings

        # No kwargs → YAML value is used (Field default is 4, YAML says 42)
        s1 = Settings(MILL_DATA_DIR=str(tmp_path))
        assert s1.max_concurrency == 42

        # Constructor kwarg overrides YAML
        s2 = Settings(MILL_DATA_DIR=str(tmp_path), MILL_MAX_CONCURRENCY="99")
        assert s2.max_concurrency == 99


class TestFlattenYamlConfig:
    """Unit tests for ``flatten_yaml_config``."""

    def test_flatten_basic_nesting(self):
        """Nested YAML dict is flattened to alias→value pairs."""
        from robotsix_mill.config_loader import flatten_yaml_config

        yaml_config = {
            "core": {
                "limits": {"max_concurrency": 8},
                "models": {"coordinator": "test/model"},
            },
            "service": {"data_dir": "/tmp/data"},
        }
        result = flatten_yaml_config(yaml_config)
        assert result["MILL_MAX_CONCURRENCY"] == 8
        assert result["MILL_MODEL"] == "test/model"
        assert result["MILL_DATA_DIR"] == "/tmp/data"

    def test_flatten_unknown_paths_ignored(self):
        """YAML paths without a mapping are silently ignored."""
        from robotsix_mill.config_loader import flatten_yaml_config

        yaml_config = {
            "core": {
                "unknown_section": {"some_key": "value"},
            },
        }
        result = flatten_yaml_config(yaml_config)
        assert "some_key" not in result
        assert result == {}

    def test_flatten_last_wins_for_duplicate_aliases(self):
        """When two YAML paths map to the same alias, the last one wins."""
        from robotsix_mill.config_loader import flatten_yaml_config

        yaml_config = {
            "core": {"models": {"web_research": "model-a"}},
            "web": {"research_model": "model-b"},
        }
        result = flatten_yaml_config(yaml_config)
        # Both core.models.web_research and web.research_model
        # map to MILL_WEB_RESEARCH_MODEL; web.research_model is
        # traversed second because 'web' > 'core' alphabetically
        assert result["MILL_WEB_RESEARCH_MODEL"] == "model-b"
