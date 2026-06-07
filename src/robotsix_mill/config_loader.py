"""YAML configuration loader and deep-merge for robotsix-mill.

Loads the layered YAML config files (defaults → local → production) and
deep-merges them into a single dict that pydantic-settings can use as
field defaults.  Also loads ``config/secrets.yaml`` into a flat dict for
the ``Secrets`` model.

Design: `docs/rfc-config-v2.md` §6 (Load order and precedence).
"""

from __future__ import annotations

import os
from pathlib import Path

from robotsix_yaml_config import (
    YamlConfigError,
    flatten_config,
    load_yaml_cascade,
    read_yaml_file,
)


# ---------------------------------------------------------------------------
#  Exception
# ---------------------------------------------------------------------------


class ConfigError(YamlConfigError):
    """Raised for config-loading failures — missing required files,
    YAML parse errors, etc. Subclasses the shared base so existing
    ``except ConfigError`` handlers keep working."""

    pass


# ---------------------------------------------------------------------------
#  YAML loading
# ---------------------------------------------------------------------------

_YAML_DIR = Path("config")
_DEFAULTS_FILE = _YAML_DIR / "mill.defaults.yaml"
_LOCAL_FILE = _YAML_DIR / "mill.local.yaml"


def load_yaml_config(config_file: str | None = None, skip_local: bool = False) -> dict:
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

    # Resolve the production overlay path (explicit arg > env var). An
    # explicit empty string means "no production file".
    prod_path: str = ""
    if config_file is not None:
        prod_path = config_file
    else:
        prod_path = os.environ.get("MILL_CONFIG_FILE", "")

    # Build the ordered layer list at call time (reading the current
    # module-level _DEFAULTS_FILE / _LOCAL_FILE values so test
    # monkeypatching applies). Precedence is later-overrides-earlier:
    # defaults → local → production. Each layer is a ``(path, required)``
    # tuple consumed by ``load_yaml_cascade``; only the local overlay is
    # optional.
    layers: list[tuple[Path, bool]] = [(_DEFAULTS_FILE, True)]
    if not skip_local:
        layers.append((_LOCAL_FILE, False))
    if prod_path:
        layers.append((Path(prod_path), True))

    try:
        return load_yaml_cascade(layers)
    except YamlConfigError as exc:
        raise ConfigError(str(exc)) from exc


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
        data = read_yaml_file(path)
    except YamlConfigError as exc:
        raise ConfigError(str(exc)) from exc
    return dict(data)


