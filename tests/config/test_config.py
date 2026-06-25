from pathlib import Path

import pytest
from pydantic import ValidationError

from robotsix_mill.config import Settings, load_settings


def test_extra_kwargs_forbidden():
    """``extra='forbid'`` in model_config: passing an unknown kwarg
    raises ``ValidationError`` instead of silently dropping it.

    Reason this is REQUIRED, not just nice-to-have: a feature branch
    written before the YAML-only refactor may still pass legacy
    ``x=...`` style kwargs. With ``extra='ignore'`` those drop
    silently — the test passes because Settings constructs cleanly,
    the bug only surfaces when the value the test EXPECTED to set is
    actually read from somewhere else (real YAML default, real
    filesystem). That's exactly the failure mode that BLOCKED ticket
    ad2f's PR. ``extra='forbid'`` makes the typo a test-collection
    error so the implement agent can see it and fix it in-pass."""
    import pydantic

    with pytest.raises(pydantic.ValidationError, match="not_a_real_field"):
        Settings(not_a_real_field=42)

    s = Settings()
    assert s.web_search is True
    assert s.require_approval is True
    assert s.sandbox_readonly is True
    assert s.auto_approve_enabled is False
    assert s.review_enabled is False


def test_default_paths():
    """``data_dir`` and ``skills_dir`` Field defaults, read from the
    committed YAML defaults.

    We read directly from ``load_yaml_config()`` rather than
    constructing ``Settings()`` because the session-scoped
    ``_isolate_default_data_dir`` fixture monkey-patches the YAML
    source to redirect ``data_dir`` into a sandbox for every test
    that constructs a bare ``Settings()``.  ``load_yaml_config()``
    is not patched by that fixture and returns the real committed
    defaults."""
    from robotsix_mill.config.loader import load_yaml_config

    cfg = load_yaml_config()
    assert cfg["service"]["data_dir"] == ".data"
    assert cfg["sandbox"]["skills_dir"] == "skills"


def test_default_empty_and_none():
    """Representative empty-string, None, and sentinel defaults."""
    s = Settings()
    assert s.forge_kind == "none"
    assert s.openrouter_api_key is None
    assert s.forge_remote_url is None
    # Branch cleanup after merge is on by default.
    assert s.delete_branch_on_merge is True


def test_default_obsolescence_gate():
    """The opt-in obsolescence gate defaults to off with a modest
    per-call request budget."""
    s = Settings()
    assert s.obsolescence_gate_enabled is False
    assert s.obsolescence_request_limit == 6


def test_default_max_spend_sentinel():
    """max_spend_usd_per_ticket defaults to $20 — ON by default as the
    universal runaway-loop backstop (0.0 would disable it)."""
    s = Settings()
    assert s.max_spend_usd_per_ticket == 20.0


def test_default_coordinator_max_tool_calls():
    """coordinator_max_tool_calls defaults to 300 — the deterministic
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
    from pydantic import ValidationError

    from robotsix_mill.config._settings_core import _CoreSettings

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
    headroom above the sampled 736 s legitimate Opus refine while
    still catching multi-hour runaways.

    Also verifies ``default_factory`` isolates instances — mutating
    one instance's dict must not leak into another.

    Checks BOTH the raw ``_CoreSettings`` model (Field default) AND
    the full ``Settings`` class (YAML cascade) — the YAML defaults
    file must echo the same value so it doesn't shadow the Field
    default with an empty dict."""
    from robotsix_mill.config._settings_core import _CoreSettings

    # Field default on the raw model (no YAML cascade).
    s1 = _CoreSettings()
    assert s1.stage_timeout_overrides == {"refine": 900}

    # Full Settings class (YAML cascade must NOT shadow the Field default).
    from robotsix_mill.config import Settings

    s_full = Settings()
    assert s_full.stage_timeout_overrides == {"refine": 900}, (
        "Settings().stage_timeout_overrides must also be {refine: 900}; "
        "if this fails, config/mill.defaults.yaml still has stage_timeout_overrides: {} "
        "which shadows the Field default with an empty dict."
    )

    # Instance isolation: default_factory, not a shared mutable literal.
    s2 = _CoreSettings()
    s2.stage_timeout_overrides["refine"] = 600
    s2.stage_timeout_overrides["merge"] = 0
    s3 = _CoreSettings()
    assert s3.stage_timeout_overrides == {"refine": 900}, (
        "mutating one instance must not leak into another — "
        "default_factory must create a fresh dict each time"
    )


