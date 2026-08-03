#!/usr/bin/env python3
"""Deterministic config-docs default-value drift checker for robotsix-mill.

Usage (from the repo root):
    python scripts/check_config_docs_sync.py

Cross-references the live Settings model field defaults against the
documented defaults in ``docs/config/configuration.md`` to catch drift
that the heuristic weekly ``completeness_check`` agent would otherwise
only notice on its next pass.

    * ``docs/config/configuration.md`` — the committed config reference
      docs.  Settings tables have ``Env var`` and ``Default`` columns.
    * ``robotsix_mill.config.Settings`` — the Pydantic-v2 model
      (introspected via ``model_fields``).

Invariants (each contributes drift lines; the run fails if any fire):

    1. Every ``Settings`` model field with a meaningful default that has
       a doc entry must have a matching documented default, unless listed
       in ``_MODEL_FIELDS_NOT_IN_DOCS``.
    2. Every env var documented in the settings tables must map to a real
       ``Settings`` field, unless listed in ``_DOC_ENV_VARS_NOT_IN_MODEL``.
    3. For every field present in both model and docs, the Python default
       must match the documented default, unless listed in
       ``_DEFAULT_MISMATCH_EXCEPTIONS``.
    4. Every field listed in ``_MODEL_FIELDS_NOT_IN_DOCS`` whose env var
       now resolves in the doc settings tables is a stale exception —
       it should be removed from the exception set.

This script is meant to be invoked from the repo root (which CI and the
``validate-config-docs-sync`` pre-commit hook both guarantee).

Exit codes:
    0 — every invariant holds; the config docs defaults are in sync.
    1 — at least one invariant fired; details are printed to stderr.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

# Ensure both the repo root and src/ are importable so 'import
# robotsix_mill' works when run as a flat script.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

_CONFIG_DOCS_MD = _REPO_ROOT / "docs" / "config" / "configuration.md"


# ---------------------------------------------------------------------------
#  Explicit, commented exception sets
# ---------------------------------------------------------------------------

# Model fields intentionally absent from the settings doc tables.
# Each entry documents WHY.
_MODEL_FIELDS_NOT_IN_DOCS: frozenset[str] = frozenset(
    {
        # -- Secrets / credentials — these are in the "Secrets reference"
        #    table, not the settings tables; all default to None and the
        #    secrets table has no "Default" column --
        "openrouter_api_key",
        "forge_token",
        "forge_repo_create_token",
        "github_app_id",
        "github_app_private_key",
        "langfuse_base_url",
        "langfuse_public_key",
        "langfuse_secret_key",
        "langfuse_project_id",
        "langfuse_project_name",
        "openrouter_management_key",
        "ntfy_url",
        "ntfy_token",
        # -- Credentials leaked from Secrets model into Settings (legacy
        #    compatibility).  Now has a documented entry; removed from
        #    exceptions —
        # -- default_factory fields whose effective default depends on
        #    runtime resolution (Path via importlib.resources, dict
        #    constructed at class-load, etc.) —
        "coordinator_timeout_overrides",
        # -- Config-file-only fields — documented with ``—`` env var
        #    so they can't be matched by env-var name.  The doc still
        #    records their defaults, but the mapping key is missing --
        "web_knowledge_cache_ttl_hours",
        "low_credit_threshold_usd",
        "low_credit_poll_enabled",
        "low_credit_poll_interval_seconds",
        "deliver_max_identical_blocks",
        "refine_web_fetch_max_calls",
        "refine_web_fetch_max_total_bytes",
        "refine_web_search_max_calls",
        # -- Fields documented only via the generic periodic-agent
        #    template (``MILL_<NAME>_PERIODIC`` / ``MILL_<NAME>_INTERVAL_SECONDS``).
        #    These agents have no individual settings-reference table,
        #    so their concrete env-var names never appear in the doc --
        "trace_health_periodic",
        "trace_health_interval_seconds",
        "timeout_escalation_periodic",
        "timeout_escalation_interval_seconds",
        "agent_check_periodic",
        "agent_check_interval_seconds",
        "health_periodic",
        "health_interval_seconds",
        "module_curator_periodic",
        "module_curator_interval_seconds",
        "dependabot_ingest_periodic",
        "dependabot_ingest_interval_seconds",
        "diagnostic_periodic",
        "diagnostic_interval_seconds",
        "langfuse_cleanup_periodic",
        "langfuse_cleanup_interval_seconds",
        "stale_branch_cleanup_periodic",
        "stale_branch_cleanup_interval_seconds",
        # -- Fields with no doc entry (yet) — listed here so the
        #    invariant passes at HEAD; each should eventually gain a
        #    doc entry or be explicitly documented as internal --
        "smoke_command",
        "allow_runtime_repo_registration",
        "diagnostic_ci_failure_threshold",
        "refine_prescriptive_spec_code_lines_threshold",
        "refine_skip_llm_on_impl_ready_spec",
        "copy_paste_periodic",
        "copy_paste_interval_seconds",
        "forge_parity_periodic",
        "forge_parity_interval_seconds",
        "web_fetch_max_calls",
        "web_fetch_max_total_bytes",
        "web_fetch_max_text_bytes",
        "web_fetch_raw",
        # -- sandbox_push_token is a secret field documented in the
        #    "Secrets reference" table, not the settings tables.  Its
        #    default is None and the secrets table has no "Default" column.
        "sandbox_push_token",
        # -- repos is configured via the "repos" key in config.json,
        #    documented in the "Repos registry" section, not the
        #    numbered settings tables.  Its default is an empty dict.
        "repos",
        # -- langfuse is a structured block (LangfuseConfig) with nested
        #    projects, documented in the "Secrets reference" section and
        #    via the observability docs, not the numbered settings tables.
        #    Its shape is too complex for a single settings-table row.
        "langfuse",
    }
)

# Env vars found in the doc settings tables that do NOT map to a
# Settings model field. Each entry documents WHY.
_DOC_ENV_VARS_NOT_IN_MODEL: frozenset[str] = frozenset(
    {
        # Web knowledge agent env vars are documented in the
        # "10.1 Web knowledge agent" table under a `—` YAML path,
        # but they don't correspond to Settings fields (they're in
        # the web-knowledge sub-agent's own config scope).
        "MILL_WEB_KNOWLEDGE_MODEL",
        "MILL_WEB_KNOWLEDGE_STALE_DAYS",
        "MILL_WEB_KNOWLEDGE_REQUEST_LIMIT",
    }
)

# Doc env-var → model field pairs where the default intentionally
# differs (e.g., the doc describes a conceptual default while the
# model uses a different internal representation). Each entry
# documents WHY.
_DEFAULT_MISMATCH_EXCEPTIONS: frozenset[tuple[str, str]] = frozenset(
    {
        # sandbox_image: model default is a lightweight image for local
        # dev; doc documents the production override as the "default"
        # ("python:3.14-slim" vs "robotsix/mill-sandbox:latest" as
        # described in the doc).  The doc row's Default column shows
        # "python:3.14-slim" which matches the model — no mismatch here
        # after all, but listed as a known tricky case.
        # review_diff_max_chars: doc uses Python underscore grouping
        # (200_000) for readability; the model stores 200000. Both
        # represent the same value (200,000).
        ("review_diff_max_chars", "MILL_REVIEW_DIFF_MAX_CHARS"),
    }
)


# ---------------------------------------------------------------------------
#  Helpers: env-var name resolution
# ---------------------------------------------------------------------------


def _field_env_var(name: str, field: Any) -> str:
    """Return the env-var name for a Settings model field.

    Fields with an explicit ``Field(alias=...)`` use that alias
    verbatim.  Otherwise the env var is ``MILL_`` + uppercase field
    name (from the model's ``env_prefix="MILL_"``).
    """
    if field.alias:
        return field.alias
    return "MILL_" + name.upper()


# ---------------------------------------------------------------------------
#  Helpers: Python default → doc-comparable string
# ---------------------------------------------------------------------------


def _python_default_to_doc_str(value: Any) -> str | None:
    """Convert a Python default value to the canonical string form
    for comparison with the documented default.

    The canonical form strips artificial quoting — e.g. the Python
    string ``"github.com"`` produces ``github.com``, matching the
    doc's quoted ``"github.com"`` after the doc parser strips its
    outer quotes.

    Returns ``None`` when the value cannot be converted (e.g.
    ``default_factory`` fields, ``Path`` objects).
    """
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        # Return the bare string value — the doc comparator also strips
        # outer double-quotes so they converge.
        return value
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    if isinstance(value, Path):
        return str(value)
    return None


def _parse_doc_default(text: str) -> str:
    """Normalize a documented default value for comparison.

    Strips outer double-quotes when present (e.g. ``"github.com"``
    → ``github.com``, ``""`` → empty string).  Bare values (``4``,
    ``true``, ``None``, ``.data``) are returned as-is.
    """
    t = text.strip()
    if len(t) >= 2 and t.startswith('"') and t.endswith('"'):
        return t[1:-1]
    return t


# ---------------------------------------------------------------------------
#  Helpers: markdown table parsing
# ---------------------------------------------------------------------------


def _parse_table_data_row(
    cells: list[str],
    default_col_idx: int | None,
    env_var_col_idx: int | None,
) -> tuple[str, str] | None:
    """Extract (env_var, default) from a table data row, or None if skipped."""
    if default_col_idx is None or env_var_col_idx is None:
        return None
    if default_col_idx >= len(cells) or env_var_col_idx >= len(cells):
        return None

    env_var = _strip_backticks(cells[env_var_col_idx].strip())
    default_text = _strip_backticks(cells[default_col_idx].strip())

    # Skip rows with no env var or placeholder env vars
    if not env_var or env_var in ("—", "-", "–"):
        return None
    # Skip placeholder patterns like MILL_<NAME>_PERIODIC
    if "<" in env_var or ">" in env_var:
        return None
    # Skip values that are clearly not env var names
    if env_var.lower() in ("yes", "no"):
        return None

    return env_var, default_text


def _parse_doc_tables(md_path: Path) -> dict[str, str]:
    """Parse settings tables from the config docs markdown.

    Returns a dict mapping env var name → documented default string
    (the raw text inside the backtick-quoted ``Default`` column).
    """
    text = md_path.read_text(encoding="utf-8")
    lines = text.splitlines()

    doc_defaults: dict[str, str] = {}
    in_table = False
    header_cols: list[str] = []
    default_col_idx: int | None = None
    env_var_col_idx: int | None = None

    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            in_table = False
            header_cols = []
            default_col_idx = None
            env_var_col_idx = None
            continue

        # Skip separator lines (|----|----|)
        if re.match(r"^\|[\s\-:|]+\|$", stripped):
            continue

        cells = _split_table_row(stripped)

        if not in_table:
            # This is a header row
            in_table = True
            header_cols = [c.strip().lower() for c in cells]
            try:
                default_col_idx = header_cols.index("default")
                # Look for "env var" column (with or without space)
                for i, h in enumerate(header_cols):
                    if h in ("env var", "env_var"):
                        env_var_col_idx = i
                        break
            except ValueError:
                # No "Default" column — skip this table
                in_table = False
                header_cols = []
                continue
            continue

        result = _parse_table_data_row(cells, default_col_idx, env_var_col_idx)
        if result is not None:
            doc_defaults[result[0]] = result[1]

    return doc_defaults


def _split_table_row(line: str) -> list[str]:
    """Split a markdown table row into cells.

    Handles the leading/trailing ``|`` and returns the cell contents
    (trimmed but with backtick content preserved).
    """
    # Remove leading and trailing pipe, then split on remaining pipes
    inner = line.strip().strip("|")
    # Split on `|` but preserve escaped pipes (unlikely)
    return [c.strip() for c in inner.split("|")]


def _strip_backticks(text: str) -> str:
    """Strip surrounding backticks from a markdown inline-code span."""
    t = text.strip()
    if t.startswith("`") and t.endswith("`"):
        return t[1:-1]
    return t


# ---------------------------------------------------------------------------
#  Invariant checks
# ---------------------------------------------------------------------------


def _check_model_fields_in_docs(
    model: type,
    doc_defaults: dict[str, str],
    exceptions: frozenset[str],
) -> list[str]:
    """Invariant 1 + 3: every Settings field with a default must be
    documented, and its documented default must match the model default.
    """
    from pydantic.fields import PydanticUndefined

    drift: list[str] = []
    for name, field in model.model_fields.items():
        if name in exceptions:
            continue
        if field.default is PydanticUndefined:
            continue

        env_var = _field_env_var(name, field)
        if env_var not in doc_defaults:
            drift.append(
                f"Settings field {name!r} (env {env_var}) has no entry in "
                "docs/config/configuration.md settings tables and is not "
                "in the exception set"
            )
            continue

        doc_default_str = _parse_doc_default(doc_defaults[env_var])
        model_default_str = _python_default_to_doc_str(field.default)
        if model_default_str is None:
            continue  # can't convert — not a comparable type

        if (name, env_var) in _DEFAULT_MISMATCH_EXCEPTIONS:
            continue

        if doc_default_str != model_default_str:
            drift.append(
                f"Default mismatch for {name!r} (env {env_var}): "
                f"docs say {doc_defaults[env_var]!r}, "
                f"model says {model_default_str!r}"
            )

    return drift


def _check_doc_env_vars_in_model(
    doc_defaults: dict[str, str],
    valid_env_vars: set[str],
    exceptions: frozenset[str],
) -> list[str]:
    """Invariant 2: every env var in doc settings tables must be a
    real Settings field alias or derived env-var name.
    """
    drift: list[str] = []
    for env_var in sorted(doc_defaults):
        if env_var in exceptions:
            continue
        if env_var not in valid_env_vars:
            drift.append(
                f"Docs env var {env_var!r} does not match any "
                "Settings field name or alias"
            )
    return drift


def _check_stale_no_doc_exceptions(
    model: type,
    doc_defaults: dict[str, str],
    exceptions: frozenset[str],
) -> list[str]:
    """Reverse-check: flag exception entries whose env var now has a doc entry.

    Each field name in *exceptions* is resolved to its env-var name
    via the live model.  If that env var appears in *doc_defaults*
    (the parsed settings-reference tables), the exception is stale —
    the field is now documented and should be removed from the
    exception set.
    """
    drift: list[str] = []
    for name in sorted(exceptions):
        field = model.model_fields.get(name)
        if field is None:
            continue  # field no longer exists on the model; skip
        env_var = _field_env_var(name, field)
        if env_var in doc_defaults:
            drift.append(
                f"Stale exception: field {name!r} (env {env_var}) is in "
                "_MODEL_FIELDS_NOT_IN_DOCS but has a documented entry "
                "in docs/config/configuration.md"
            )
    return drift


def _build_valid_env_vars(model: type) -> set[str]:
    """Return the set of all valid env-var names for a Settings model."""
    names: set[str] = set()
    for name, field in model.model_fields.items():
        names.add(_field_env_var(name, field))
    return names


def collect_drift() -> list[str]:
    """Load the real on-disk surfaces and run every invariant."""

    from robotsix_mill.config import Settings

    if not _CONFIG_DOCS_MD.exists():
        return [f"Config docs file not found: {_CONFIG_DOCS_MD}"]

    doc_defaults = _parse_doc_tables(_CONFIG_DOCS_MD)
    if not doc_defaults:
        return ["No settings tables with 'Default' column found in config docs"]

    valid_env_vars = _build_valid_env_vars(Settings)

    drift: list[str] = []
    drift += _check_model_fields_in_docs(
        Settings, doc_defaults, _MODEL_FIELDS_NOT_IN_DOCS
    )
    drift += _check_doc_env_vars_in_model(
        doc_defaults, valid_env_vars, _DOC_ENV_VARS_NOT_IN_MODEL
    )
    drift += _check_stale_no_doc_exceptions(
        Settings, doc_defaults, _MODEL_FIELDS_NOT_IN_DOCS
    )
    return drift


def main() -> int:
    drift = collect_drift()
    if drift:
        for entry in drift:
            print(f"STALE: {entry}", file=sys.stderr)
        print(
            f"FAIL: {len(drift)} config-docs-sync drift item(s) detected",
            file=sys.stderr,
        )
        return 1

    print("config docs sync OK (all documented defaults match model defaults)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