def load_repos_yaml(file_path: str | None = None) -> dict:
    """Read ``config/repos.yaml`` (or ``MILL_REPOS_FILE`` if set).

    Returns a dict keyed by repo ID with nested ``board_id`` and
    ``langfuse`` sub-dicts
    (e.g. ``{"my-repo": {"board_id": "...", "langfuse": {...}}, ...}``).

    Missing file → returns an empty dict (not an error — repos config is
    optional).

    Malformed YAML → raises ``ConfigError`` with the file path and
    parse error details.
    """
    # Determine the path: explicit arg > env var > default.
    # An explicit empty string means "no file" (used by the test suite).
    if file_path is not None:
        path_str = file_path
    else:
        path_str = os.environ.get("MILL_REPOS_FILE")

    if path_str is None:
        path_str = str(_YAML_DIR / "repos.yaml")
    elif path_str == "":
        return {}
    path = Path(path_str)

    if not path.exists():
        return {}

    try:
        data = read_yaml_file(path)
    except YamlConfigError as exc:
        raise ConfigError(str(exc)) from exc
    # Extract the ``repos`` key if present (standard format).
    if "repos" in data:
        repos_data = data["repos"]
        if not isinstance(repos_data, dict):
            raise ConfigError(
                f"Expected a mapping under 'repos' key in {path}, "
                f"got {type(repos_data).__name__}"
            )
        return dict(repos_data)
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
    "core.models.coordinator": "model",
    "core.models.explore": "explore_model",
    "core.models.test": "test_model",
    "core.models.refine": "refine_model",
    "core.models.answer": "answer_model",
    "core.models.retrospect": "retrospect_model",
    "core.models.audit": "audit_model",
    "core.models.dedup": "dedup_model",
    "core.models.web_research": "web_research_model",
    "core.models.review": "review_model",
    "core.models.trace_inspector": "trace_inspector_model",
    "core.models.test_gap": "test_gap_model",
    "core.models.agent_check": "agent_check_model",
    "core.models.health": "health_model",
    "core.models.survey": "survey_model",
    "core.models.doc": "doc_model",
    "core.models.doc_classifier": "doc_classifier_model",
    "core.models.triage": "triage_model",
    "core.models.auto_approve": "auto_approve_model",
    "core.models.scope_triage": "scope_triage_model",
    "core.models.rate_limit_fallback": "rate_limit_fallback_model",
    # -- core: LLM backend toggle (DeepSeek ↔ Claude SDK) --
    "core.llm_backend": "llm_backend",
    "core.claude_sdk_agents": "claude_sdk_agents",
    "core.enable_repo_creation": "enable_repo_creation",
    # Cross-repo meta-agent pass (surveys all repos for extraction/alignment).
    "core.meta_periodic": "meta_periodic",
    "core.meta_interval_seconds": "meta_interval_seconds",
    # Cross-repo cost-analyst pass (aggregate cost-reduction proposals).
    "core.cost_analyst_periodic": "cost_analyst_periodic",
    "core.cost_analyst_interval_seconds": "cost_analyst_interval_seconds",
    "core.cost_analyst_window_hours": "cost_analyst_window_hours",
    "core.cost_analyst_top_stages": "cost_analyst_top_stages",
    "core.cost_analyst_target_repo_id": "cost_analyst_target_repo_id",
    # -- core.limits --
    "core.limits.coordinator_requests": "coordinator_request_limit",
    "core.limits.test_requests": "test_request_limit",
    "core.limits.consult_requests": "consult_request_limit",
    "core.limits.explore_requests": "explore_request_limit",
    "core.limits.explore_max_tokens": "explore_max_tokens",
    "core.limits.refine_requests": "refine_request_limit",
    "core.limits.dedup_requests": "dedup_request_limit",
    "core.limits.dedup_max_candidates": "dedup_max_candidates",
    "core.limits.doc_classifier_requests": "doc_classifier_request_limit",
    "core.limits.doc_classifier_diff_max_chars": "doc_classifier_diff_max_chars",
    "core.limits.review_requests": "review_request_limit",
    "core.limits.web_research_requests": "web_research_request_limit",
    "core.limits.scope_triage_requests": "scope_triage_request_limit",
    "core.limits.max_fix_iterations": "max_fix_iterations",
    "core.limits.max_stuck_cycles": "max_stuck_cycles",
    "core.limits.max_spend_usd_per_ticket": "max_spend_usd_per_ticket",
    "core.limits.stage_timeout_seconds": "stage_timeout_seconds",
    "core.limits.stage_timeout_overrides": "stage_timeout_overrides",
    "core.limits.model_request_timeout": "model_request_timeout",
    "core.limits.transient_retries": "transient_retries",
    "core.limits.transient_backoff_base": "transient_backoff_base",
    "core.limits.transient_backoff_cap": "transient_backoff_cap",
    "core.limits.rate_limit_backoff_base": "rate_limit_backoff_base",
    "core.limits.rate_limit_backoff_cap": "rate_limit_backoff_cap",
    "core.limits.rate_limit_fallback_retries": "rate_limit_fallback_retries",
    # -- core.memory --
    "core.memory.max_memory_chars": "max_memory_chars",
    "core.memory.retrospect_log_max_chars": "retrospect_log_max_chars",
    "core.memory.reference_files_max_count": "reference_files_max_count",
    "core.memory.reference_files_max_total_lines": "reference_files_max_total_lines",
    "core.memory.dedup_lookback_days": "dedup_lookback_days",
    # -- forge --
    "forge.kind": "FORGE_KIND",
    "forge.remote_url": "FORGE_REMOTE_URL",
    "forge.target_branch": "FORGE_TARGET_BRANCH",
    "forge.auth_mode": "FORGE_AUTH",
    "forge.github_api_url": "github_api_url",
    "forge.gitlab_api_url": "gitlab_api_url",
    "forge.github_app_private_key_path": "GITHUB_APP_PRIVATE_KEY_PATH",
    # -- sandbox --
    "sandbox.image": "sandbox_image",
    "sandbox.memory": "sandbox_memory",
    "sandbox.pids_limit": "sandbox_pids_limit",
    "sandbox.readonly": "sandbox_readonly",
    "sandbox.data_volume": "data_volume",
    "sandbox.data_mount": "sandbox_data_mount",
    "sandbox.command_timeout": "command_timeout",
    "sandbox.test_command": "test_command",
    "sandbox.skills_dir": "skills_dir",
    # -- web --
    "web.search_enabled": "web_search",
    "web.research_model": "web_research_model",
    "web.research_request_limit": "web_research_request_limit",
    "web.fetch_image": "fetch_image",
    "web.fetch_max_bytes": "web_fetch_max_bytes",
    "web.fetch_timeout": "web_fetch_timeout",
    # YAML-only fields (no env-var alias); the mapping value is the
    # Settings field name verbatim, not a ``MILL_*`` env var. New
    # settings ship through this path so the env-var surface stops
    # growing while existing settings can migrate over time.
    "web.fetch_max_text_bytes": "web_fetch_max_text_bytes",
    "web.fetch_raw": "web_fetch_raw",
    "web.fetch_max_calls": "web_fetch_max_calls",
    "web.fetch_max_total_bytes": "web_fetch_max_total_bytes",
    "core.lint_on_edit": "lint_on_edit",
    # -- gates --
    "gates.require_approval": "require_approval",
    "gates.auto_approve_enabled": "auto_approve_enabled",
    "gates.auto_approve_model": "auto_approve_model",
    "gates.review_enabled": "review_enabled",
    "gates.review_model": "review_model",
    "gates.review_max_rounds": "review_max_rounds",
    "gates.review_feedback_enabled": "review_feedback_enabled",
    "gates.review_revision_model": "review_revision_model",
    "gates.auto_merge_enabled": "auto_merge_enabled",
    "gates.refine_triage_enabled": "refine_triage_enabled",
    "gates.spec_review_enabled": "spec_review_enabled",
    "gates.scope_triage_enabled": "scope_triage_enabled",
    "gates.pr_summary_enabled": "pr_summary_enabled",
    "gates.pr_summary_model": "pr_summary_model",
    # -- ci --
    "ci.max_auto_retries": "ci_max_auto_retries",
    # -- pipeline --
    # Post-MILL_*-alias-purge these map directly to Settings field
    # names (no env-var alias on the Field). Keeping a MILL_* value
    # here would make YamlSettingsSource look up an alias that no
    # longer exists on the model, silently dropping every YAML
    # override in this block.
    "pipeline.branch_prefix": "branch_prefix",
    "pipeline.merge_poll_seconds": "merge_poll_seconds",
    "pipeline.rebase_max_attempts": "rebase_max_attempts",
    "pipeline.ci_fix_max_attempts": "ci_fix_max_attempts",
    "pipeline.retrospect_spawn_drafts": "retrospect_spawn_drafts",
    "pipeline.retrospect_spawn_agented_proposals": "retrospect_spawn_agented_proposals",
    "pipeline.prune_clone_on_close": "prune_clone_on_close",
    "pipeline.max_archived_tickets": "max_archived_tickets",
    "pipeline.retrospect_memory_path": "retrospect_memory_path",
    "pipeline.trace_inspector_memory_path": "trace_inspector_memory_path",
    "pipeline.trace_review_target_repo_id": "trace_review_target_repo_id",
    "pipeline.implement_memory_path": "implement_memory_path",
    "pipeline.refine_memory_path": "refine_memory_path",
    "pipeline.doc_memory_path": "doc_memory_path",
    "pipeline.ci_fix_memory_path": "ci_fix_memory_path",
    "pipeline.rebase_memory_path": "rebase_memory_path",
    "pipeline.ci_patterns_path": "ci_patterns_path",
    "pipeline.review_revision_memory_path": "review_revision_memory_path",
    # -- periodic.audit --
    "periodic.audit.model": "audit_model",
    "periodic.audit.enabled": "audit_periodic",
    "periodic.audit.interval_seconds": "audit_interval_seconds",
    "periodic.audit.memory_path": "audit_memory_path",
    # -- periodic.trace_health --
    "periodic.trace_health.enabled": "trace_health_periodic",
    "periodic.trace_health.interval_seconds": "trace_health_interval_seconds",
    # -- periodic.health --
    "periodic.health.model": "health_model",
    "periodic.health.enabled": "health_periodic",
    "periodic.health.interval_seconds": "health_interval_seconds",
    "periodic.health.memory_path": "health_memory_path",
    # -- periodic.test_gap --
    "periodic.test_gap.model": "test_gap_model",
    "periodic.test_gap.enabled": "test_gap_periodic",
    "periodic.test_gap.interval_seconds": "test_gap_interval_seconds",
    "periodic.test_gap.memory_path": "test_gap_memory_path",
    # -- periodic.agent_check --
    "periodic.agent_check.model": "agent_check_model",
    "periodic.agent_check.enabled": "agent_check_periodic",
    "periodic.agent_check.interval_seconds": "agent_check_interval_seconds",
    "periodic.agent_check.memory_path": "agent_check_memory_path",
    # -- periodic.bc_check --
    "periodic.bc_check.model": "bc_check_model",
    "periodic.bc_check.enabled": "bc_check_periodic",
    "periodic.bc_check.interval_seconds": "bc_check_interval_seconds",
    "periodic.bc_check.memory_path": "bc_check_memory_path",
    # -- periodic.completeness_check --
    "periodic.completeness_check.model": "completeness_check_model",
    "periodic.completeness_check.enabled": "completeness_check_periodic",
    "periodic.completeness_check.interval_seconds": "completeness_check_interval_seconds",
    "periodic.completeness_check.memory_path": "completeness_check_memory_path",
    # -- periodic.copy_paste --
    "periodic.copy_paste.model": "copy_paste_model",
    "periodic.copy_paste.enabled": "copy_paste_periodic",
    "periodic.copy_paste.interval_seconds": "copy_paste_interval_seconds",
    "periodic.copy_paste.memory_path": "copy_paste_memory_path",
    # -- periodic.survey --
    "periodic.survey.model": "survey_model",
    "periodic.survey.enabled": "survey_periodic",
    "periodic.survey.interval_seconds": "survey_interval_seconds",
    "periodic.survey.memory_path": "survey_memory_path",
    # -- periodic.cost_reconciliation --
    "periodic.cost_reconciliation.enabled": "cost_reconciliation_periodic",
    "periodic.cost_reconciliation.interval_seconds": "cost_reconciliation_interval_seconds",
    "periodic.cost_reconciliation.memory_path": "cost_reconciliation_memory_path",
    # -- periodic.config_sync --
    "periodic.config_sync.model": "config_sync_model",
    "periodic.config_sync.enabled": "config_sync_periodic",
    "periodic.config_sync.interval_seconds": "config_sync_interval_seconds",
    "periodic.config_sync.memory_path": "config_sync_memory_path",
    # -- periodic.ci_monitor (global cap only — enabled/interval are per-repo) --
    "periodic.ci_monitor.log_max_bytes": "ci_log_max_bytes",
    # -- periodic.timeout_escalation --
    "periodic.timeout_escalation.enabled": "timeout_escalation_periodic",
    "periodic.timeout_escalation.interval_seconds": "timeout_escalation_interval_seconds",
    "periodic.timeout_escalation.threshold_seconds": "timeout_escalation_threshold_seconds",
    # -- periodic.langfuse_cleanup --
    "periodic.langfuse_cleanup.enabled": "langfuse_cleanup_periodic",
    "periodic.langfuse_cleanup.interval_seconds": "langfuse_cleanup_interval_seconds",
    "periodic.langfuse_cleanup.max_traces": "langfuse_cleanup_max_traces",
    # -- periodic.module_curator --
    "periodic.module_curator.model": "module_curator_model",
    "periodic.module_curator.enabled": "module_curator_periodic",
    "periodic.module_curator.interval_seconds": "module_curator_interval_seconds",
    "periodic.module_curator.memory_path": "module_curator_memory_path",
    # -- periodic.board_cleanup --
    "periodic.board_cleanup.model": "board_cleanup_model",
    "periodic.board_cleanup.enabled": "board_cleanup_periodic",
    "periodic.board_cleanup.interval_seconds": "board_cleanup_interval_seconds",
    "periodic.board_cleanup.memory_path": "board_cleanup_memory_path",
    # -- service --
    "service.data_dir": "data_dir",
    "service.api_host": "api_host",
    "service.api_port": "api_port",
    "service.api_url": "api_url",
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
    return flatten_config(yaml_config, alias_map=_YAML_PATH_TO_ALIAS)