def test_default_board_agent_disabled():
    """Board agent is opt-in — off by default, pointed at this mill's own
    board API, with writes enabled and no broker configured."""
    s = Settings()
    assert s.board_agent_enabled is False
    assert s.board_agent_api_url == "http://127.0.0.1:8077"
    assert s.board_agent_api_token == ""
    assert s.board_agent_repo_id == ""
    assert s.board_agent_write_ops is True
    # Broker config is empty by default → the board agent stays off until a
    # broker_host is set (see lifespan._start_board_agent).
    assert s.board_agent_broker_host == ""
    assert s.board_agent_broker_port == 443
    assert s.board_agent_broker_scheme == "https"
    assert s.board_agent_broker_token == ""


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
    # --- core / api key ---
    ("openrouter_api_key", "OPENROUTER_API_KEY", "sk-test", "sk-test"),
    # --- request limits ---
    ("coordinator_request_limit", "MILL_PER_PASS_REQUEST_BUDGET", "42", 42),
    ("refine_request_limit", "MILL_REFINE_REQUEST_LIMIT", "42", 42),
    ("test_request_limit", "MILL_TEST_REQUEST_LIMIT", "15", 15),
    ("maintenance_request_limit", "MILL_MAINTENANCE_REQUEST_LIMIT", "33", 33),
    ("max_fix_iterations", "MILL_MAX_FIX_ITERATIONS", "6", 6),
    # --- retry / backoff ---
    ("transient_retries", "MILL_TRANSIENT_RETRIES", "7", 7),
    ("transient_backoff_base", "MILL_TRANSIENT_BACKOFF_BASE", "5.0", 5.0),
    ("transient_backoff_cap", "MILL_TRANSIENT_BACKOFF_CAP", "60.0", 60.0),
    ("rate_limit_backoff_base", "MILL_RATE_LIMIT_BACKOFF_BASE", "10.0", 10.0),
    ("rate_limit_backoff_cap", "MILL_RATE_LIMIT_BACKOFF_CAP", "300.0", 300.0),
    ("explore_request_limit", "MILL_EXPLORE_REQUEST_LIMIT", "99", 99),
    ("dedup_request_limit", "MILL_DEDUP_REQUEST_LIMIT", "3", 3),
    (
        "obsolescence_request_limit",
        "MILL_OBSOLESCENCE_REQUEST_LIMIT",
        "5",
        5,
    ),
    # --- memory / reference files ---
    ("max_memory_chars", "MILL_MAX_MEMORY_CHARS", "16000", 16000),
    ("retrospect_log_max_chars", "MILL_RETROSPECT_LOG_MAX_CHARS", "15000", 15000),
    ("reference_files_max_count", "MILL_REFERENCE_FILES_MAX_COUNT", "10", 10),
    (
        "reference_files_max_total_lines",
        "MILL_REFERENCE_FILES_MAX_TOTAL_LINES",
        "5000",
        5000,
    ),
    ("dedup_lookback_days", "MILL_DEDUP_LOOKBACK_DAYS", "14", 14),
    (
        "review_prior_context_max_chars",
        "MILL_REVIEW_PRIOR_CONTEXT_MAX_CHARS",
        "16000",
        16000,
    ),
    (
        "dedup_skip_on_no_overlap",
        "MILL_DEDUP_SKIP_ON_NO_OVERLAP",
        "0",
        False,
    ),
    (
        "dedup_candidate_body_max_chars",
        "MILL_DEDUP_CANDIDATE_BODY_MAX_CHARS",
        "1234",
        1234,
    ),
    # --- paths ---
    ("data_dir", "MILL_DATA_DIR", "/custom/data", Path("/custom/data")),
    # --- API ---
    ("api_host", "MILL_API_HOST", "0.0.0.0", "0.0.0.0"),
    ("api_port", "MILL_API_PORT", "9090", 9090),
    ("api_url", "MILL_API_URL", "http://example.com:9999", "http://example.com:9999"),
    # --- forge ---
    ("forge_kind", "FORGE_KIND", "none", "none"),
    (
        "forge_remote_url",
        "FORGE_REMOTE_URL",
        "https://git.example.com/repo",
        "https://git.example.com/repo",
    ),
    ("forge_token", "FORGE_TOKEN", "glpat-xxxx", "glpat-xxxx"),
    ("forge_target_branch", "FORGE_TARGET_BRANCH", "develop", "develop"),
    ("forge_auth", "FORGE_AUTH", "token", "token"),
    ("github_app_id", "GITHUB_APP_ID", "123456", "123456"),
    ("github_app_private_key", "GITHUB_APP_PRIVATE_KEY", "pk-----", "pk-----"),
    (
        "github_app_private_key_path",
        "GITHUB_APP_PRIVATE_KEY_PATH",
        "/keys/app.pem",
        "/keys/app.pem",
    ),
    (
        "github_api_url",
        "MILL_GITHUB_API_URL",
        "https://github.myco.com",
        "https://github.myco.com",
    ),
    (
        "gitlab_api_url",
        "MILL_GITLAB_API_URL",
        "https://gitlab.myco.com/api/v4",
        "https://gitlab.myco.com/api/v4",
    ),
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
    ("web_research_request_limit", "MILL_WEB_RESEARCH_REQUEST_LIMIT", "4", 4),
    (
        "fetch_image",
        "MILL_FETCH_IMAGE",
        "curlimages/curl:latest",
        "curlimages/curl:latest",
    ),
    ("web_fetch_max_bytes", "MILL_WEB_FETCH_MAX_BYTES", "500000", 500000),
    ("web_fetch_timeout", "MILL_WEB_FETCH_TIMEOUT", "15", 15),
    ("skills_dir", "MILL_SKILLS_DIR", "/skills", Path("/skills")),
    # --- approval ---
    ("require_approval", "MILL_REQUIRE_APPROVAL", "false", False),
    ("auto_approve_enabled", "MILL_AUTO_APPROVE_ENABLED", "1", True),
    # --- review ---
    ("review_enabled", "MILL_REVIEW_ENABLED", "true", True),
    ("auto_merge_enabled", "MILL_AUTO_MERGE_ENABLED", "1", True),
    ("refine_triage_enabled", "MILL_REFINE_TRIAGE_ENABLED", "0", False),
    ("spec_review_enabled", "MILL_SPEC_REVIEW_ENABLED", "true", True),
    ("review_max_rounds", "MILL_REVIEW_MAX_ROUNDS", "1", 1),
    # --- retrospect ---
    ("retrospect_spawn_drafts", "MILL_RETROSPECT_SPAWN_DRAFTS", "0", False),
    (
        "retrospect_spawn_agented_proposals",
        "MILL_RETROSPECT_SPAWN_AGENTED_PROPOSALS",
        "1",
        True,
    ),
    (
        "trace_inspector_memory_path",
        "MILL_TRACE_INSPECTOR_MEMORY_PATH",
        "/mem/ti.md",
        Path("/mem/ti.md"),
    ),
    (
        "retrospect_memory_path",
        "MILL_RETROSPECT_MEMORY_PATH",
        "/mem/retro.md",
        Path("/mem/retro.md"),
    ),
    ("merge_poll_seconds", "MILL_MERGE_POLL_SECONDS", "60", 60),
    ("prune_clone_on_close", "MILL_PRUNE_CLONE_ON_CLOSE", "false", False),
    ("max_archived_tickets", "MILL_MAX_ARCHIVED_TICKETS", "50", 50),
    # --- rebase / CI fix ---
    ("rebase_max_attempts", "MILL_REBASE_MAX_ATTEMPTS", "2", 2),
    ("ci_fix_max_attempts", "MILL_CI_FIX_MAX_ATTEMPTS", "4", 4),
    # --- CI monitor (global cap only — enabled/interval are per-repo) ---
    ("ci_log_max_bytes", "MILL_CI_LOG_MAX_BYTES", "32768", 32768),
    # --- audit ---
    ("audit_periodic", "MILL_AUDIT_PERIODIC", "true", True),
    ("audit_interval_seconds", "MILL_AUDIT_INTERVAL_SECONDS", "7200", 7200),
    (
        "audit_memory_path",
        "MILL_AUDIT_MEMORY_PATH",
        "/mem/audit.md",
        Path("/mem/audit.md"),
    ),
    # --- trace health ---
    ("trace_health_periodic", "MILL_TRACE_HEALTH_PERIODIC", "1", True),
    (
        "trace_health_interval_seconds",
        "MILL_TRACE_HEALTH_INTERVAL_SECONDS",
        "3600",
        3600,
    ),
    # --- stale branch cleanup ---
    (
        "stale_branch_cleanup_periodic",
        "MILL_STALE_BRANCH_CLEANUP_PERIODIC",
        "1",
        True,
    ),
    (
        "stale_branch_cleanup_interval_seconds",
        "MILL_STALE_BRANCH_CLEANUP_INTERVAL_SECONDS",
        "7200",
        7200,
    ),
    (
        "stale_branch_max_age_days",
        "MILL_STALE_BRANCH_MAX_AGE_DAYS",
        "14",
        14,
    ),
    (
        "stale_branch_cleanup_prefix_only",
        "MILL_STALE_BRANCH_CLEANUP_PREFIX_ONLY",
        "0",
        False,
    ),
    # --- test gap ---
    ("test_gap_periodic", "MILL_TEST_GAP_PERIODIC", "1", True),
    ("test_gap_interval_seconds", "MILL_TEST_GAP_INTERVAL_SECONDS", "1800", 1800),
    (
        "test_gap_memory_path",
        "MILL_TEST_GAP_MEMORY_PATH",
        "/mem/tg.md",
        Path("/mem/tg.md"),
    ),
    # --- agent check ---
    (
        "agent_check_memory_path",
        "MILL_AGENT_CHECK_MEMORY_PATH",
        "/mem/ac.md",
        Path("/mem/ac.md"),
    ),
    ("agent_check_periodic", "MILL_AGENT_CHECK_PERIODIC", "1", True),
    ("agent_check_interval_seconds", "MILL_AGENT_CHECK_INTERVAL_SECONDS", "7200", 7200),
    # --- health ---
    ("health_periodic", "MILL_HEALTH_PERIODIC", "true", True),
    ("health_interval_seconds", "MILL_HEALTH_INTERVAL_SECONDS", "43200", 43200),
    (
        "health_memory_path",
        "MILL_HEALTH_MEMORY_PATH",
        "/mem/health.md",
        Path("/mem/health.md"),
    ),
    # --- survey ---
    (
        "survey_memory_path",
        "MILL_SURVEY_MEMORY_PATH",
        "/mem/survey.md",
        Path("/mem/survey.md"),
    ),
    ("survey_periodic", "MILL_SURVEY_PERIODIC", "0", False),
    ("survey_interval_seconds", "MILL_SURVEY_INTERVAL_SECONDS", "3600", 3600),
    # --- action-agent memory paths ---
    (
        "implement_memory_path",
        "MILL_IMPLEMENT_MEMORY_PATH",
        "/mem/imp.md",
        Path("/mem/imp.md"),
    ),
    (
        "refine_memory_path",
        "MILL_REFINE_MEMORY_PATH",
        "/mem/ref.md",
        Path("/mem/ref.md"),
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
    # --- notifications ---
    ("ntfy_url", "NTFY_URL", "https://ntfy.example.com", "https://ntfy.example.com"),
    ("ntfy_token", "NTFY_TOKEN", "tk-test", "tk-test"),
    # --- board agent ---
    ("board_agent_enabled", "MILL_BOARD_AGENT_ENABLED", "true", True),
    (
        "board_agent_api_url",
        "MILL_BOARD_AGENT_API_URL",
        "https://board.example.com",
        "https://board.example.com",
    ),
    (
        "board_agent_api_token",
        "MILL_BOARD_AGENT_API_TOKEN",
        "sk-board-token",
        "sk-board-token",
    ),
    ("board_agent_repo_id", "MILL_BOARD_AGENT_REPO_ID", "my-repo", "my-repo"),
    ("board_agent_write_ops", "MILL_BOARD_AGENT_WRITE_OPS", "false", False),
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
        monkeypatch.setenv("MILL_MAX_FIX_ITERATIONS", "8")
        s = Settings()
        assert s.max_fix_iterations == 8
        assert isinstance(s.max_fix_iterations, int)

    def test_float_coercion(self, monkeypatch):
        monkeypatch.setenv("MILL_TRANSIENT_BACKOFF_BASE", "2.5")
        s = Settings()
        assert s.transient_backoff_base == 2.5
        assert isinstance(s.transient_backoff_base, float)

    @pytest.mark.parametrize(
        "env_val,expected",
        [
            ("true", True),
            ("false", False),
            ("1", True),
            ("0", False),
            ("True", True),
            ("False", False),
        ],
    )
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

    def test_workspaces_dir(self, tmp_path):
        s = Settings(data_dir=str(tmp_path))
        assert s.workspaces_dir_for("my-board") == tmp_path / "my-board" / "workspaces"

    def test_workspaces_dir_empty_raises(self, tmp_path):
        s = Settings(data_dir=str(tmp_path))
        with pytest.raises(ValueError, match="board_id is required"):
            s.workspaces_dir_for("")

    # -- tracing_enabled --

    def test_tracing_enabled_true(self, secrets_set):
        secrets_set(
            langfuse_base_url="https://lf.example.com",
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
        )
        s = Settings()
        assert s.tracing_enabled is True

    def test_tracing_enabled_false_missing_url(self, secrets_set):
        secrets_set(
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
        )
        s = Settings()
        assert s.tracing_enabled is False

    def test_tracing_enabled_false_missing_public_key(self, secrets_set):
        secrets_set(
            langfuse_base_url="https://lf.example.com",
            langfuse_secret_key="sk-test",
        )
        s = Settings()
        assert s.tracing_enabled is False

    def test_tracing_enabled_false_empty_string(self, secrets_set):
        secrets_set(
            langfuse_base_url="",
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
        )
        s = Settings()
        assert s.tracing_enabled is False

    # -- memory_file properties (11 total) --

    MEMORY_FILE_PROPERTIES = [
        ("retrospect_memory_file", "retrospect_memory_path", "retrospect_memory.md"),
        (
            "trace_inspector_memory_file",
            "trace_inspector_memory_path",
            "trace_inspector_memory.md",
        ),
        ("audit_memory_file", "audit_memory_path", "audit_memory.md"),
        ("agent_check_memory_file", "agent_check_memory_path", "agent_check_memory.md"),
        ("health_memory_file", "health_memory_path", "health_memory.md"),
        ("test_gap_memory_file", "test_gap_memory_path", "test_gap_memory.md"),
        ("state_sync_memory_file", "state_sync_memory_path", "state_sync_memory.md"),
        ("survey_memory_file", "survey_memory_path", "survey_memory.md"),
        ("implement_memory_file", "implement_memory_path", "implement_memory.md"),
        ("refine_memory_file", "refine_memory_path", "refine_memory.md"),
        ("ci_fix_memory_file", "ci_fix_memory_path", "ci_fix_memory.md"),
        ("rebase_memory_file", "rebase_memory_path", "rebase_memory.md"),
    ]

    @pytest.mark.parametrize(
        "prop_name,override_field,fallback_filename", MEMORY_FILE_PROPERTIES
    )
    def test_memory_file_fallback(
        self, tmp_path, prop_name, override_field, fallback_filename
    ):
        """When the override path is None, the property derives from data_dir."""
        s = Settings(data_dir=str(tmp_path))
        assert getattr(s, prop_name) == tmp_path / fallback_filename

    # Map Python field name → env-var alias for the memory_path override fields
    MEMORY_PATH_ALIASES: dict[str, str] = {
        "retrospect_memory_path": "MILL_RETROSPECT_MEMORY_PATH",
        "trace_inspector_memory_path": "MILL_TRACE_INSPECTOR_MEMORY_PATH",
        "audit_memory_path": "MILL_AUDIT_MEMORY_PATH",
        "agent_check_memory_path": "MILL_AGENT_CHECK_MEMORY_PATH",
        "health_memory_path": "MILL_HEALTH_MEMORY_PATH",
        "test_gap_memory_path": "MILL_TEST_GAP_MEMORY_PATH",
        "state_sync_memory_path": "MILL_STATE_SYNC_MEMORY_PATH",
        "survey_memory_path": "MILL_SURVEY_MEMORY_PATH",
        "implement_memory_path": "MILL_IMPLEMENT_MEMORY_PATH",
        "refine_memory_path": "MILL_REFINE_MEMORY_PATH",
        "ci_fix_memory_path": "MILL_CI_FIX_MEMORY_PATH",
        "rebase_memory_path": "MILL_REBASE_MEMORY_PATH",
    }

    @pytest.mark.parametrize(
        "prop_name,override_field,fallback_filename", MEMORY_FILE_PROPERTIES
    )
    def test_memory_file_override(
        self, monkeypatch, tmp_path, prop_name, override_field, fallback_filename
    ):
        """When the override path is set, it wins over the derived path."""
        custom = Path("/custom/memory.md")
        alias = self.MEMORY_PATH_ALIASES[override_field]
        monkeypatch.setenv(alias, str(custom))
        s = Settings(data_dir=str(tmp_path))
        assert getattr(s, prop_name) == custom

    def test_memory_file_edge_case_empty_override(self, monkeypatch, tmp_path):
        """retrospect_memory_path='' is falsy but not None — property
        should still return the explicit value (empty Path), not the
        fallback."""
        monkeypatch.setenv("MILL_RETROSPECT_MEMORY_PATH", "")
        s = Settings(data_dir=str(tmp_path))
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
# 6. load_settings() smoke test
# ---------------------------------------------------------------------------


def test_load_settings_returns_settings_instance():
    """Trivial: the module-level factory returns a Settings object."""
    s = load_settings()
    assert isinstance(s, Settings)


# ---------------------------------------------------------------------------
# 7. YAML config loading
# ---------------------------------------------------------------------------


class TestYamlLoading:
    """Tests for ``config_loader.load_yaml_config`` and
    ``config_loader.load_secrets_yaml``."""

    def test_load_yaml_config_returns_nested_dict(self):
        """``load_yaml_config()`` returns a nested dict matching the
        defaults YAML structure when no overlays are present."""
        from robotsix_mill.config.loader import load_yaml_config

        config = load_yaml_config()
        assert isinstance(config, dict)
        assert "core" in config
        assert "limits" in config["core"]
        assert config["core"]["limits"]["max_fix_iterations"] == 8
        assert "service" in config
        assert config["service"]["data_dir"] == ".data"

    def test_load_yaml_config_subscription_tier_defaults(self):
        """Defaults YAML carries the subscription-tier routing settings
        with the documented defaults."""
        from robotsix_mill.config.loader import load_yaml_config

        config = load_yaml_config()
        gates = config.get("gates", {})
        assert gates.get("refine_subscription_tier_routing_enabled") is True
        assert gates.get("refine_subscription_model_default") == "sonnet"
        assert gates.get("refine_subscription_model_complex") == "opus"
        assert gates.get("refine_trivial_model_level") == 3
        assert gates.get("refine_trivial_subscription_model") == "sonnet"
        limits = config.get("core", {}).get("limits", {})
        assert limits.get("refine_requests_simple") == 40

    def test_load_yaml_config_deep_merges_local_overlay(self, tmp_path, monkeypatch):
        """``load_yaml_config()`` deep-merges a local overlay YAML over
        defaults."""
        from robotsix_mill.config.loader import load_yaml_config

        # Write a temporary local overlay
        local_dir = tmp_path / "config"
        local_dir.mkdir()
        defaults = local_dir / "mill.defaults.yaml"
        # Copy the real defaults so the loader finds them
        import shutil

        shutil.copy("config/mill.defaults.yaml", defaults)
        local = local_dir / "mill.local.yaml"
        local.write_text("core:\n  limits:\n    max_fix_iterations: 99\n")

        # Patch the module-level path to point at our temp dir
        import robotsix_mill.config.loader as cl

        monkeypatch.setattr(cl, "_DEFAULTS_FILE", defaults)
        monkeypatch.setattr(cl, "_LOCAL_FILE", local)

        config = load_yaml_config()
        assert config["core"]["limits"]["max_fix_iterations"] == 99
        # Other values unchanged from defaults
        assert config["core"]["limits"]["max_stuck_cycles"] == 3

    def test_load_yaml_config_missing_defaults_raises(self, monkeypatch):
        """``load_yaml_config()`` raises ``ConfigError`` when
        ``mill.defaults.yaml`` is missing."""
        from robotsix_mill.config.loader import ConfigError, load_yaml_config
        import robotsix_mill.config.loader as cl

        monkeypatch.setattr(cl, "_DEFAULTS_FILE", cl.Path("/nonexistent/defaults.yaml"))
        with pytest.raises(ConfigError, match="Required config file not found"):
            load_yaml_config()

    def test_load_secrets_yaml_returns_populated_dict(self, tmp_path):
        """``load_secrets_yaml()`` returns a populated dict when a valid
        secrets file exists."""
        from robotsix_mill.config.loader import load_secrets_yaml

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
        from robotsix_mill.config.loader import load_secrets_yaml

        result = load_secrets_yaml("/nonexistent/secrets.yaml")
        assert result == {}

    def test_load_secrets_yaml_malformed_raises(self, tmp_path):
        """``load_secrets_yaml()`` raises ``ConfigError`` on malformed YAML."""
        from robotsix_mill.config.loader import ConfigError, load_secrets_yaml

        secrets_file = tmp_path / "bad.yaml"
        secrets_file.write_text("{ invalid: yaml: : }")
        with pytest.raises(ConfigError, match="YAML parse error"):
            load_secrets_yaml(str(secrets_file))

    # -- load_yaml_config edge cases -----------------------------------

    def test_load_yaml_config_empty_config_file(self):
        """``load_yaml_config(config_file="")`` treats explicit empty
        string as "no production file" and does NOT raise."""
        from robotsix_mill.config.loader import load_yaml_config

        result = load_yaml_config(config_file="")
        assert isinstance(result, dict)
        assert result  # non-empty (real defaults loaded)

    def test_load_yaml_config_malformed_defaults_raises(self, tmp_path, monkeypatch):
        """Malformed YAML in the defaults file raises ``ConfigError``."""
        from robotsix_mill.config.loader import ConfigError, load_yaml_config
        import robotsix_mill.config.loader as cl

        defaults = tmp_path / "bad_defaults.yaml"
        defaults.write_text("{ invalid: yaml: : }")
        monkeypatch.setattr(cl, "_DEFAULTS_FILE", defaults)
        with pytest.raises(ConfigError, match="YAML parse error"):
            load_yaml_config()

    def test_load_yaml_config_malformed_local_raises(self, tmp_path, monkeypatch):
        """Malformed YAML in the local overlay raises ``ConfigError``."""
        from robotsix_mill.config.loader import ConfigError, load_yaml_config
        import robotsix_mill.config.loader as cl
        import shutil

        local_dir = tmp_path / "config"
        local_dir.mkdir()
        defaults = local_dir / "mill.defaults.yaml"
        shutil.copy("config/mill.defaults.yaml", defaults)
        local = local_dir / "mill.local.yaml"
        local.write_text("{ invalid: yaml: : }")
        monkeypatch.setattr(cl, "_DEFAULTS_FILE", defaults)
        monkeypatch.setattr(cl, "_LOCAL_FILE", local)
        with pytest.raises(ConfigError, match="YAML parse error"):
            load_yaml_config()

    def test_load_yaml_config_three_layer_cascade(self, tmp_path, monkeypatch):
        """Defaults + local + production deep-merge: local overrides
        defaults, production overrides local."""
        from robotsix_mill.config.loader import load_yaml_config
        import robotsix_mill.config.loader as cl
        import shutil

        local_dir = tmp_path / "config"
        local_dir.mkdir()
        defaults = local_dir / "mill.defaults.yaml"
        shutil.copy("config/mill.defaults.yaml", defaults)
        local = local_dir / "mill.local.yaml"
        local.write_text("core:\n  limits:\n    max_fix_iterations: 99\n")
        prod = local_dir / "mill.production.yaml"
        prod.write_text("core:\n  limits:\n    max_fix_iterations: 50\n")
        monkeypatch.setattr(cl, "_DEFAULTS_FILE", defaults)
        monkeypatch.setattr(cl, "_LOCAL_FILE", local)
        config = load_yaml_config(config_file=str(prod))
        assert config["core"]["limits"]["max_fix_iterations"] == 50

    def test_load_yaml_config_skip_local_with_production(self, tmp_path, monkeypatch):
        """``skip_local=True`` with a production file skips the local
        overlay but includes production."""
        from robotsix_mill.config.loader import load_yaml_config
        import robotsix_mill.config.loader as cl
        import shutil

        local_dir = tmp_path / "config"
        local_dir.mkdir()
        defaults = local_dir / "mill.defaults.yaml"
        shutil.copy("config/mill.defaults.yaml", defaults)
        local = local_dir / "mill.local.yaml"
        local.write_text("core:\n  limits:\n    max_fix_iterations: 99\n")
        prod = local_dir / "mill.production.yaml"
        prod.write_text("core:\n  limits:\n    max_fix_iterations: 50\n")
        monkeypatch.setattr(cl, "_DEFAULTS_FILE", defaults)
        monkeypatch.setattr(cl, "_LOCAL_FILE", local)
        config = load_yaml_config(config_file=str(prod), skip_local=True)
        # Production override (50) is applied; local override (99) is skipped.
        # The real defaults value is 8.
        assert config["core"]["limits"]["max_fix_iterations"] == 50

    def test_load_yaml_config_subscription_tier_override(self, tmp_path, monkeypatch):
        """A YAML override for the subscription-tier routing settings
        is correctly mapped through the loader aliases."""
        from robotsix_mill.config.loader import load_yaml_config
        import robotsix_mill.config.loader as cl
        import shutil

        local_dir = tmp_path / "config"
        local_dir.mkdir()
        defaults = local_dir / "mill.defaults.yaml"
        shutil.copy("config/mill.defaults.yaml", defaults)
        local = local_dir / "mill.local.yaml"
        local.write_text(
            "gates:\n"
            "  refine_subscription_tier_routing_enabled: false\n"
            "  refine_subscription_model_default: haiku\n"
            "  refine_subscription_model_complex: sonnet\n"
            "  refine_trivial_model_level: 1\n"
            "  refine_trivial_subscription_model: haiku\n"
            "core:\n"
            "  limits:\n"
            "    refine_requests_simple: 25\n"
        )
        monkeypatch.setattr(cl, "_DEFAULTS_FILE", defaults)
        monkeypatch.setattr(cl, "_LOCAL_FILE", local)
        config = load_yaml_config()
        assert config["gates"]["refine_subscription_tier_routing_enabled"] is False
        assert config["gates"]["refine_subscription_model_default"] == "haiku"
        assert config["gates"]["refine_subscription_model_complex"] == "sonnet"
        assert config["gates"]["refine_trivial_model_level"] == 1
        assert config["gates"]["refine_trivial_subscription_model"] == "haiku"
        assert config["core"]["limits"]["refine_requests_simple"] == 25

    # -- load_secrets_yaml edge cases ----------------------------------

    def test_load_secrets_yaml_empty_string_path(self):
        """``load_secrets_yaml("")`` returns ``{}`` (explicit no-file)."""
        from robotsix_mill.config.loader import load_secrets_yaml

        assert load_secrets_yaml("") == {}

    def test_load_secrets_yaml_env_var_overrides_default(self, tmp_path, monkeypatch):
        """``MILL_SECRETS_FILE`` env var pointing to a valid temp YAML
        file is used instead of the default ``config/secrets.yaml``."""
        from robotsix_mill.config.loader import load_secrets_yaml

        secrets_file = tmp_path / "custom_secrets.yaml"
        secrets_file.write_text("openrouter_api_key: sk-from-env\n")
        monkeypatch.setenv("MILL_SECRETS_FILE", str(secrets_file))
        result = load_secrets_yaml()
        assert result == {"openrouter_api_key": "sk-from-env"}


# ---------------------------------------------------------------------------
# 8. Secrets model
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
            'github_app_id: "456"\n'
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
        from robotsix_mill.config.loader import load_repos_yaml

        result = load_repos_yaml("/nonexistent/repos.yaml")
        assert result == {}

    def test_malformed_yaml_raises(self, tmp_path):
        """Malformed YAML → raises ``ConfigError`` with file path in message."""
        from robotsix_mill.config.loader import ConfigError, load_repos_yaml

        bad_file = tmp_path / "bad.yaml"
        bad_file.write_text("{ invalid: yaml: : }")
        with pytest.raises(ConfigError, match="YAML parse error"):
            load_repos_yaml(str(bad_file))

    def test_valid_file_returns_dict(self, tmp_path):
        """Valid YAML → returns dict keyed by repo ID with nested
        ``board_id`` and ``langfuse`` sub-dict."""
        from robotsix_mill.config.loader import load_repos_yaml

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
        from robotsix_mill.config.loader import load_repos_yaml

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
        from robotsix_mill.config.loader import load_repos_yaml

        monkeypatch.setenv("MILL_REPOS_FILE", "")
        result = load_repos_yaml()
        assert result == {}

    def test_explicit_arg_overrides_env_var(self, tmp_path, monkeypatch):
        """Explicit ``file_path`` arg takes precedence over env var."""
        from robotsix_mill.config.loader import load_repos_yaml

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
        env_file.write_text(
            "repos:\n  env-repo:\n    board_id: wrong\n    langfuse:\n      project_name: w\n      public_key: w\n      secret_key: w\n"
        )
        monkeypatch.setenv("MILL_REPOS_FILE", str(env_file))
        result = load_repos_yaml(str(explicit_file))
        assert "explicit-repo" in result
        assert "env-repo" not in result

    def test_top_level_meta_block_not_surfaced_as_repo(self, tmp_path):
        """A sibling ``meta:`` block is NOT returned as a repo entry."""
        from robotsix_mill.config.loader import load_repos_yaml

        repos_file = tmp_path / "repos.yaml"
        repos_file.write_text(
            "repos:\n"
            "  my-repo:\n"
            "    board_id: my-board\n"
            "meta:\n"
            "  langfuse:\n"
            "    public_key: pk-meta\n"
            "    secret_key: sk-meta\n"
        )
        result = load_repos_yaml(str(repos_file))
        assert "my-repo" in result
        assert "meta" not in result

    # -- legacy / edge-case paths ---------------------------------------

    def test_legacy_flat_format(self, tmp_path):
        """YAML document without a ``repos:`` top-level key — the
        document itself IS the repo mapping (with ``meta`` filtered out)."""
        from robotsix_mill.config.loader import load_repos_yaml

        repos_file = tmp_path / "repos.yaml"
        repos_file.write_text(
            "my-repo:\n"
            "  board_id: my-board\n"
            "  langfuse:\n"
            "    public_key: pk-1\n"
            "other-repo:\n"
            "  board_id: other-board\n"
            "meta:\n"
            "  langfuse:\n"
            "    public_key: pk-meta\n"
        )
        result = load_repos_yaml(str(repos_file))
        assert result == {
            "my-repo": {"board_id": "my-board", "langfuse": {"public_key": "pk-1"}},
            "other-repo": {"board_id": "other-board"},
        }
        assert "meta" not in result

    def test_non_dict_repos_value_raises(self, tmp_path):
        """A ``repos:`` key whose value is a string (not a mapping)
        raises ``ConfigError``."""
        from robotsix_mill.config.loader import ConfigError, load_repos_yaml

        repos_file = tmp_path / "repos.yaml"
        repos_file.write_text("repos: not-a-mapping\n")
        with pytest.raises(ConfigError, match="Expected a mapping under 'repos'"):
            load_repos_yaml(str(repos_file))

    def test_empty_repos_dict(self, tmp_path):
        """``repos: {}`` returns an empty dict (not an error)."""
        from robotsix_mill.config.loader import load_repos_yaml

        repos_file = tmp_path / "repos.yaml"
        repos_file.write_text("repos: {}\n")
        result = load_repos_yaml(str(repos_file))
        assert result == {}


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

    # -- ci_monitor fields --

    def test_ci_monitor_defaults(self):
        """ci_monitor_enabled defaults to True, interval defaults to 900."""
        from robotsix_mill.config import RepoConfig

        rc = RepoConfig(
            repo_id="r",
            board_id="b",
            langfuse_project_name="p",
            langfuse_public_key="pk",
            langfuse_secret_key="sk",
        )
        assert rc.ci_monitor_enabled is True
        assert rc.ci_monitor_interval_seconds == 900

    def test_ci_monitor_interval_minimum(self):
        """ci_monitor_interval_seconds < 60 raises ValidationError."""
        from pydantic import ValidationError
        from robotsix_mill.config import RepoConfig

        with pytest.raises(ValidationError, match="ci_monitor_interval_seconds"):
            RepoConfig(
                repo_id="r",
                board_id="b",
                langfuse_project_name="p",
                langfuse_public_key="pk",
                langfuse_secret_key="sk",
                ci_monitor_interval_seconds=30,
            )

    def test_ci_monitor_custom(self):
        """ci_monitor fields can be overridden."""
        from robotsix_mill.config import RepoConfig

        rc = RepoConfig(
            repo_id="r",
            board_id="b",
            langfuse_project_name="p",
            langfuse_public_key="pk",
            langfuse_secret_key="sk",
            ci_monitor_enabled=False,
            ci_monitor_interval_seconds=3600,
        )
        assert rc.ci_monitor_enabled is False
        assert rc.ci_monitor_interval_seconds == 3600

    # -- test_command + language moved OUT of RepoConfig into the repo's own
    # .robotsix-mill/config.yaml; RepoConfig no longer carries them.

    def test_repoconfig_rejects_removed_per_repo_keys(self):
        """``test_command`` / ``language`` are no longer RepoConfig fields."""
        from robotsix_mill.config import RepoConfig

        assert "test_command" not in RepoConfig.model_fields
        assert "language" not in RepoConfig.model_fields


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

    def test_valid_yaml_produces_registry(self, tmp_path, secrets_set):
        """``load_repos_config()`` from valid YAML returns populated registry."""
        from robotsix_mill.config import load_repos_config, RepoConfig, ReposRegistry

        secrets_set(
            langfuse_public_key="pk-a",
            langfuse_secret_key="sk-a",
            langfuse_project_name="proj-a",
        )
        repos_file = tmp_path / "repos.yaml"
        repos_file.write_text("repos:\n  repo-a:\n    board_id: board-a\n")
        rr = load_repos_config(str(repos_file))
        assert isinstance(rr, ReposRegistry)
        assert "repo-a" in rr.repos
        rc = rr.repos["repo-a"]
        assert isinstance(rc, RepoConfig)
        assert rc.repo_id == "repo-a"
        assert rc.board_id == "board-a"
        # Langfuse comes from the global secrets, not per repo.
        assert rc.langfuse_project_name == "proj-a"
        assert rc.langfuse_public_key == "pk-a"
        assert rc.langfuse_secret_key == "sk-a"
        assert rc.langfuse_base_url == "https://cloud.langfuse.com"

    def test_yaml_parses_ci_monitor_fields(self, tmp_path):
        """``load_repos_config()`` parses the optional ``ci_monitor`` sub-dict."""
        from robotsix_mill.config import load_repos_config

        repos_file = tmp_path / "repos.yaml"
        repos_file.write_text(
            "repos:\n"
            "  repo-a:\n"
            "    board_id: board-a\n"
            "    langfuse:\n"
            "      project_name: proj-a\n"
            "      public_key: pk-a\n"
            "      secret_key: sk-a\n"
            "    ci_monitor:\n"
            "      enabled: false\n"
            "      interval_seconds: 7200\n"
        )
        rr = load_repos_config(str(repos_file))
        rc = rr.repos["repo-a"]
        assert rc.ci_monitor_enabled is False
        assert rc.ci_monitor_interval_seconds == 7200

    def test_yaml_parses_deployed_log_folder(self, tmp_path):
        """``load_repos_config()`` parses the optional per-repo
        ``deployed_log_folder`` host path; absent → ``None``."""
        from robotsix_mill.config import load_repos_config

        repos_file = tmp_path / "repos.yaml"
        repos_file.write_text(
            "repos:\n"
            "  repo-a:\n"
            "    board_id: board-a\n"
            "    deployed_log_folder: /var/log/repo-a\n"
            "  repo-b:\n"
            "    board_id: board-b\n"
        )
        rr = load_repos_config(str(repos_file))
        assert rr.repos["repo-a"].deployed_log_folder == "/var/log/repo-a"
        assert rr.repos["repo-b"].deployed_log_folder is None

    def test_cross_repo_target_with_gitlab_forge_raises(self, tmp_path, monkeypatch):
        """A ``cross_repo_target`` on a repo while ``FORGE_KIND=gitlab`` →
        ``ConfigError`` naming the repo (cross-fork MRs are GitHub-only)."""
        from robotsix_mill.config import load_repos_config
        from robotsix_mill.config.loader import ConfigError

        monkeypatch.setenv("FORGE_KIND", "gitlab")
        monkeypatch.setenv("FORGE_REMOTE_URL", "https://gitlab.com/o/r.git")
        repos_file = tmp_path / "repos.yaml"
        repos_file.write_text(
            "repos:\n"
            "  repo-a:\n"
            "    board_id: board-a\n"
            "    cross_repo_target:\n"
            "      upstream_remote_url: https://gitlab.com/up/r.git\n"
            "      fork_remote_url: https://gitlab.com/fork/r.git\n"
        )
        with pytest.raises(ConfigError, match="repo-a.*GitHub-only"):
            load_repos_config(str(repos_file))

    def test_cross_repo_target_with_github_forge_loads(self, tmp_path, monkeypatch):
        """A ``cross_repo_target`` on a repo while ``FORGE_KIND=github`` →
        loads OK (cross-fork MRs are supported by the GitHub adapter)."""
        from robotsix_mill.config import load_repos_config

        monkeypatch.setenv("FORGE_KIND", "github")
        monkeypatch.setenv("FORGE_REMOTE_URL", "https://github.com/o/r.git")
        repos_file = tmp_path / "repos.yaml"
        repos_file.write_text(
            "repos:\n"
            "  repo-a:\n"
            "    board_id: board-a\n"
            "    cross_repo_target:\n"
            "      upstream_remote_url: https://github.com/up/r.git\n"
            "      fork_remote_url: https://github.com/fork/r.git\n"
        )
        rr = load_repos_config(str(repos_file))
        assert rr.repos["repo-a"].cross_repo_target is not None

    def test_gitlab_forge_without_cross_repo_target_loads(self, tmp_path, monkeypatch):
        """``FORGE_KIND=gitlab`` with no ``cross_repo_target`` on any repo →
        loads OK (regression: existing GitLab configs keep loading)."""
        from robotsix_mill.config import load_repos_config

        monkeypatch.setenv("FORGE_KIND", "gitlab")
        monkeypatch.setenv("FORGE_REMOTE_URL", "https://gitlab.com/o/r.git")
        repos_file = tmp_path / "repos.yaml"
        repos_file.write_text("repos:\n  repo-a:\n    board_id: board-a\n")
        rr = load_repos_config(str(repos_file))
        assert rr.repos["repo-a"].cross_repo_target is None

    def test_meta_board_inherits_global_langfuse(self, tmp_path, secrets_set):
        """The synthetic meta board is configured from the global secrets
        (kept OUT of ``repos``)."""
        from robotsix_mill.config import load_repos_config

        secrets_set(
            langfuse_public_key="pk-global",
            langfuse_secret_key="sk-global",
            langfuse_project_name="robotsix-mill",
            langfuse_base_url="https://lf.example.net",
        )
        repos_file = tmp_path / "repos.yaml"
        repos_file.write_text("repos:\n  repo-a:\n    board_id: board-a\n")
        rr = load_repos_config(str(repos_file))
        # meta is NOT a repo
        assert "meta" not in rr.repos
        assert rr.meta is not None
        assert rr.meta.repo_id == "meta"
        assert rr.meta.board_id == "meta"
        assert rr.meta.langfuse_project_name == "robotsix-mill"
        assert rr.meta.langfuse_public_key == "pk-global"
        assert rr.meta.langfuse_secret_key == "sk-global"
        assert rr.meta.langfuse_base_url == "https://lf.example.net"

    def test_meta_absent_yields_none(self, tmp_path):
        """No ``meta:`` block → ``rr.meta`` is None (meta runs untraced)."""
        from robotsix_mill.config import load_repos_config

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
        assert rr.meta is None

    def test_meta_without_keys_yields_none(self, tmp_path):
        """A ``meta:`` block missing the secret key → ``rr.meta`` is None
        (incomplete creds are ignored, not half-applied)."""
        from robotsix_mill.config import load_repos_config

        repos_file = tmp_path / "repos.yaml"
        repos_file.write_text(
            "repos:\n"
            "  repo-a:\n"
            "    board_id: board-a\n"
            "    langfuse:\n"
            "      project_name: proj-a\n"
            "      public_key: pk-a\n"
            "      secret_key: sk-a\n"
            "meta:\n"
            "  langfuse:\n"
            "    project_name: robotsix-meta\n"
            "    public_key: pk-meta\n"
        )
        rr = load_repos_config(str(repos_file))
        assert rr.meta is None

    def test_yaml_ci_monitor_defaults_when_absent(self, tmp_path):
        """When ``ci_monitor`` is absent, fields default to True / 900."""
        from robotsix_mill.config import load_repos_config

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
        rc = rr.repos["repo-a"]
        assert rc.ci_monitor_enabled is True
        assert rc.ci_monitor_interval_seconds == 900

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

    def test_get_repo_config_valid_id(self, tmp_path, secrets_set):
        """``get_repo_config()`` with a valid ID returns the correct config."""
        from robotsix_mill.config import (
            _reset_repos_config,
            get_repo_config,
            load_repos_config,
        )

        secrets_set(
            langfuse_public_key="pk",
            langfuse_secret_key="sk",
            langfuse_project_name="my-proj",
        )
        repos_file = tmp_path / "repos.yaml"
        repos_file.write_text("repos:\n  my-repo:\n    board_id: my-board\n")
        _reset_repos_config()
        import robotsix_mill.config as _cfg

        _cfg._repos_config = load_repos_config(str(repos_file))

        rc = get_repo_config("my-repo")
        assert rc.repo_id == "my-repo"
        assert rc.board_id == "my-board"
        assert rc.langfuse_project_name == "my-proj"

    def test_get_repo_config_unknown_id(self):
        """``get_repo_config()`` with unknown ID raises ``ConfigError``."""
        from robotsix_mill.config.loader import ConfigError
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

    def test_language_in_repos_yaml_is_ignored(self, tmp_path):
        """A stray ``language`` key in repos.yaml is silently ignored —
        language now lives in the repo's own .robotsix-mill/config.yaml."""
        from robotsix_mill.config import load_repos_config

        repos_file = tmp_path / "repos.yaml"
        repos_file.write_text(
            "repos:\n"
            "  r:\n"
            "    board_id: b\n"
            "    language: python\n"  # stray legacy key — ignored
            "    langfuse:\n"
            "      project_name: p\n"
            "      public_key: pk\n"
            "      secret_key: sk\n"
        )
        rr = load_repos_config(str(repos_file))
        assert not hasattr(rr.repos["r"], "language")


# ---------------------------------------------------------------------------
# 11. Semantic validators
# ---------------------------------------------------------------------------


class TestValidationValid:
    """Valid Settings constructions that pass all validators."""

    def test_default_settings_passes(self):
        """Default ``Settings()`` passes all validators (smoke test)."""
        s = Settings()
        assert s.max_fix_iterations == 8

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

    def test_forge_kind_auto_with_remote_url_passes(self):
        """``forge_kind=auto`` with ``forge_remote_url`` passes validation."""
        s = Settings(
            FORGE_KIND="auto",
            FORGE_REMOTE_URL="https://github.com/o/r.git",
        )
        assert s.forge_kind == "auto"


class TestValidationInvalid:
    """Invalid Settings constructions that must raise ``ValidationError``."""

    def test_api_url_invalid_format_raises(self):
        """``api_url`` not starting with http(s) raises ValidationError."""
        with pytest.raises(ValidationError, match="String should match pattern"):
            Settings(api_url="not-a-url")

    def test_github_api_url_invalid_format_raises(self):
        """``github_api_url`` not starting with http(s) raises."""
        with pytest.raises(ValidationError, match="String should match pattern"):
            Settings(github_api_url="ftp://bad")

    def test_gitlab_api_url_invalid_format_raises(self):
        """``gitlab_api_url`` not starting with http(s) raises."""
        with pytest.raises(ValidationError, match="String should match pattern"):
            Settings(gitlab_api_url="ftp://bad")

    def test_trace_health_interval_too_low_raises(self):
        """``trace_health_interval_seconds=60`` raises ValidationError."""
        with pytest.raises(
            ValidationError, match="trace_health_interval_seconds must be ≥ 3600"
        ):
            Settings(trace_health_interval_seconds=60)

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

    def test_forge_kind_auto_without_remote_url_raises(self):
        """``forge_kind=auto`` without ``forge_remote_url`` raises."""
        with pytest.raises(
            ValidationError, match="forge_kind=auto requires forge_remote_url"
        ):
            Settings(FORGE_KIND="auto")

    def test_explore_request_limit_zero_raises(self):
        """``explore_request_limit=0`` raises ValidationError."""
        with pytest.raises(
            ValidationError, match="Input should be greater than or equal to 1"
        ):
            Settings(explore_request_limit=0)

    def test_refine_request_limit_zero_raises(self):
        """``refine_request_limit=0`` raises ValidationError."""
        with pytest.raises(
            ValidationError, match="Input should be greater than or equal to 1"
        ):
            Settings(refine_request_limit=0)

    def test_refine_request_limit_simple_zero_raises(self):
        """``refine_request_limit_simple=0`` raises ValidationError."""
        with pytest.raises(
            ValidationError, match="Input should be greater than or equal to 1"
        ):
            Settings(refine_request_limit_simple=0)

    def test_refine_subscription_settings_defaults(self):
        """Default values for the subscription-tier routing settings."""
        s = Settings(data_dir="/tmp")
        assert s.refine_subscription_tier_routing_enabled is True
        assert s.refine_subscription_model_default == "sonnet"
        assert s.refine_subscription_model_complex == "opus"
        assert s.refine_request_limit_simple == 40
        assert s.refine_findings_downgrade_enabled is True
        assert s.refine_findings_downgrade_min_chars == 200
        assert s.refine_subscription_model_findings == "sonnet"
        assert s.refine_trivial_model_level == 3
        assert s.refine_trivial_subscription_model == "sonnet"

    def test_refine_subscription_settings_custom_values(self):
        """Subscription-tier routing settings accept custom values."""
        s = Settings(
            data_dir="/tmp",
            refine_subscription_tier_routing_enabled=False,
            refine_subscription_model_default="haiku",
            refine_subscription_model_complex="sonnet",
            refine_request_limit_simple=25,
            refine_trivial_model_level=1,
            refine_trivial_subscription_model="sonnet",
        )
        assert s.refine_subscription_tier_routing_enabled is False
        assert s.refine_subscription_model_default == "haiku"
        assert s.refine_subscription_model_complex == "sonnet"
        assert s.refine_request_limit_simple == 25
        assert s.refine_trivial_model_level == 1
        assert s.refine_trivial_subscription_model == "sonnet"


# ---------------------------------------------------------------------------
# 12. Integration: load_settings / load_secrets factories
# ---------------------------------------------------------------------------


class TestFactories:
    """Integration tests for ``load_settings()`` and ``load_secrets()``."""

    def test_load_settings_returns_settings_with_yaml_defaults(
        self, tmp_path, monkeypatch
    ):
        """``load_settings()`` applies YAML values when no other source
        sets them."""
        import robotsix_mill.config.loader as cl

        # Write a temporary defaults file with a non-standard value
        local_dir = tmp_path / "config"
        local_dir.mkdir()
        defaults = local_dir / "mill.defaults.yaml"
        import shutil

        shutil.copy("config/mill.defaults.yaml", defaults)
        defaults.write_text(
            defaults.read_text().replace(
                "max_fix_iterations: 8", "max_fix_iterations: 42"
            )
        )
        monkeypatch.setattr(cl, "_DEFAULTS_FILE", defaults)
        monkeypatch.setattr(cl, "_LOCAL_FILE", cl.Path("/nonexistent/mill.local.yaml"))

        from robotsix_mill.config import load_settings

        s = load_settings()
        assert s.max_fix_iterations == 42

    def test_load_secrets_returns_secrets_object(self):
        """``load_secrets()`` returns a ``Secrets`` object."""
        from robotsix_mill.config import load_secrets, Secrets

        s = load_secrets()
        assert isinstance(s, Secrets)

    def test_load_settings_env_override_yaml(self, tmp_path, monkeypatch):
        """Env vars override YAML defaults in ``load_settings()``."""
        import robotsix_mill.config.loader as cl

        # Local overlay sets max_fix_iterations=42, but env var says 77
        local_dir = tmp_path / "config"
        local_dir.mkdir()
        defaults = local_dir / "mill.defaults.yaml"
        import shutil

        shutil.copy("config/mill.defaults.yaml", defaults)
        local = local_dir / "mill.local.yaml"
        local.write_text("core:\n  limits:\n    max_fix_iterations: 42\n")
        monkeypatch.setattr(cl, "_DEFAULTS_FILE", defaults)
        monkeypatch.setattr(cl, "_LOCAL_FILE", local)
        monkeypatch.setenv("MILL_MAX_FIX_ITERATIONS", "77")

        from robotsix_mill.config import load_settings

        s = load_settings()
        assert s.max_fix_iterations == 77  # env var wins, not YAML's 42

    def test_settings_init_applies_yaml_fallback(self, tmp_path, monkeypatch):
        """``Settings()`` applies YAML when field is at default;
        constructor kwargs override YAML."""
        import robotsix_mill.config.loader as cl

        local_dir = tmp_path / "config"
        local_dir.mkdir()
        defaults = local_dir / "mill.defaults.yaml"
        import shutil

        shutil.copy("config/mill.defaults.yaml", defaults)
        defaults.write_text(
            defaults.read_text().replace(
                "max_fix_iterations: 8", "max_fix_iterations: 42"
            )
        )
        monkeypatch.setattr(cl, "_DEFAULTS_FILE", defaults)
        monkeypatch.setattr(cl, "_LOCAL_FILE", cl.Path("/nonexistent/mill.local.yaml"))

        from robotsix_mill.config import Settings

        # No kwargs → YAML value is used (Field default is 8, YAML says 42)
        s1 = Settings(data_dir=str(tmp_path))
        assert s1.max_fix_iterations == 42

        # Constructor kwarg overrides YAML
        s2 = Settings(data_dir=str(tmp_path), max_fix_iterations=99)
        assert s2.max_fix_iterations == 99


class TestFlattenYamlConfig:
    """Unit tests for ``flatten_yaml_config``."""

    def test_flatten_basic_nesting(self):
        """Nested YAML dict is flattened to alias→value pairs."""
        from robotsix_mill.config.loader import flatten_yaml_config

        yaml_config = {
            "core": {
                "limits": {"max_fix_iterations": 8, "max_stuck_cycles": 5},
            },
            "gates": {"review_enabled": True},
        }
        result = flatten_yaml_config(yaml_config)
        assert result["max_fix_iterations"] == 8
        assert result["max_stuck_cycles"] == 5
        assert result["review_enabled"] is True

    def test_flatten_doc_classifier_diff_max_chars(self):
        """``core.limits.doc_classifier_diff_max_chars`` flattens to the
        ``doc_classifier_diff_max_chars`` Settings alias."""
        from robotsix_mill.config.loader import flatten_yaml_config

        yaml_config = {"core": {"limits": {"doc_classifier_diff_max_chars": 1234}}}
        result = flatten_yaml_config(yaml_config)
        assert result["doc_classifier_diff_max_chars"] == 1234

    def test_flatten_meta_periodic(self):
        """``core.meta_periodic`` / ``core.meta_interval_seconds`` flatten to
        their Settings aliases (enables the daily cross-repo meta pass)."""
        from robotsix_mill.config.loader import flatten_yaml_config

        out = flatten_yaml_config(
            {"core": {"meta_periodic": True, "meta_interval_seconds": 86400}}
        )
        assert out["meta_periodic"] is True
        assert out["meta_interval_seconds"] == 86400

    def test_flatten_enable_repo_creation(self):
        """``core.enable_repo_creation`` flattens to the
        ``enable_repo_creation`` Settings alias (gates the new-repo
        scaffold path; without the mapping the YAML key is silently
        ignored and repo creation stays off)."""
        from robotsix_mill.config.loader import flatten_yaml_config

        yaml_config = {"core": {"enable_repo_creation": True}}
        result = flatten_yaml_config(yaml_config)
        assert result["enable_repo_creation"] is True

    def test_flatten_unknown_paths_ignored(self):
        """YAML paths without a mapping are silently ignored."""
        from robotsix_mill.config.loader import flatten_yaml_config

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
        from robotsix_mill.config.loader import flatten_yaml_config

        yaml_config = {
            "core": {"limits": {"web_research_requests": 1}},
            "web": {"research_request_limit": 2},
        }
        result = flatten_yaml_config(yaml_config)
        # Both core.limits.web_research_requests and web.research_request_limit
        # map to web_research_request_limit; web.research_request_limit is
        # traversed second because 'web' > 'core' alphabetically
        assert result["web_research_request_limit"] == 2

    def test_flatten_coordinator_max_tool_calls(self):
        """``core.limits.coordinator_max_tool_calls`` flattens to the
        ``coordinator_max_tool_calls`` Settings field name."""
        from robotsix_mill.config.loader import flatten_yaml_config

        yaml_config = {"core": {"limits": {"coordinator_max_tool_calls": 123}}}
        result = flatten_yaml_config(yaml_config)
        assert result["coordinator_max_tool_calls"] == 123


class TestPeriodicPresenceModel:
    """Per-repo periodic enablement moved from repos.yaml ``periodic:`` flags
    to per-repo ``.robotsix-mill/periodic/<name>.yaml`` file presence. The
    RepoConfig ``*_periodic`` flags + ``_periodic_flags_from_yaml`` are gone."""

    def test_repoconfig_has_no_periodic_flags(self):
        from robotsix_mill.config import RepoConfig

        rc = RepoConfig(
            repo_id="r",
            board_id="b",
            langfuse_project_name="p",
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
        )
        for name in ("audit", "health", "module_curator", "langfuse_cleanup"):
            assert not hasattr(rc, f"{name}_periodic")

    def test_periodic_flags_helpers_removed(self):
        import robotsix_mill.config as cfg

        assert not hasattr(cfg, "_periodic_flags_from_yaml")
        assert not hasattr(cfg, "_PERIODIC_FLAG_NAMES")

    def test_repos_yaml_periodic_block_is_ignored(self, tmp_path):
        """A stray ``periodic:`` block in repos.yaml no longer errors — the
        loader never forwards it to RepoConfig."""
        from robotsix_mill.config import load_repos_config

        f = tmp_path / "repos.yaml"
        f.write_text(
            "repos:\n"
            "  r:\n"
            "    board_id: b\n"
            "    langfuse:\n"
            "      project_name: p\n"
            "      public_key: pk\n"
            "      secret_key: sk\n"
            "    periodic:\n"
            "      audit: { enabled: true }\n"
        )
        rr = load_repos_config(str(f))
        assert "r" in rr.repos and rr.repos["r"].repo_id == "r"


# ---------------------------------------------------------------------------
# 13. load_yaml_config → load_yaml_cascade delegation contract
# ---------------------------------------------------------------------------


class TestLoadYamlConfigDelegation:
    """Pin the delegation contract: ``load_yaml_config`` builds the
    ordered ``(Path, bool)`` layer list and forwards it to the shared
    ``load_yaml_cascade``, returning its result unchanged.

    The cascade name is patched on the *consumer* module
    (``robotsix_mill.config.loader``) — because ``config_loader`` did
    ``from robotsix_yaml_config import load_yaml_cascade``, patching the
    source module ``robotsix_yaml_config.load_yaml_cascade`` would not
    intercept the already-bound name.

    Expected layer lists are built from the live
    ``config_loader._DEFAULTS_FILE`` / ``_LOCAL_FILE`` module values
    (the autouse ``_no_dotenv`` fixture monkeypatches ``_LOCAL_FILE``)
    rather than hard-coded literals, so the assertions stay robust to
    fixture isolation. ``_DEFAULTS_FILE`` is left at the committed
    ``config/mill.defaults.yaml`` (which exists), so the missing-defaults
    pre-check never short-circuits the delegation path."""

    def test_default_layers(self):
        from unittest import mock
        import robotsix_mill.config.loader as cl

        sentinel = {"sentinel": "cascade-result"}
        with mock.patch.object(
            cl, "load_yaml_cascade", return_value=sentinel
        ) as mock_cascade:
            result = cl.load_yaml_config()

        # Result is passed through unchanged.
        assert result is sentinel
        mock_cascade.assert_called_once_with(
            [(cl._DEFAULTS_FILE, True), (cl._LOCAL_FILE, False)]
        )

    def test_skip_local_drops_local_layer(self):
        from unittest import mock
        import robotsix_mill.config.loader as cl

        sentinel = {"sentinel": "cascade-result"}
        with mock.patch.object(
            cl, "load_yaml_cascade", return_value=sentinel
        ) as mock_cascade:
            result = cl.load_yaml_config(skip_local=True)

        assert result is sentinel
        mock_cascade.assert_called_once_with([(cl._DEFAULTS_FILE, True)])

    def test_config_file_appends_production_layer(self):
        from pathlib import Path
        from unittest import mock
        import robotsix_mill.config.loader as cl

        sentinel = {"sentinel": "cascade-result"}
        with mock.patch.object(
            cl, "load_yaml_cascade", return_value=sentinel
        ) as mock_cascade:
            result = cl.load_yaml_config(config_file="some/prod.yaml")

        assert result is sentinel
        mock_cascade.assert_called_once_with(
            [
                (cl._DEFAULTS_FILE, True),
                (cl._LOCAL_FILE, False),
                (Path("some/prod.yaml"), True),
            ]
        )


class TestConfigErrorBackwardCompat:
    """``ConfigError`` subclasses the shared ``YamlConfigError`` base, so
    existing ``except ConfigError`` handlers (in ``cli.py``, ``config.py``,
    and ``stages/*.py``) keep catching loader failures after the
    delegation refactor — no production-code edits at the catch sites."""

    def test_config_error_subclasses_shared_base(self):
        from robotsix_yaml_config import YamlConfigError
        from robotsix_mill.config.loader import ConfigError

        assert issubclass(ConfigError, YamlConfigError)

    def test_except_config_error_catches_loader_error(self, monkeypatch):
        from robotsix_mill.config.loader import ConfigError, load_yaml_config
        import robotsix_mill.config.loader as cl

        # Force the missing-defaults path so the loader raises.
        monkeypatch.setattr(cl, "_DEFAULTS_FILE", cl.Path("/nonexistent/defaults.yaml"))
        caught = False
        try:
            load_yaml_config()
        except ConfigError:
            caught = True
        assert caught


# ---------------------------------------------------------------------------
#  Component-agent settings invariant
# ---------------------------------------------------------------------------


class TestComponentAgentSettings:
    def test_enabled_without_host_raises(self):
        with pytest.raises(ValueError, match="component_agent_broker_host"):
            Settings(
                component_agent_enabled=True,
                component_agent_broker_token="token",
                component_agent_broker_host="",
            )

    def test_enabled_without_token_raises(self):
        with pytest.raises(ValueError, match="component_agent_broker_token"):
            Settings(
                component_agent_enabled=True,
                component_agent_broker_host="broker.example.com",
                component_agent_broker_token="",
            )

    def test_enabled_with_host_and_token_ok(self):
        s = Settings(
            component_agent_enabled=True,
            component_agent_broker_host="broker.example.com",
            component_agent_broker_token="secret",
        )
        assert s.component_agent_enabled is True

    def test_disabled_without_host_ok(self):
        s = Settings(
            component_agent_enabled=False,
            component_agent_broker_host="",
            component_agent_broker_token="",
        )
        assert s.component_agent_enabled is False


# -- resolve_child_board_id tests ------------------------------------------


class TestResolveChildBoardId:
    def test_known_repo_returns_board_id(self):
        from robotsix_mill.config import RepoConfig, ReposRegistry
        from robotsix_mill.config.repos import resolve_child_board_id

        repos = ReposRegistry(
            repos={
                "robotsix-mill": RepoConfig(
                    repo_id="robotsix-mill",
                    board_id="board-mill",
                    langfuse_project_name="mill",
                    langfuse_public_key="pk",
                    langfuse_secret_key="sk",
                ),
                "robotsix-auto-mail": RepoConfig(
                    repo_id="robotsix-auto-mail",
                    board_id="board-mail",
                    langfuse_project_name="mail",
                    langfuse_public_key="pk",
                    langfuse_secret_key="sk",
                ),
            }
        )
        assert (
            resolve_child_board_id("robotsix-auto-mail", "board-mill", "epic-1", repos)
            == "board-mail"
        )

    def test_unknown_repo_falls_back_to_epic_board(self, caplog):
        from robotsix_mill.config import RepoConfig, ReposRegistry
        from robotsix_mill.config.repos import resolve_child_board_id

        repos = ReposRegistry(
            repos={
                "robotsix-mill": RepoConfig(
                    repo_id="robotsix-mill",
                    board_id="board-mill",
                    langfuse_project_name="mill",
                    langfuse_public_key="pk",
                    langfuse_secret_key="sk",
                ),
            }
        )
        result = resolve_child_board_id(
            "nonexistent-repo", "board-mill", "epic-1", repos
        )
        assert result == "board-mill"
        # Must emit a warning naming the epic and the bad repo.
        assert "epic-1" in caplog.text
        assert "nonexistent-repo" in caplog.text
        assert "board-mill" in caplog.text

    def test_empty_repo_id_returns_epic_board(self):
        from robotsix_mill.config import RepoConfig, ReposRegistry
        from robotsix_mill.config.repos import resolve_child_board_id

        repos = ReposRegistry(
            repos={
                "robotsix-mill": RepoConfig(
                    repo_id="robotsix-mill",
                    board_id="board-mill",
                    langfuse_project_name="mill",
                    langfuse_public_key="pk",
                    langfuse_secret_key="sk",
                ),
            }
        )
        assert resolve_child_board_id("", "board-mill", "epic-1", repos) == "board-mill"
        assert (
            resolve_child_board_id("   ", "board-mill", "epic-1", repos) == "board-mill"
        )

    def test_no_repos_returns_epic_board(self):
        from robotsix_mill.config.repos import resolve_child_board_id

        result = resolve_child_board_id("some-repo", "board-mill", "epic-1", None)
        assert result == "board-mill"
