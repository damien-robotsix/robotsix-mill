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


def _load_repos_document(file_path: str | None = None) -> dict:
    """Read and parse the full ``config/repos.yaml`` document.

    Shared by :func:`load_repos_yaml` (which extracts the ``repos``
    mapping). Returns the raw top-level mapping, or
    ``{}`` for a missing file / explicit ``""`` (no-file) path.
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
    return data if isinstance(data, dict) else {}


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
    data = _load_repos_document(file_path)
    if not data:
        return {}
    # Extract the ``repos`` key if present (standard format).
    if "repos" in data:
        repos_data = data["repos"]
        if not isinstance(repos_data, dict):
            raise ConfigError(
                f"Expected a mapping under 'repos' key in repos.yaml, "
                f"got {type(repos_data).__name__}"
            )
        return dict(repos_data)
    # Legacy flat format: the document IS the repo mapping. The sibling
    # ``meta`` block is not a repo, so never surface it as one.
    return {k: v for k, v in data.items() if k != "meta"}


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
    # -- core: Claude SDK vision gate (level-3 agents) --
    "core.claude_sdk_vision_enabled": "claude_sdk_vision_enabled",
    "core.claude_max_concurrency": "MILL_CLAUDE_MAX_CONCURRENCY",
    "core.enable_repo_creation": "enable_repo_creation",
    "core.repo_visibility_default": "repo_visibility_default",
    # Cross-repo meta-agent pass (surveys all repos for extraction/alignment).
    "core.meta_periodic": "meta_periodic",
    "core.meta_interval_seconds": "meta_interval_seconds",
    # Web-knowledge gateway sub-agent tuning (stale-days, request cap, model override).
    "core.web_knowledge_stale_days": "web_knowledge_stale_days",
    "core.web_knowledge_request_limit": "web_knowledge_request_limit",
    "core.web_knowledge_model": "web_knowledge_model",
    # Daily diagnostic agent (deterministic check orchestrator).
    "periodic.diagnostic.enabled": "diagnostic_periodic",
    "periodic.diagnostic.interval_seconds": "diagnostic_interval_seconds",
    "periodic.diagnostic.target_repo_id": "diagnostic_target_repo_id",
    "periodic.diagnostic.monitored_repo_ids": "diagnostic_monitored_repo_ids",
    # -- core.limits --
    "core.limits.coordinator_requests": "coordinator_request_limit",
    "core.limits.subtask_request_limit": "subtask_request_limit",
    "core.limits.test_requests": "test_request_limit",
    "core.limits.consult_requests": "consult_request_limit",
    "core.limits.explore_requests": "explore_request_limit",
    "core.limits.explore_max_tokens": "explore_max_tokens",
    "core.limits.parallel_explore_max": "parallel_explore_max",
    "core.limits.max_refine_explore_calls": "max_refine_explore_calls",
    "core.limits.max_refine_read_file_calls": "max_refine_read_file_calls",
    "core.limits.refine_requests": "refine_request_limit",
    "core.limits.refine_requests_simple": "refine_request_limit_simple",
    "core.limits.coordinator_max_tool_calls": "coordinator_max_tool_calls",
    "core.limits.refine_max_tool_calls": "refine_max_tool_calls",
    "core.limits.refine_max_errors": "refine_max_errors",
    "core.limits.maintenance_requests": "maintenance_request_limit",
    "core.limits.audit_requests": "audit_request_limit",
    "core.limits.dedup_requests": "dedup_request_limit",
    "core.limits.dedup_max_candidates": "dedup_max_candidates",
    "core.limits.dedup_skip_on_no_overlap": "dedup_skip_on_no_overlap",
    "core.limits.dedup_candidate_body_max_chars": "dedup_candidate_body_max_chars",
    "core.limits.obsolescence_requests": "obsolescence_request_limit",
    "core.limits.doc_requests": "doc_request_limit",
    "core.limits.doc_classifier_requests": "doc_classifier_request_limit",
    "core.limits.doc_classifier_diff_max_chars": "doc_classifier_diff_max_chars",
    "core.limits.review_requests": "review_request_limit",
    "core.limits.web_research_requests": "web_research_request_limit",
    "core.limits.scope_triage_requests": "scope_triage_request_limit",
    "core.limits.scope_triage_max_files": "scope_triage_max_files",
    "core.limits.triage_requests": "triage_request_limit",
    "core.limits.already_done_requests": "already_done_request_limit",
    "core.limits.max_fix_iterations": "max_fix_iterations",
    "core.limits.max_stuck_cycles": "max_stuck_cycles",
    "core.limits.max_spend_usd_per_ticket": "max_spend_usd_per_ticket",
    "core.limits.stage_timeout_seconds": "stage_timeout_seconds",
    "core.limits.stage_timeout_overrides": "stage_timeout_overrides",
    "core.limits.transient_retries": "transient_retries",
    "core.limits.transient_backoff_base": "transient_backoff_base",
    "core.limits.transient_backoff_cap": "transient_backoff_cap",
    "core.limits.rate_limit_backoff_base": "rate_limit_backoff_base",
    "core.limits.rate_limit_backoff_cap": "rate_limit_backoff_cap",
    "core.limits.stage_retry_max_attempts": "stage_retry_max_attempts",
    "core.limits.stage_retry_base_delay": "stage_retry_base_delay",
    "core.limits.stage_retry_max_delay": "stage_retry_max_delay",
    "core.limits.network_probe_host": "network_probe_host",
    "core.limits.network_outage_retry_seconds": "network_outage_retry_seconds",
    "core.limits.max_global_concurrency": "MILL_MAX_GLOBAL_CONCURRENCY",
    # -- core: credit-balance warning --
    "core.low_credit_threshold_usd": "low_credit_threshold_usd",
    "core.low_credit_poll_enabled": "low_credit_poll_enabled",
    "core.low_credit_poll_interval_seconds": "low_credit_poll_interval_seconds",
    "core.requeue_batch_size": "requeue_batch_size",
    "core.requeue_batch_pause_seconds": "requeue_batch_pause_seconds",
    "core.startup_jitter_seconds": "startup_jitter_seconds",
    "core.board_list_cache_ttl_seconds": "board_list_cache_ttl_seconds",
    # -- core.memory --
    "core.memory.max_memory_chars": "max_memory_chars",
    "core.memory.retrospect_log_max_chars": "retrospect_log_max_chars",
    "core.memory.retrospect_candidates_max_entries": "retrospect_candidates_max_entries",
    "core.memory.reference_files_max_count": "reference_files_max_count",
    "core.memory.reference_files_max_total_lines": "reference_files_max_total_lines",
    "core.memory.dedup_lookback_days": "dedup_lookback_days",
    # -- stages.review --
    "stages.review.prior_context_max_chars": "review_prior_context_max_chars",
    "stages.review.diff_max_chars": "review_diff_max_chars",
    "stages.review.output_token_budget": "review_output_token_budget",
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
    "sandbox.smoke_command": "smoke_command",
    "sandbox.skills_dir": "skills_dir",
    "sandbox.network": "sandbox_network",
    "sandbox.proxy_url": "sandbox_proxy_url",
    # -- web --
    "web.search_enabled": "web_search",
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
    "core.limits.refine_web_fetch_max_calls": "refine_web_fetch_max_calls",
    "core.limits.refine_web_fetch_max_total_bytes": "refine_web_fetch_max_total_bytes",
    "core.limits.refine_web_search_max_calls": "refine_web_search_max_calls",
    "core.lint_on_edit": "lint_on_edit",
    "core.read_file_max_chars": "read_file_max_chars",
    "core.language_instructions_dir": "language_instructions_dir",
    # -- gates --
    "gates.require_approval": "require_approval",
    "gates.auto_approve_enabled": "auto_approve_enabled",
    "gates.review_enabled": "review_enabled",
    "gates.review_max_rounds": "review_max_rounds",
    "gates.review_feedback_enabled": "review_feedback_enabled",
    "gates.auto_merge_enabled": "auto_merge_enabled",
    "gates.comments_after_body": "comments_after_body",
    "gates.refine_triage_enabled": "refine_triage_enabled",
    "gates.max_re_refine_cycles_before_cheap": "max_re_refine_cycles_before_cheap",
    "gates.refine_trivial_routing_enabled": "refine_trivial_routing_enabled",
    "gates.refine_trivial_model_level": "refine_trivial_model_level",
    "gates.refine_subscription_tier_routing_enabled": "refine_subscription_tier_routing_enabled",
    "gates.refine_subscription_model_default": "refine_subscription_model_default",
    "gates.refine_subscription_model_complex": "refine_subscription_model_complex",
    "gates.maintenance_triage_enabled": "maintenance_triage_enabled",
    "gates.refine_advisory_dedup_enabled": "refine_advisory_dedup_enabled",
    "gates.spec_review_enabled": "spec_review_enabled",
    "gates.scope_triage_enabled": "scope_triage_enabled",
    "gates.pr_summary_enabled": "pr_summary_enabled",
    "gates.obsolescence_gate_enabled": "obsolescence_gate_enabled",
    "gates.freshness_gate_enabled": "freshness_gate_enabled",
    "gates.reviewer_agreement_gate_enabled": "reviewer_agreement_gate_enabled",
    "gates.prerequisite_gate_enabled": "prerequisite_gate_enabled",
    "gates.refine_mill_misroute_gate_enabled": "refine_mill_misroute_gate_enabled",
    "gates.auto_merge_main_debt_detection_enabled": "auto_merge_main_debt_detection_enabled",
    # -- ci --
    "ci.codeql_fp_triage_enabled": "codeql_fp_triage_enabled",
    # -- pipeline --
    # Post-MILL_*-alias-purge these map directly to Settings field
    # names (no env-var alias on the Field). Keeping a MILL_* value
    # here would make YamlSettingsSource look up an alias that no
    # longer exists on the model, silently dropping every YAML
    # override in this block.
    "pipeline.branch_prefix": "branch_prefix",
    "pipeline.merge_poll_seconds": "merge_poll_seconds",
    "pipeline.rebase_max_attempts": "rebase_max_attempts",
    "pipeline.ci_fix_max_iterations": "ci_fix_max_iterations",
    "pipeline.ci_fix_max_attempts": "ci_fix_max_attempts",
    "pipeline.ci_fix_max_cycles": "ci_fix_max_cycles",
    "pipeline.ci_fix_max_identical_failures": "ci_fix_max_identical_failures",
    "pipeline.ci_fix_wait_poll_interval_s": "ci_fix_wait_poll_interval_s",
    "pipeline.ci_fix_wait_timeout_s": "ci_fix_wait_timeout_s",
    "pipeline.ci_fix_request_limit": "ci_fix_request_limit",
    "pipeline.auto_fix_max_cycles": "auto_fix_max_cycles",
    "pipeline.ping_pong_max_alternations": "ping_pong_max_alternations",
    "pipeline.review_revision_max_attempts": "review_revision_max_attempts",
    "pipeline.retrospect_spawn_drafts": "retrospect_spawn_drafts",
    "pipeline.retrospect_spawn_agented_proposals": "retrospect_spawn_agented_proposals",
    "pipeline.prune_clone_on_close": "prune_clone_on_close",
    "pipeline.max_archived_tickets": "max_archived_tickets",
    "pipeline.max_comments_per_ticket": "max_comments_per_ticket",
    "pipeline.retrospect_memory_path": "retrospect_memory_path",
    "pipeline.trace_inspector_memory_path": "trace_inspector_memory_path",
    "pipeline.trace_review_target_repo_id": "trace_review_target_repo_id",
    "pipeline.implement_memory_path": "implement_memory_path",
    "pipeline.refine_memory_path": "refine_memory_path",
    "pipeline.doc_memory_path": "doc_memory_path",
    "pipeline.ci_fix_memory_path": "ci_fix_memory_path",
    "pipeline.rebase_memory_path": "rebase_memory_path",
    "pipeline.ci_patterns_path": "ci_patterns_path",
    "pipeline.delete_branch_on_merge": "delete_branch_on_merge",
    "pipeline.review_revision_memory_path": "review_revision_memory_path",
    # -- periodic.bespoke --
    "periodic.bespoke_periodic": "bespoke_periodic",
    "periodic.bespoke_discovery_interval_seconds": "bespoke_discovery_interval_seconds",
    # -- periodic.audit --
    "periodic.audit.enabled": "audit_periodic",
    "periodic.audit.interval_seconds": "audit_interval_seconds",
    "periodic.audit.memory_path": "audit_memory_path",
    # -- periodic.trace_health --
    "periodic.trace_health.enabled": "trace_health_periodic",
    "periodic.trace_health.interval_seconds": "trace_health_interval_seconds",
    # -- periodic.trace_review --
    "periodic.trace_review.enabled": "trace_review_periodic",
    "periodic.trace_review.interval_seconds": "trace_review_interval_seconds",
    "periodic.trace_review.cost_multiplier": "trace_review_cost_multiplier",
    "periodic.trace_review.obs_multiplier": "trace_review_obs_multiplier",
    "periodic.trace_review.max_repeated_tool": "trace_review_max_repeated_tool",
    "periodic.trace_review.max_drafts_per_run": "trace_review_max_drafts_per_run",
    "periodic.trace_review.max_traces_per_run": "trace_review_max_traces_per_run",
    # -- periodic.trace_review.inspector (dynamic budget) --
    "periodic.trace_review.inspector_min_requests": "trace_review_inspector_min_requests",
    "periodic.trace_review.inspector_max_requests": "trace_review_inspector_max_requests",
    "periodic.trace_review.inspector_requests_per_obs": "trace_review_inspector_requests_per_obs",
    "periodic.trace_review.inspector_max_obs_for_tools": "trace_review_inspector_max_obs_for_tools",
    "periodic.trace_review.inspector_toolless_requests": "trace_review_inspector_toolless_requests",
    # -- periodic.trace_review (inspector operational knobs) --
    "periodic.trace_review.max_tool_calls": "trace_review_max_tool_calls",
    "periodic.trace_review.max_errors": "trace_review_max_errors",
    "periodic.trace_review.model_level": "trace_review_model_level",
    "periodic.trace_review.per_obs_cost_threshold": "trace_review_per_obs_cost_threshold",
    "periodic.trace_review.tool_request_limit": "trace_review_tool_request_limit",
    "periodic.trace_review.max_inspections_per_run": "trace_review_max_inspections_per_run",
    "periodic.trace_review.initial_lookback_hours": "trace_review_initial_lookback_hours",
    "periodic.trace_review.restart_correlation_window_seconds": "trace_review_restart_correlation_window_seconds",
    "periodic.trace_review.dedup_lookback_days": "trace_review_dedup_lookback_days",
    # -- periodic.stale_branch_cleanup --
    "periodic.stale_branch_cleanup.enabled": "stale_branch_cleanup_periodic",
    "periodic.stale_branch_cleanup.interval_seconds": "stale_branch_cleanup_interval_seconds",
    "periodic.stale_branch_cleanup.max_age_days": "stale_branch_max_age_days",
    "periodic.stale_branch_cleanup.prefix_only": "stale_branch_cleanup_prefix_only",
    # -- periodic.sandbox_reaper --
    "periodic.sandbox_reaper.enabled": "sandbox_reaper_periodic",
    "periodic.sandbox_reaper.interval_seconds": "sandbox_reaper_interval_seconds",
    # -- periodic.health --
    "periodic.health.enabled": "health_periodic",
    "periodic.health.interval_seconds": "health_interval_seconds",
    "periodic.health.memory_path": "health_memory_path",
    # -- periodic.run_health --
    "periodic.run_health.enabled": "run_health_periodic",
    "periodic.run_health.interval_seconds": "run_health_interval_seconds",
    "periodic.run_health.window_hours": "run_health_window_hours",
    "periodic.run_health.target_repo_id": "run_health_target_repo_id",
    "periodic.run_health.memory_path": "run_health_memory_path",
    # -- periodic.test_gap --
    "periodic.test_gap.enabled": "test_gap_periodic",
    "periodic.test_gap.interval_seconds": "test_gap_interval_seconds",
    "periodic.test_gap.memory_path": "test_gap_memory_path",
    "periodic.test_gap.request_limit": "test_gap_request_limit",
    "periodic.test_gap.max_tool_calls": "test_gap_max_tool_calls",
    "periodic.test_gap.max_errors": "test_gap_max_errors",
    # -- periodic.agent_check --
    "periodic.agent_check.enabled": "agent_check_periodic",
    "periodic.agent_check.interval_seconds": "agent_check_interval_seconds",
    "periodic.agent_check.memory_path": "agent_check_memory_path",
    # -- periodic.bc_check --
    "periodic.bc_check.enabled": "bc_check_periodic",
    "periodic.bc_check.interval_seconds": "bc_check_interval_seconds",
    "periodic.bc_check.memory_path": "bc_check_memory_path",
    # -- periodic.completeness_check --
    "periodic.completeness_check.enabled": "completeness_check_periodic",
    "periodic.completeness_check.interval_seconds": "completeness_check_interval_seconds",
    "periodic.completeness_check.memory_path": "completeness_check_memory_path",
    # -- periodic.copy_paste --
    "periodic.copy_paste.enabled": "copy_paste_periodic",
    "periodic.copy_paste.interval_seconds": "copy_paste_interval_seconds",
    "periodic.copy_paste.memory_path": "copy_paste_memory_path",
    # -- periodic.forge_parity --
    "periodic.forge_parity.enabled": "forge_parity_periodic",
    "periodic.forge_parity.interval_seconds": "forge_parity_interval_seconds",
    "periodic.forge_parity.memory_path": "forge_parity_memory_path",
    # -- periodic.survey --
    "periodic.survey.enabled": "survey_periodic",
    "periodic.survey.interval_seconds": "survey_interval_seconds",
    "periodic.survey.memory_path": "survey_memory_path",
    "periodic.survey.request_limit": "survey_request_limit",
    "periodic.survey.web_fetch_max_calls": "survey_web_fetch_max_calls",
    "periodic.survey.web_fetch_max_total_bytes": "survey_web_fetch_max_total_bytes",
    "periodic.survey.web_search_max_calls": "survey_web_search_max_calls",
    # -- periodic.data_dir_audit --
    "periodic.data_dir_audit.enabled": "data_dir_audit_periodic",
    "periodic.data_dir_audit.interval_seconds": "data_dir_audit_interval_seconds",
    "periodic.data_dir_audit.memory_path": "data_dir_audit_memory_path",
    "periodic.data_dir_audit.size_threshold_bytes": "data_dir_audit_size_threshold_bytes",
    "periodic.data_dir_audit.growth_delta_bytes": "data_dir_audit_growth_delta_bytes",
    "periodic.data_dir_audit.growth_delta_pct": "data_dir_audit_growth_delta_pct",
    "periodic.data_dir_audit.growth_delta_pct_min_bytes": "data_dir_audit_growth_delta_pct_min_bytes",
    "periodic.data_dir_audit.max_drafts_per_pass": "data_dir_audit_max_drafts_per_pass",
    "periodic.data_dir_audit.prune_closed": "data_dir_audit_prune_closed",
    "periodic.data_dir_audit.prune_closed_age_seconds": "data_dir_audit_prune_closed_age_seconds",
    "periodic.data_dir_audit.prune_terminal_clones": "data_dir_audit_prune_terminal_clones",
    "periodic.data_dir_audit.prune_terminal_clones_age_seconds": "data_dir_audit_prune_terminal_clones_age_seconds",
    "periodic.data_dir_audit.prune_db_rows": "data_dir_audit_prune_db_rows",
    "periodic.data_dir_audit.prune_memory_ledgers": "data_dir_audit_prune_memory_ledgers",
    "periodic.data_dir_audit.prune_orphans": "data_dir_audit_prune_orphans",
    "periodic.data_dir_audit.prune_orphans_age_seconds": "data_dir_audit_prune_orphans_age_seconds",
    # -- periodic.state_sync --
    "periodic.state_sync.model": "state_sync_model",
    "periodic.state_sync.enabled": "state_sync_periodic",
    "periodic.state_sync.interval_seconds": "state_sync_interval_seconds",
    "periodic.state_sync.memory_path": "state_sync_memory_path",
    # -- periodic.env_doc_sync --
    "periodic.env_doc_sync.model": "env_doc_sync_model",
    "periodic.env_doc_sync.enabled": "env_doc_sync_periodic",
    "periodic.env_doc_sync.interval_seconds": "env_doc_sync_interval_seconds",
    "periodic.env_doc_sync.memory_path": "env_doc_sync_memory_path",
    # -- periodic.config_sync --
    "periodic.config_sync.enabled": "config_sync_periodic",
    "periodic.config_sync.interval_seconds": "config_sync_interval_seconds",
    "periodic.config_sync.memory_path": "config_sync_memory_path",
    # -- periodic.member_sync (deterministic — no model, no memory_path) --
    "periodic.member_sync.enabled": "member_sync_periodic",
    "periodic.member_sync.interval_seconds": "member_sync_interval_seconds",
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
    "periodic.module_curator.enabled": "module_curator_periodic",
    "periodic.module_curator.interval_seconds": "module_curator_interval_seconds",
    "periodic.module_curator.memory_path": "module_curator_memory_path",
    "periodic.module_curator.request_limit": "module_curator_request_limit",
    # -- service --
    "service.data_dir": "data_dir",
    "service.default_repo_id": "default_repo_id",
    "service.api_host": "api_host",
    "service.api_port": "api_port",
    "service.api_url": "api_url",
    "service.shutdown_grace_seconds": "shutdown_grace_seconds",
    # -- board_agent (agent-comm broker responder) --
    "board_agent.enabled": "board_agent_enabled",
    "board_agent.api_url": "board_agent_api_url",
    "board_agent.api_token": "board_agent_api_token",
    "board_agent.repo_id": "board_agent_repo_id",
    "board_agent.write_ops": "board_agent_write_ops",
    "board_agent.broker_host": "board_agent_broker_host",
    "board_agent.broker_port": "board_agent_broker_port",
    "board_agent.broker_scheme": "board_agent_broker_scheme",
    "board_agent.broker_token": "board_agent_broker_token",
    # -- board_manager (conversational LLM board manager) --
    "board_manager.enabled": "board_manager_enabled",
    "board_manager.broker_token": "board_manager_broker_token",
    "board_manager.model": "board_manager_model",
    "board_manager.recall_model": "board_manager_recall_model",
    "board_manager.max_conversations": "board_manager_max_conversations",
    # -- component_agent (monitor/config responder on the broker) --
    "component_agent.enabled": "component_agent_enabled",
    "component_agent.agent_id": "component_agent_agent_id",
    "component_agent.broker_host": "component_agent_broker_host",
    "component_agent.broker_port": "component_agent_broker_port",
    "component_agent.broker_scheme": "component_agent_broker_scheme",
    "component_agent.broker_token": "component_agent_broker_token",
    # -- epic dedup lookback (top-level, mirrors core.memory.dedup_lookback_days) --
    "epic_dedup_lookback_days": "epic_dedup_lookback_days",
}


def flatten_yaml_config(yaml_config: dict) -> dict[str, object]:
    """Flatten a nested YAML config dict into kwargs for ``Settings()``.

    Walks the nested dict, maps each ``dotted.path`` key through
    ``_YAML_PATH_TO_ALIAS``, and returns a flat dict of env-var alias
    names → values.  Only values that have a mapping are included —
    unknown paths are silently ignored.

    When the same env-var alias is reachable through multiple YAML paths,
    the value from the *last* path traversed wins (dict insertion order).
    """
    return flatten_config(yaml_config, alias_map=_YAML_PATH_TO_ALIAS)
