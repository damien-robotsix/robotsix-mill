"""Shared allowlists for config-sync drift checking.

Used by both the deterministic CI check (``scripts/check_config_sync.py``)
and the periodic LLM agent (``src/robotsix_mill/agents/config_syncing.py``)
so the two cannot disagree about the same repo state.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
#  Explicit, commented exception sets
# ---------------------------------------------------------------------------

# Settings keys in config.example.json that are intentionally NOT
# Settings model fields/aliases.  Each entry documents WHY.
SETTINGS_KEYS_NOT_IN_MODEL: frozenset[str] = frozenset()

# Settings model fields intentionally absent from
# ``config.example.json`` ``settings`` block.  Each field documents
# WHY it is not in the committed template.
MODEL_FIELDS_NOT_IN_JSON: frozenset[str] = frozenset(
    {
        # -- Secrets / credentials — sourced from the config.json
        #    ``secrets:`` block (Secrets model) or env vars --
        "openrouter_api_key",
        "forge_token",
        "forge_repo_create_token",
        "fleet_notify_token",
        "fleet_notify_url",
        "github_app_id",
        "github_app_private_key",
        "openrouter_management_key",
        "ntfy_url",
        "ntfy_token",
        "sandbox_push_token",
        # -- Langfuse config — canonical block at top-level ``langfuse:``,
        #    not a flat SecretStr field --
        "langfuse",
        # -- OpenRouter config — canonical block at top-level
        #    ``openrouter:``, not a flat SecretStr field --
        "openrouter",
        # -- Repos registry — not a flat setting field --
        "repos",
        # -- Fields with no JSON entry (yet) — listed here so the
        #    invariant passes at HEAD; each should eventually gain a
        #    JSON entry or be explicitly documented as env-only --
        "trace_review_max_inspector_runs_per_pass",
        # -- ci-fix agent timeout: default 1800 wraps the LLM agent call
        #    inside the ci-fix stage.  Committing a JSON default would
        #    be misleading because the live value SHOULD be smaller than
        #    the worker's stage_timeout_seconds and the operator tunes
        #    it per-deployment. --
        "ci_fix_agent_timeout_seconds",
        # -- packaged resource dirs — defaults resolve via
        #    importlib.resources to machine-specific absolute paths, so a
        #    committed template value can only be wrong somewhere.  A
        #    CWD-relative template value ("skills") bricked the container
        #    on 2026-07-19 (implement preflight blocked every ticket with
        #    "missing skill file"); overrides remain available via env or
        #    a deployment's own config.json --
        "skills_dir",
        "language_instructions_dir",
    }
)

# Rationales for MODEL_FIELDS_NOT_IN_JSON entries — one per field.
# Used by the periodic agent to explain *why* a field was previously
# judged intentional, so a reviewer can reassess.
MODEL_FIELDS_NOT_IN_JSON_RATIONALES: dict[str, str] = {
    "openrouter_api_key": "Secrets / credentials — sourced from the config.json secrets: block (Secrets model) or env vars",
    "forge_token": "Secrets / credentials — sourced from the config.json secrets: block (Secrets model) or env vars",
    "forge_repo_create_token": "Secrets / credentials — sourced from the config.json secrets: block (Secrets model) or env vars",
    "fleet_notify_token": "Secrets / credentials — sourced from the config.json secrets: block (Secrets model) or env vars",
    "fleet_notify_url": "Secrets / credentials — sourced from the config.json secrets: block (Secrets model) or env vars",
    "github_app_id": "Secrets / credentials — sourced from the config.json secrets: block (Secrets model) or env vars",
    "github_app_private_key": "Secrets / credentials — sourced from the config.json secrets: block (Secrets model) or env vars",
    "openrouter_management_key": "Secrets / credentials — sourced from the config.json secrets: block (Secrets model) or env vars",
    "ntfy_url": "Secrets / credentials — sourced from the config.json secrets: block (Secrets model) or env vars",
    "ntfy_token": "Secrets / credentials — sourced from the config.json secrets: block (Secrets model) or env vars",
    "sandbox_push_token": "Secrets / credentials — sourced from the config.json secrets: block (Secrets model) or env vars",
    "langfuse": "Langfuse config — canonical block at top-level langfuse:, not a flat SecretStr field",
    "openrouter": "OpenRouter config — canonical block at top-level openrouter:, not a flat SecretStr field",
    "repos": "Repos registry — not a flat setting field",
    "trace_review_max_inspector_runs_per_pass": "Field with no JSON entry yet — should eventually gain a JSON entry or be explicitly documented as env-only",
    "ci_fix_agent_timeout_seconds": "ci-fix agent timeout — default 1800 wraps the LLM agent call inside the ci-fix stage. Committing a JSON default would be misleading because the live value SHOULD be smaller than the worker's stage_timeout_seconds and the operator tunes it per-deployment",
    "skills_dir": "Packaged resource dir — default resolves via importlib.resources to machine-specific absolute paths, so a committed template value can only be wrong somewhere",
    "language_instructions_dir": "Packaged resource dir — default resolves via importlib.resources to machine-specific absolute paths",
}

# ``Secrets`` fields that are intentionally NOT user-configurable via
# the config file, so they never appear in the ``secrets:`` block of
# ``config/config.example.json``.
SECRETS_NOT_IN_EXAMPLE: frozenset[str] = frozenset()
