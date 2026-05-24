"""Unit tests for src/robotsix_mill/config.py — Settings defaults, env-var
aliases, type coercion, computed properties, and edge cases."""

from __future__ import annotations

from pathlib import Path

import pytest

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
    """MILL_DOC_REQUEST_LIMIT defaults to 4."""
    s = Settings()
    assert s.doc_request_limit == 4


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
    ("forge_kind", "FORGE_KIND", "gitlab", "gitlab"),
    ("forge_remote_url", "FORGE_REMOTE_URL", "https://git.example.com/repo", "https://git.example.com/repo"),
    ("forge_token", "FORGE_TOKEN", "glpat-xxxx", "glpat-xxxx"),
    ("forge_target_branch", "FORGE_TARGET_BRANCH", "develop", "develop"),
    ("forge_auth", "FORGE_AUTH", "app", "app"),
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
        monkeypatch.setenv("FORGE_KIND", "gitlab")
        s = Settings()
        assert s.forge_kind == "gitlab"

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
        s = Settings()
        assert s.tracing_enabled is True

    def test_tracing_enabled_false_missing_url(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
        # no LANGFUSE_BASE_URL
        s = Settings()
        assert s.tracing_enabled is False

    def test_tracing_enabled_false_missing_public_key(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_BASE_URL", "https://lf.example.com")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
        s = Settings()
        assert s.tracing_enabled is False

    def test_tracing_enabled_false_empty_string(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_BASE_URL", "")
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
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
