"""YAML configuration loader and deep-merge for robotsix-mill.

Loads the layered YAML config files (defaults → local → production) and
deep-merges them into a single dict that pydantic-settings can use as
field defaults.  Also loads ``config/secrets.yaml`` into a flat dict for
the ``Secrets`` model.

Design: `docs/rfc-config-v2.md` §6 (Load order and precedence).
"""

from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path

import yaml


# ---------------------------------------------------------------------------
#  Exception
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    """Raised for config-loading failures — missing required files,
    YAML parse errors, etc."""

    pass


# ---------------------------------------------------------------------------
#  Deep merge
# ---------------------------------------------------------------------------


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge *overlay* into *base* (mutates *base*).

    Scalar values in *overlay* overwrite those in *base*.  Nested dicts
    are merged recursively; lists are replaced wholesale.
    """
    for key, value in overlay.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = deepcopy(value)
    return base


# ---------------------------------------------------------------------------
#  YAML loading
# ---------------------------------------------------------------------------

_YAML_DIR = Path("config")
_DEFAULTS_FILE = _YAML_DIR / "mill.defaults.yaml"
_LOCAL_FILE = _YAML_DIR / "mill.local.yaml"


def _read_yaml_file(path: Path) -> dict:
    """Read and parse a single YAML file, returning a dict.

    Returns an empty dict for non-existent optional files.
    Raises ``ConfigError`` on parse failures.
    """
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ConfigError(
            f"YAML parse error in {path}: {exc}"
        ) from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(
            f"Expected a mapping at top level of {path}, got {type(data).__name__}"
        )
    return data


def load_yaml_config(
    config_file: str | None = None, skip_local: bool = False
) -> dict:
    """Load and deep-merge YAML config files in RFC §6 precedence order.

    1. ``config/mill.defaults.yaml`` (always, committed)
    2. ``config/mill.local.yaml`` if present (gitignored, optional)
       — skipped when *skip_local* is ``True``
    3. ``config/mill.production.yaml`` if *config_file* (or
       ``MILL_CONFIG_FILE`` env var) points to it.  An explicit empty
       string ``""`` means "no production file" (used by the test suite).

    Returns a nested dict mirroring the YAML structure
    (e.g. ``{"core": {"models": {"coordinator": "deepseek/..."}, ...}}``).

    Raises ``ConfigError`` if ``config/mill.defaults.yaml`` is missing
    or any file contains malformed YAML.
    """
    if not _DEFAULTS_FILE.exists():
        raise ConfigError(
            f"Required config file not found: {_DEFAULTS_FILE}. "
            "This file is committed to the repo and must always be present."
        )

    merged: dict = {}

    # Layer 1: committed defaults (required)
    defaults = _read_yaml_file(_DEFAULTS_FILE)
    _deep_merge(merged, defaults)

    # Layer 2: per-developer local overrides (optional)
    if not skip_local:
        local = _read_yaml_file(_LOCAL_FILE)
        if local:
            _deep_merge(merged, local)

    # Layer 3: production overrides (optional)
    prod_path: str = ""
    if config_file is not None:
        prod_path = config_file
    else:
        prod_path = os.environ.get("MILL_CONFIG_FILE", "")
    if prod_path:
        prod = _read_yaml_file(Path(prod_path))
        if prod:
            _deep_merge(merged, prod)

    return merged


def load_secrets_yaml(secrets_file: str | None = None) -> dict:
    """Read ``config/secrets.yaml`` (or ``MILL_SECRETS_FILE`` if set).

    Returns a flat dict keyed by the YAML field names
    (e.g. ``{"openrouter_api_key": "sk-...", ...}``).

    Missing file → returns an empty dict (not an error — secrets are
    optional for CI / mocked tests).

    Malformed YAML → raises ``ConfigError`` with the file path and
    parse error details.
    """
    # Determine the path: explicit arg > env var > default.
    # An explicit empty string means "no file" (used by the test suite).
    if secrets_file is not None:
        path_str = secrets_file
    else:
        path_str = os.environ.get("MILL_SECRETS_FILE")

    if path_str is None:
        path_str = str(_YAML_DIR / "secrets.yaml")
    elif path_str == "":
        return {}
    path = Path(path_str)

    if not path.exists():
        return {}

    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ConfigError(
            f"YAML parse error in {path}: {exc}"
        ) from exc

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(
            f"Expected a mapping at top level of {path}, got {type(data).__name__}"
        )
    return dict(data)


# ---------------------------------------------------------------------------
#  YAML dotted-path → env-var alias mapping
# ---------------------------------------------------------------------------

# Maps ``"group.subgroup.field"`` YAML paths to the env-var alias
# (the ``Field(alias=...)`` value) on the ``Settings`` model.  Built
# from the RFC §4.2 mapping table and kept in sync with
# ``config/mill.defaults.yaml``.
#
# We use env-var aliases (not Python field names) because
# ``Settings(extra="ignore")`` silently drops kwargs keyed by the
# Python field name — it only recognises alias names.
_YAML_PATH_TO_ALIAS: dict[str, str] = {
    # -- core.models --
    "core.models.coordinator": "MILL_MODEL",
    "core.models.explore": "MILL_EXPLORE_MODEL",
    "core.models.test": "MILL_TEST_MODEL",
    "core.models.refine": "MILL_REFINE_MODEL",
    "core.models.answer": "MILL_ANSWER_MODEL",
    "core.models.retrospect": "MILL_RETROSPECT_MODEL",
    "core.models.audit": "MILL_AUDIT_MODEL",
    "core.models.dedup": "MILL_DEDUP_MODEL",
    "core.models.web_research": "MILL_WEB_RESEARCH_MODEL",
    "core.models.review": "MILL_REVIEW_MODEL",
    "core.models.trace_inspector": "MILL_TRACE_INSPECTOR_MODEL",
    "core.models.test_gap": "MILL_TEST_GAP_MODEL",
    "core.models.agent_check": "MILL_AGENT_CHECK_MODEL",
    "core.models.health": "MILL_HEALTH_MODEL",
    "core.models.survey": "MILL_SURVEY_MODEL",
    "core.models.doc": "MILL_DOC_MODEL",
    "core.models.triage": "MILL_TRIAGE_MODEL",
    "core.models.auto_approve": "MILL_AUTO_APPROVE_MODEL",
    "core.models.rate_limit_fallback": "MILL_RATE_LIMIT_FALLBACK_MODEL",
    # -- core.limits --
    "core.limits.coordinator_requests": "MILL_COORDINATOR_REQUEST_LIMIT",
    "core.limits.test_requests": "MILL_TEST_REQUEST_LIMIT",
    "core.limits.explore_requests": "MILL_EXPLORE_REQUEST_LIMIT",
    "core.limits.dedup_requests": "MILL_DEDUP_REQUEST_LIMIT",
    "core.limits.web_research_requests": "MILL_WEB_RESEARCH_REQUEST_LIMIT",
    "core.limits.max_concurrency": "MILL_MAX_CONCURRENCY",
    "core.limits.max_fix_iterations": "MILL_MAX_FIX_ITERATIONS",
    "core.limits.max_stuck_cycles": "MILL_MAX_STUCK_CYCLES",
    "core.limits.max_spend_usd_per_ticket": "MILL_MAX_SPEND_USD_PER_TICKET",
    "core.limits.model_request_timeout": "MILL_MODEL_REQUEST_TIMEOUT",
    "core.limits.transient_retries": "MILL_TRANSIENT_RETRIES",
    "core.limits.transient_backoff_base": "MILL_TRANSIENT_BACKOFF_BASE",
    "core.limits.transient_backoff_cap": "MILL_TRANSIENT_BACKOFF_CAP",
    "core.limits.rate_limit_backoff_base": "MILL_RATE_LIMIT_BACKOFF_BASE",
    "core.limits.rate_limit_backoff_cap": "MILL_RATE_LIMIT_BACKOFF_CAP",
    "core.limits.rate_limit_fallback_retries": "MILL_RATE_LIMIT_FALLBACK_RETRIES",
    # -- core.memory --
    "core.memory.max_memory_chars": "MILL_MAX_MEMORY_CHARS",
    "core.memory.reference_files_max_count": "MILL_REFERENCE_FILES_MAX_COUNT",
    "core.memory.reference_files_max_total_lines": "MILL_REFERENCE_FILES_MAX_TOTAL_LINES",
    "core.memory.dedup_lookback_days": "MILL_DEDUP_LOOKBACK_DAYS",
    "core.memory.dedup_lookback_commits": "MILL_DEDUP_LOOKBACK_COMMITS",
    # -- forge --
    "forge.kind": "FORGE_KIND",
    "forge.remote_url": "FORGE_REMOTE_URL",
    "forge.target_branch": "FORGE_TARGET_BRANCH",
    "forge.auth_mode": "FORGE_AUTH",
    "forge.github_api_url": "MILL_GITHUB_API_URL",
    "forge.gitlab_api_url": "MILL_GITLAB_API_URL",
    "forge.github_app_private_key_path": "GITHUB_APP_PRIVATE_KEY_PATH",
    # -- sandbox --
    "sandbox.image": "MILL_SANDBOX_IMAGE",
    "sandbox.memory": "MILL_SANDBOX_MEMORY",
    "sandbox.pids_limit": "MILL_SANDBOX_PIDS_LIMIT",
    "sandbox.readonly": "MILL_SANDBOX_READONLY",
    "sandbox.data_volume": "MILL_DATA_VOLUME",
    "sandbox.data_mount": "MILL_SANDBOX_DATA_MOUNT",
    "sandbox.command_timeout": "MILL_COMMAND_TIMEOUT",
    "sandbox.test_command": "MILL_TEST_COMMAND",
    "sandbox.skills_dir": "MILL_SKILLS_DIR",
    # -- web --
    "web.search_enabled": "MILL_WEB_SEARCH",
    "web.research_model": "MILL_WEB_RESEARCH_MODEL",
    "web.research_request_limit": "MILL_WEB_RESEARCH_REQUEST_LIMIT",
    "web.fetch_image": "MILL_FETCH_IMAGE",
    "web.fetch_max_bytes": "MILL_WEB_FETCH_MAX_BYTES",
    "web.fetch_timeout": "MILL_WEB_FETCH_TIMEOUT",
    # -- gates --
    "gates.require_approval": "MILL_REQUIRE_APPROVAL",
    "gates.auto_approve_enabled": "MILL_AUTO_APPROVE_ENABLED",
    "gates.auto_approve_model": "MILL_AUTO_APPROVE_MODEL",
    "gates.review_enabled": "MILL_REVIEW_ENABLED",
    "gates.review_model": "MILL_REVIEW_MODEL",
    "gates.review_max_rounds": "MILL_REVIEW_MAX_ROUNDS",
    "gates.auto_merge_enabled": "MILL_AUTO_MERGE_ENABLED",
    "gates.refine_triage_enabled": "MILL_REFINE_TRIAGE_ENABLED",
    "gates.spec_review_enabled": "MILL_SPEC_REVIEW_ENABLED",
    # -- pipeline --
    "pipeline.branch_prefix": "MILL_BRANCH_PREFIX",
    "pipeline.merge_poll_seconds": "MILL_MERGE_POLL_SECONDS",
    "pipeline.rebase_max_attempts": "MILL_REBASE_MAX_ATTEMPTS",
    "pipeline.ci_fix_max_attempts": "MILL_CI_FIX_MAX_ATTEMPTS",
    "pipeline.retrospect_spawn_drafts": "MILL_RETROSPECT_SPAWN_DRAFTS",
    "pipeline.retrospect_deep_analysis_frequency": "MILL_RETROSPECT_DEEP_ANALYSIS_FREQUENCY",
    "pipeline.prune_clone_on_close": "MILL_PRUNE_CLONE_ON_CLOSE",
    "pipeline.max_archived_tickets": "MILL_MAX_ARCHIVED_TICKETS",
    "pipeline.retrospect_memory_path": "MILL_RETROSPECT_MEMORY_PATH",
    "pipeline.trace_inspector_memory_path": "MILL_TRACE_INSPECTOR_MEMORY_PATH",
    "pipeline.implement_memory_path": "MILL_IMPLEMENT_MEMORY_PATH",
    "pipeline.refine_memory_path": "MILL_REFINE_MEMORY_PATH",
    "pipeline.ci_fix_memory_path": "MILL_CI_FIX_MEMORY_PATH",
    "pipeline.rebase_memory_path": "MILL_REBASE_MEMORY_PATH",
    # -- periodic.audit --
    "periodic.audit.model": "MILL_AUDIT_MODEL",
    "periodic.audit.enabled": "MILL_AUDIT_PERIODIC",
    "periodic.audit.interval_seconds": "MILL_AUDIT_INTERVAL_SECONDS",
    "periodic.audit.memory_path": "MILL_AUDIT_MEMORY_PATH",
    # -- periodic.trace_health --
    "periodic.trace_health.enabled": "MILL_TRACE_HEALTH_PERIODIC",
    "periodic.trace_health.interval_seconds": "MILL_TRACE_HEALTH_INTERVAL_SECONDS",
    # -- periodic.health --
    "periodic.health.model": "MILL_HEALTH_MODEL",
    "periodic.health.enabled": "MILL_HEALTH_PERIODIC",
    "periodic.health.interval_seconds": "MILL_HEALTH_INTERVAL_SECONDS",
    "periodic.health.memory_path": "MILL_HEALTH_MEMORY_PATH",
    # -- periodic.test_gap --
    "periodic.test_gap.model": "MILL_TEST_GAP_MODEL",
    "periodic.test_gap.enabled": "MILL_TEST_GAP_PERIODIC",
    "periodic.test_gap.interval_seconds": "MILL_TEST_GAP_INTERVAL_SECONDS",
    "periodic.test_gap.memory_path": "MILL_TEST_GAP_MEMORY_PATH",
    # -- periodic.agent_check --
    "periodic.agent_check.model": "MILL_AGENT_CHECK_MODEL",
    "periodic.agent_check.enabled": "MILL_AGENT_CHECK_PERIODIC",
    "periodic.agent_check.interval_seconds": "MILL_AGENT_CHECK_INTERVAL_SECONDS",
    "periodic.agent_check.memory_path": "MILL_AGENT_CHECK_MEMORY_PATH",
    # -- periodic.survey --
    "periodic.survey.model": "MILL_SURVEY_MODEL",
    "periodic.survey.enabled": "MILL_SURVEY_PERIODIC",
    "periodic.survey.interval_seconds": "MILL_SURVEY_INTERVAL_SECONDS",
    "periodic.survey.memory_path": "MILL_SURVEY_MEMORY_PATH",
    # -- periodic.ci_monitor --
    "periodic.ci_monitor.enabled": "MILL_CI_MONITOR_PERIODIC",
    "periodic.ci_monitor.interval_seconds": "MILL_CI_MONITOR_INTERVAL_SECONDS",
    "periodic.ci_monitor.log_max_bytes": "MILL_CI_LOG_MAX_BYTES",
    # -- service --
    "service.data_dir": "MILL_DATA_DIR",
    "service.api_host": "MILL_API_HOST",
    "service.api_port": "MILL_API_PORT",
    "service.api_url": "MILL_API_URL",
}


def flatten_yaml_config(yaml_config: dict) -> dict[str, object]:
    """Flatten a nested YAML config dict into kwargs for ``Settings()``.

    Walks the nested dict, maps each ``dotted.path`` key through
    ``_YAML_PATH_TO_ALIAS``, and returns a flat dict of env-var alias
    names → values.  Only values that have a mapping are included —
    unknown paths are silently ignored.

    When the same env-var alias is reachable through multiple YAML paths
    (e.g. ``core.models.web_research`` and ``web.research_model`` both
    map to ``MILL_WEB_RESEARCH_MODEL``), the value from the *last* path
    traversed wins (dict insertion order).
    """
    result: dict[str, object] = {}

    def _walk(d: dict, prefix: str = "") -> None:
        for key, value in d.items():
            full_key = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                _walk(value, full_key)
            else:
                alias = _YAML_PATH_TO_ALIAS.get(full_key)
                if alias is not None:
                    result[alias] = value

    _walk(yaml_config)
    return result
