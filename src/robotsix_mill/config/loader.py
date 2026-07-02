"""YAML configuration loader for robotsix-mill.

The mill reads a SINGLE config file: ``config/config.yaml`` (gitignored)
when present, else the committed ``config/config.example.yaml`` template
(so CI / tests without a ``config.yaml`` still get the committed
defaults).  The file holds every non-secret knob plus a top-level
``secrets:`` block; :func:`load_yaml_config` returns the non-secret part
(for the ``Settings`` model) and :func:`load_secrets_yaml` returns the
``secrets:`` sub-map (for the ``Secrets`` model).
"""

from __future__ import annotations


import os
from pathlib import Path

from robotsix_yaml_config import (
    YamlConfigError,
    flatten_config,
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
_CONFIG_FILE = _YAML_DIR / "config.yaml"
_EXAMPLE_FILE = _YAML_DIR / "config.example.yaml"

# Literal placeholder used for every secret in ``config.example.yaml``.
# A secret leaf equal to this is treated as UNSET (the field falls back
# to its ``None`` default), so example / CI runs behave like "no secret
# configured".
_SECRET_SENTINEL = "SECRET"  # noqa: S105 — sentinel, not a real credential


def _resolve_config_path(config_file: str | None) -> Path:
    """Resolve the single config file to read.

    Precedence: explicit *config_file* arg > ``MILL_CONFIG_FILE`` env >
    default resolution (``config/config.yaml`` if it exists, else the
    committed ``config/config.example.yaml`` template).  An explicit
    empty string ``""`` means "use the committed example" — the hermetic
    choice used by the test suite (its secrets are all-``SECRET`` → unset).
    """
    if config_file is not None:
        explicit: str | None = config_file
    else:
        explicit = os.environ.get("MILL_CONFIG_FILE")

    if explicit:  # non-empty explicit path
        return Path(explicit)
    if explicit == "":  # explicit empty → committed example (hermetic)
        return _EXAMPLE_FILE
    # explicit is None → default resolution.
    return _CONFIG_FILE if _CONFIG_FILE.exists() else _EXAMPLE_FILE


def load_yaml_config(config_file: str | None = None, skip_local: bool = False) -> dict:
    """Load the single mill config file and return its non-secret part.

    Reads ``config/config.yaml`` when present, else the committed
    ``config/config.example.yaml`` (overridable via the *config_file* arg
    or the ``MILL_CONFIG_FILE`` env var; ``""`` forces the committed
    example).  The top-level ``secrets:`` block is stripped — it is
    consumed separately by :func:`load_secrets_yaml` / the ``Secrets``
    model, never merged into ``Settings``.

    Returns a nested dict mirroring the YAML structure
    (e.g. ``{"core": {"limits": {"test_requests": 30}, ...}}``).

    *skip_local* is accepted for backward compatibility and ignored —
    there is no longer a separate local overlay layer.

    Raises ``ConfigError`` if the resolved file is missing or contains
    malformed YAML.
    """
    del skip_local  # no-op: single-file model has no separate overlay
    path = _resolve_config_path(config_file)
    if not path.exists():
        raise ConfigError(
            f"Required config file not found: {path}. Copy "
            "config/config.example.yaml to config/config.yaml, or point "
            "MILL_CONFIG_FILE at a config file."
        )
    try:
        data = read_yaml_file(path)
    except YamlConfigError as exc:
        raise ConfigError(str(exc)) from exc
    result = dict(data) if isinstance(data, dict) else {}
    result.pop("secrets", None)
    return result


def load_secrets_yaml(secrets_file: str | None = None) -> dict:
    """Return the ``secrets:`` sub-map of the single mill config file.

    Path resolution mirrors :func:`load_yaml_config`: explicit
    *secrets_file* arg > ``MILL_SECRETS_FILE`` env > default
    (``config/config.yaml`` if present, else ``config/config.example.yaml``).
    An explicit empty string ``""`` forces the committed example (used by
    the test suite).

    Returns a flat dict keyed by the secret field names
    (e.g. ``{"openrouter_api_key": "sk-...", ...}``).  Blank values and
    leaves equal to the ``SECRET`` sentinel are dropped so unset secrets
    fall back to the ``Secrets`` model's ``None`` defaults.

    Missing file → empty dict (not an error — secrets are optional for
    CI / mocked tests).  Malformed YAML → ``ConfigError``.
    """
    if secrets_file is not None:
        explicit: str | None = secrets_file
    else:
        explicit = os.environ.get("MILL_SECRETS_FILE")

    if explicit:
        path = Path(explicit)
    elif explicit == "":
        path = _EXAMPLE_FILE
    else:  # None → default resolution
        path = _CONFIG_FILE if _CONFIG_FILE.exists() else _EXAMPLE_FILE

    if not path.exists():
        return {}

    try:
        data = read_yaml_file(path)
    except YamlConfigError as exc:
        raise ConfigError(str(exc)) from exc

    raw = data.get("secrets", {}) if isinstance(data, dict) else {}
    if not isinstance(raw, dict):
        return {}
    return {
        key: value
        for key, value in raw.items()
        if not (
            value is None
            or (isinstance(value, str) and value.strip() in ("", _SECRET_SENTINEL))
        )
    }


def _load_repos_document(file_path: str | None = None) -> dict:  # noqa: C901
    """Read and parse the repos configuration document.

    Merges operator repos from ``config.yaml`` (``repos:`` key) with
    machine-owned overlay entries from ``<data_dir>/registered_repos.yaml``.
    The operator entry wins on repo-id conflict. Returns the merged
    ``{"repos": {...}}`` mapping, or ``{}`` when nothing is configured —
    zero repos is valid.

    An explicit *file_path* arg or the ``MILL_REPOS_FILE`` env var overrides
    the config file and reads the given file directly (used by the test
    suite); an explicit ``""`` means "no repos".
    """
    # 1. Explicit override: arg > env var — reads the given file directly.
    if file_path is not None:
        path_str: str | None = file_path
    else:
        path_str = os.environ.get("MILL_REPOS_FILE")
    if path_str == "":
        return {}
    if path_str is not None:
        path = Path(path_str)
        if not path.exists():
            return {}
        try:
            data = read_yaml_file(path)
        except YamlConfigError as exc:
            raise ConfigError(str(exc)) from exc
        return data if isinstance(data, dict) else {}

    # 2. Operator repos from config.yaml (``repos:`` key).
    try:
        cfg = load_yaml_config()
    except ConfigError:
        cfg = {}
    has_operator_key = isinstance(cfg, dict) and "repos" in cfg
    operator_repos: dict[str, object] = (
        (cfg.get("repos") or {}) if has_operator_key else {}
    )
    if not isinstance(operator_repos, dict):
        operator_repos = {}

    # 3. Machine-owned overlay: <data_dir>/registered_repos.yaml.
    #    data_dir is read from the same loaded config (service.data_dir)
    #    to stay consistent with Settings without a circular import.
    data_dir_str: str = (
        (cfg.get("service") or {}).get("data_dir", ".data")
        if isinstance(cfg, dict)
        else ".data"
    )
    overlay_path = Path(data_dir_str) / "registered_repos.yaml"
    overlay_repos: dict[str, object] = {}
    if overlay_path.exists():
        try:
            overlay_data = read_yaml_file(overlay_path)
            raw_overlay = (
                (overlay_data.get("repos") or {})
                if isinstance(overlay_data, dict)
                else {}
            )
            if isinstance(raw_overlay, dict):
                overlay_repos = raw_overlay
        except YamlConfigError:
            pass  # corrupt overlay is tolerated; treat as empty

    # 4. Inject source marker so load_repos_config can set RepoConfig.source.
    for entry in overlay_repos.values():
        if isinstance(entry, dict):
            entry.setdefault("_mill_source", "auto")

    # 5. Merge: operator wins on repo-id conflict.
    if has_operator_key or overlay_repos:
        merged = {**overlay_repos, **operator_repos}  # operator overwrites overlay
        return {"repos": merged}

    return {}


def load_repos_yaml(file_path: str | None = None) -> dict[str, object]:
    """Read the merged repos configuration (``config/config.yaml`` +
    ``<data_dir>/registered_repos.yaml``).

    Returns a dict keyed by repo ID with nested ``board_id`` and
    ``langfuse`` sub-dicts
    (e.g. ``{"my-repo": {"board_id": "...", "langfuse": {...}}, ...}``).

    Missing key/file → returns an empty dict (not an error — repos config
    is optional).

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
                f"Expected a mapping under the 'repos' key, "
                f"got {type(repos_data).__name__}"
            )
        return dict(repos_data)
    # Flat format (override files only): the document IS the repo mapping.
    # The sibling ``meta`` block is not a repo, so never surface it as one.
    return {k: v for k, v in data.items() if k != "meta"}


# ---------------------------------------------------------------------------
#  YAML dotted-path → env-var alias mapping
# ---------------------------------------------------------------------------

# Maps ``"group.subgroup.field"`` YAML paths to the env-var alias
# (the ``Field(alias=...)`` value) on the ``Settings`` model.  Built
# from the RFC §4.2 mapping table and kept in sync with
# ``config/config.example.yaml``.
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
    "core.web_knowledge_cache_ttl_hours": "web_knowledge_cache_ttl_hours",
    "core.web_knowledge_request_limit": "web_knowledge_request_limit",
    "core.web_knowledge_model": "web_knowledge_model",
    # Daily diagnostic agent (deterministic check orchestrator).
    "periodic.diagnostic.enabled": "diagnostic_periodic",
    "periodic.diagnostic.interval_seconds": "diagnostic_interval_seconds",
    "periodic.diagnostic.target_repo_id": "diagnostic_target_repo_id",
    "periodic.diagnostic.monitored_repo_ids": "diagnostic_monitored_repo_ids",
    # -- core.limits --
    "core.limits.coordinator_requests": "MILL_PER_PASS_REQUEST_BUDGET",
    "core.limits.subtask_request_limit": "subtask_request_limit",
    "core.limits.test_requests": "test_request_limit",
    "core.limits.consult_requests": "consult_request_limit",
    "core.limits.explore_requests": "explore_request_limit",
    "core.limits.explore_max_tokens": "explore_max_tokens",
    "core.limits.max_refine_explore_calls": "max_refine_explore_calls",
    "core.limits.max_refine_read_file_calls": "max_refine_read_file_calls",
    "core.limits.refine_requests": "refine_request_limit",
    "core.limits.refine_requests_simple": "refine_request_limit_simple",
    "core.limits.coordinator_max_tool_calls": "coordinator_max_tool_calls",
    "core.limits.refine_max_tool_calls": "refine_max_tool_calls",
    "core.limits.refine_max_errors": "refine_max_errors",
    "core.limits.refine_dynamic_limit_multiplier": "refine_dynamic_limit_multiplier",
    "core.limits.refine_dynamic_limit_min": "refine_dynamic_limit_min",
    "core.limits.refine_dynamic_limit_spec_chars": "refine_dynamic_limit_spec_chars",
    "core.limits.refine_usage_warning_threshold": "refine_usage_warning_threshold",
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
    "core.limits.scope_triage_max_files": "scope_triage_max_files",
    "core.limits.triage_requests": "triage_request_limit",
    "core.limits.max_fix_iterations": "max_fix_iterations",
    "core.limits.max_stuck_cycles": "max_stuck_cycles",
    "core.limits.max_spend_usd_per_ticket": "max_spend_usd_per_ticket",
    "core.limits.max_traces_per_ticket": "max_traces_per_ticket",
    "core.limits.max_openrouter_marginal_usd_per_ticket": "max_openrouter_marginal_usd_per_ticket",
    "core.limits.stage_timeout_seconds": "stage_timeout_seconds",
    "core.limits.stage_timeout_overrides": "stage_timeout_overrides",
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
    "core.memory.dedup_lookback_days": "dedup_lookback_days",
    # -- stages.review --
    "stages.review.prior_context_max_chars": "review_prior_context_max_chars",
    "stages.review.diff_max_chars": "review_diff_max_chars",
    "stages.review.output_token_budget": "review_output_token_budget",
    "stages.review.delta_context_retry_enabled": "delta_context_retry_enabled",
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
    "gates.max_implement_review_cycles": "max_implement_review_cycles",
    "gates.implement_max_spawns_per_ticket": "implement_max_spawns_per_ticket",
    "gates.review_feedback_enabled": "review_feedback_enabled",
    "gates.auto_merge_enabled": "auto_merge_enabled",
    "gates.comments_after_body": "comments_after_body",
    "gates.refine_triage_enabled": "refine_triage_enabled",
    "gates.refine_prescriptive_spec_code_lines_threshold": "refine_prescriptive_spec_code_lines_threshold",
    "gates.refine_skip_llm_on_impl_ready_spec": "refine_skip_llm_on_impl_ready_spec",
    "gates.max_re_refine_cycles_before_cheap": "max_re_refine_cycles_before_cheap",
    "gates.max_refine_passes_per_ticket": "max_refine_passes_per_ticket",
    "gates.refine_trivial_routing_enabled": "refine_trivial_routing_enabled",
    "gates.refine_trivial_model_level": "refine_trivial_model_level",
    "gates.refine_trivial_subscription_model": "refine_trivial_subscription_model",
    "gates.refine_subscription_tier_routing_enabled": "refine_subscription_tier_routing_enabled",
    "gates.refine_subscription_model_default": "refine_subscription_model_default",
    "gates.refine_subscription_model_complex": "refine_subscription_model_complex",
    "gates.refine_findings_downgrade_enabled": "refine_findings_downgrade_enabled",
    "gates.refine_findings_downgrade_min_chars": "refine_findings_downgrade_min_chars",
    "gates.refine_subscription_model_findings": "refine_subscription_model_findings",
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
    "periodic.trace_review.min_confidence": "trace_review_min_confidence",
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
    # -- periodic.run_health --
    "periodic.run_health.enabled": "run_health_periodic",
    "periodic.run_health.interval_seconds": "run_health_interval_seconds",
    "periodic.run_health.window_hours": "run_health_window_hours",
    "periodic.run_health.target_repo_id": "run_health_target_repo_id",
    "periodic.run_health.memory_path": "run_health_memory_path",
    # -- periodic.test_gap --
    "periodic.test_gap.enabled": "test_gap_periodic",
    "periodic.test_gap.interval_seconds": "test_gap_interval_seconds",
    "periodic.test_gap.request_limit": "test_gap_request_limit",
    "periodic.test_gap.max_tool_calls": "test_gap_max_tool_calls",
    "periodic.test_gap.max_errors": "test_gap_max_errors",
    # -- periodic.agent_check --
    "periodic.agent_check.enabled": "agent_check_periodic",
    "periodic.agent_check.interval_seconds": "agent_check_interval_seconds",
    # -- periodic.bc_check --
    "periodic.bc_check.enabled": "bc_check_periodic",
    "periodic.bc_check.interval_seconds": "bc_check_interval_seconds",
    # -- periodic.completeness_check --
    "periodic.completeness_check.enabled": "completeness_check_periodic",
    "periodic.completeness_check.interval_seconds": "completeness_check_interval_seconds",
    "periodic.completeness_check.request_limit": "completeness_check_request_limit",
    # -- periodic.copy_paste --
    "periodic.copy_paste.enabled": "copy_paste_periodic",
    "periodic.copy_paste.interval_seconds": "copy_paste_interval_seconds",
    # -- periodic.forge_parity --
    "periodic.forge_parity.enabled": "forge_parity_periodic",
    "periodic.forge_parity.interval_seconds": "forge_parity_interval_seconds",
    # -- periodic.survey --
    "periodic.survey.enabled": "survey_periodic",
    "periodic.survey.interval_seconds": "survey_interval_seconds",
    "periodic.survey.request_limit": "survey_request_limit",
    "periodic.survey.web_fetch_max_calls": "survey_web_fetch_max_calls",
    "periodic.survey.web_fetch_max_total_bytes": "survey_web_fetch_max_total_bytes",
    "periodic.survey.web_search_max_calls": "survey_web_search_max_calls",
    # -- periodic.data_dir_gc --
    "periodic.data_dir_gc.enabled": "data_dir_gc_periodic",
    "periodic.data_dir_gc.interval_seconds": "data_dir_gc_interval_seconds",
    "periodic.data_dir_gc.prune_closed": "data_dir_gc_prune_closed",
    "periodic.data_dir_gc.prune_closed_age_seconds": "data_dir_gc_prune_closed_age_seconds",
    "periodic.data_dir_gc.prune_terminal_clones": "data_dir_gc_prune_terminal_clones",
    "periodic.data_dir_gc.prune_terminal_clones_age_seconds": "data_dir_gc_prune_terminal_clones_age_seconds",
    "periodic.data_dir_gc.prune_db_rows": "data_dir_gc_prune_db_rows",
    "periodic.data_dir_gc.prune_memory_ledgers": "data_dir_gc_prune_memory_ledgers",
    "periodic.data_dir_gc.prune_orphans": "data_dir_gc_prune_orphans",
    "periodic.data_dir_gc.prune_orphans_age_seconds": "data_dir_gc_prune_orphans_age_seconds",
    # -- periodic.dependabot_ingest --
    "periodic.dependabot_ingest.enabled": "dependabot_ingest_periodic",
    "periodic.dependabot_ingest.interval_seconds": "dependabot_ingest_interval_seconds",
    "periodic.dependabot_ingest.max_drafts_per_pass": "dependabot_ingest_max_drafts_per_pass",
    # -- periodic.state_sync --
    "periodic.state_sync.enabled": "state_sync_periodic",
    "periodic.state_sync.interval_seconds": "state_sync_interval_seconds",
    # -- periodic.env_doc_sync --
    "periodic.env_doc_sync.enabled": "env_doc_sync_periodic",
    "periodic.env_doc_sync.interval_seconds": "env_doc_sync_interval_seconds",
    # -- periodic.config_sync --
    "periodic.config_sync.enabled": "config_sync_periodic",
    "periodic.config_sync.interval_seconds": "config_sync_interval_seconds",
    # -- periodic.member_sync (deterministic — no model, no memory_path) --
    "periodic.member_sync.enabled": "member_sync_periodic",
    "periodic.member_sync.interval_seconds": "member_sync_interval_seconds",
    # -- periodic.ci_monitor (global cap only — enabled/interval are per-repo) --
    "periodic.ci_monitor.log_max_bytes": "ci_log_max_bytes",
    # -- periodic.timeout_escalation --
    "periodic.timeout_escalation.enabled": "timeout_escalation_periodic",
    "periodic.timeout_escalation.interval_seconds": "timeout_escalation_interval_seconds",
    "periodic.timeout_escalation.threshold_seconds": "timeout_escalation_threshold_seconds",
    # -- periodic.triage_boilerplate --
    "periodic.triage_boilerplate.enabled": "triage_boilerplate_periodic",
    "periodic.triage_boilerplate.interval_seconds": "triage_boilerplate_interval_seconds",
    # -- periodic.langfuse_cleanup --
    "periodic.langfuse_cleanup.enabled": "langfuse_cleanup_periodic",
    "periodic.langfuse_cleanup.interval_seconds": "langfuse_cleanup_interval_seconds",
    "periodic.langfuse_cleanup.max_traces": "langfuse_cleanup_max_traces",
    # -- periodic.module_curator --
    "periodic.module_curator.enabled": "module_curator_periodic",
    "periodic.module_curator.interval_seconds": "module_curator_interval_seconds",
    "periodic.module_curator.request_limit": "module_curator_request_limit",
    # -- periodic.orphaned_pr_check (deterministic per-repo stale-PR cleanup) --
    "periodic.orphaned_pr_check.enabled": "orphaned_pr_check_periodic",
    "periodic.orphaned_pr_check.interval_seconds": "orphaned_pr_check_interval_seconds",
    "periodic.orphaned_pr_check.min_age_hours": "orphaned_pr_min_age_hours",
    "periodic.orphaned_pr_check.max_actions_per_pass": "orphaned_pr_max_actions_per_pass",
    "periodic.orphaned_pr_check.dry_run": "orphaned_pr_dry_run",
    "periodic.orphaned_pr_check.bot_logins": "orphaned_pr_bot_logins",
    "periodic.orphaned_pr_check.max_closes_per_pass": "orphaned_pr_max_closes_per_pass",
    "periodic.orphaned_pr_check.max_files_per_pass": "orphaned_pr_max_files_per_pass",
    "periodic.orphaned_pr_check.track_foreign_prs": "orphaned_pr_track_foreign_prs",
    # -- service --
    "service.data_dir": "data_dir",
    "service.default_repo_id": "default_repo_id",
    "service.api_host": "api_host",
    "service.api_port": "api_port",
    "service.api_url": "api_url",
    "service.shutdown_grace_seconds": "shutdown_grace_seconds",
    # -- epic dedup lookback (top-level, mirrors core.memory.dedup_lookback_days) --
    "epic_dedup_lookback_days": "epic_dedup_lookback_days",
}


def flatten_yaml_config(yaml_config: dict[str, object]) -> dict[str, object]:
    """Flatten a nested YAML config dict into kwargs for ``Settings()``.

    Walks the nested dict, maps each ``dotted.path`` key through
    ``_YAML_PATH_TO_ALIAS``, and returns a flat dict of env-var alias
    names → values.  Only values that have a mapping are included —
    unknown paths are silently ignored.

    When the same env-var alias is reachable through multiple YAML paths,
    the value from the *last* path traversed wins (dict insertion order).
    """
    return flatten_config(yaml_config, alias_map=_YAML_PATH_TO_ALIAS)
