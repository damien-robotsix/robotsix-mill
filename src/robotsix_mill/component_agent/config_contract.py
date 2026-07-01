"""Config contract for the component-agent responder.

Mirrors robotsix-chat's contract API: the responder consumes
``get_config_snapshot``, ``describe_config``, ``validate_config_update``,
and ``apply_config_update`` to serve the ``config-get`` / ``config-set``
operations over the broker.
"""

from __future__ import annotations

import logging
from typing import Any

from robotsix_agent_comm.protocol import ConfigContractError  # noqa: F401 — re-export

from ..config.settings import Settings

logger = logging.getLogger("robotsix_mill")


# ---------------------------------------------------------------------------
#  Secret redaction
# ---------------------------------------------------------------------------

# Field-name substrings that trigger redaction in get_config_snapshot.
# Reuses the convention from config/secrets.py where every value is
# rendered as '***' in __repr__/model_dump.
_SECRET_NAME_MARKERS = ("token", "api_key", "password", "secret")


def _is_secret_key(key: str) -> bool:
    """Return True when *key* should be redacted (contains a secret marker)."""
    lower = key.lower()
    return any(marker in lower for marker in _SECRET_NAME_MARKERS)


# ---------------------------------------------------------------------------
#  SETTABLE_KEYS — live-mutable fields only
# ---------------------------------------------------------------------------

# Fields that CAN be live-applied at runtime without restart. Every
# included key MUST have zero startup-bound side effects (no forge
# wiring, no data-dir layout, no sandbox provisioning, no broker
# connection config).  Each key includes a one-line justification.

SETTABLE_KEYS: frozenset[str] = frozenset(
    {
        # Periodic-pass enable flags — each pass reads its flag on every
        # loop iteration; toggling takes effect on the next tick.
        "diagnostic_periodic",  # diagnostic agent toggle
        "audit_periodic",  # audit agent toggle
        "trace_health_periodic",  # trace-health check toggle
        "trace_review_periodic",  # trace-review toggle
        "health_periodic",  # health check toggle
        "agent_check_periodic",  # agent-check toggle
        "bc_check_periodic",  # bc-check toggle
        "completeness_check_periodic",  # completeness-check toggle
        "copy_paste_periodic",  # copy-paste toggle
        "module_curator_periodic",  # module-curator toggle
        "test_gap_periodic",  # test-gap toggle
        "survey_periodic",  # survey toggle
        "config_sync_periodic",  # config-sync toggle
        "data_dir_gc_periodic",  # data-dir gc toggle
        "langfuse_cleanup_periodic",  # Langfuse cleanup toggle
        "timeout_escalation_periodic",  # timeout escalation toggle
        "meta_periodic",  # meta-agent toggle
        "run_health_periodic",  # run-health toggle
        "stale_branch_cleanup_periodic",  # stale-branch cleanup toggle
        "sandbox_reaper_periodic",  # sandbox reaper toggle
        "forge_parity_periodic",  # forge-parity toggle
        "state_sync_periodic",  # state-sync toggle
        "env_doc_sync_periodic",  # env-doc sync toggle
        "member_sync_periodic",  # member-sync toggle
        # Model-name overrides — read on each agent invocation; the next
        # call picks up the new value.
        "board_manager_model",  # board-manager level-3 model override
        "board_manager_recall_model",  # board-manager level-1 recall model override
        "web_knowledge_model",  # web-knowledge gateway model override
        "state_sync_model",  # state-sync model override
        "env_doc_sync_model",  # env-doc sync model override
        # Memory / conversation cap — checked on each new conversation.
        "board_manager_max_conversations",  # max conversations in board-manager memory
        # Credit monitoring — checked on each poll loop iteration.
        "low_credit_threshold_usd",  # credit-balance alert threshold
        "low_credit_poll_enabled",  # credit-balance polling toggle
        "low_credit_poll_interval_seconds",  # credit-balance poll cadence
        # Stuck-detection / requeue tuning — read on each stuck check.
        "max_stuck_cycles",  # no-progress cycles before BLOCKED escalation
        "requeue_batch_size",  # startup requeue batch size
        "requeue_batch_pause_seconds",  # pause between requeue batches
        # CI fix tuning — read per CI-fix cycle.
        "max_fix_iterations",  # max CI-fix iterations per run
    }
)


# ---------------------------------------------------------------------------
#  Config snapshot / describe
# ---------------------------------------------------------------------------


def get_config_snapshot(settings: Settings) -> dict[str, Any]:
    """Return a flat dotted-path view of *settings* with secrets redacted.

    Iterates ``Settings.model_fields`` keys via ``getattr`` and redacts any
    key whose name contains ``token``, ``api_key``, ``password``, or
    ``secret`` to ``"***"``.  Reuses the redaction convention from
    ``config/secrets.py``.

    Uses ``getattr`` per-field instead of ``model_dump()`` to avoid
    allocating a full intermediate dict of ~347 fields on every call.
    """
    result: dict[str, Any] = {}
    for key in Settings.model_fields:
        value = getattr(settings, key)
        if _is_secret_key(key):
            result[key] = "***"
        else:
            result[key] = value
    return result


def describe_config() -> dict[str, Any]:
    """Return a structure describing which keys are settable and their types.

    Returns ``{"settable": {key: {"type": ...}}}`` derived from SETTABLE_KEYS
    and the ``Settings.model_fields`` metadata.
    """
    fields = Settings.model_fields
    settable: dict[str, dict[str, str]] = {}
    for key in sorted(SETTABLE_KEYS):
        if key in fields:
            ann = fields[key].annotation
            type_name = _type_name(ann)
            settable[key] = {"type": type_name}
    return {"settable": settable}


def _type_name(annotation: Any) -> str:
    """Human-readable type name for a pydantic field annotation."""
    origin = getattr(annotation, "__origin__", None)
    if origin is not None:
        args = getattr(annotation, "__args__", ())
        return f"{origin.__name__}[{', '.join(_type_name(a) for a in args)}]"
    if hasattr(annotation, "__name__"):
        return str(annotation.__name__)
    return str(annotation)


# ---------------------------------------------------------------------------
#  Validate / apply config updates
# ---------------------------------------------------------------------------


def validate_config_update(
    settings: Settings,
    updates: dict[str, Any],
) -> dict[str, tuple[Any, Any]]:
    """Validate a set of dotted-key→value updates WITHOUT mutating *settings*.

    Returns an audit map ``{key: (old_value, new_value)}`` on success.

    Raises ``ConfigContractError`` when:
    - Any key is not in ``SETTABLE_KEYS`` (startup-only field rejected).
    - The merged candidate fails pydantic validation (cross-field invariants).
    """
    # Reject unknown/startup-only keys FIRST — before any mutation.
    unknown = [k for k in updates if k not in SETTABLE_KEYS]
    if unknown:
        raise ConfigContractError(
            code="unknown_keys",
            message=f"Keys not in SETTABLE_KEYS: {', '.join(sorted(unknown))}",
            unknown_keys=sorted(unknown),
        )

    # Build the full candidate by merging the current settings with updates.
    current = settings.model_dump()
    merged = {**current, **updates}

    # Rebuild the model so @model_validator invariants run.
    try:
        candidate = Settings(**merged)
    except Exception as exc:
        raise ConfigContractError(
            code="validation_failed",
            message=f"Config update failed validation: {exc}",
            validation_error=str(exc),
        ) from exc

    # Build the audit map: old → new for each updated key.
    audit: dict[str, tuple[Any, Any]] = {}
    new_dump = candidate.model_dump()
    for key in updates:
        old_val = current.get(key)
        new_val = new_dump.get(key)
        audit[key] = (old_val, new_val)
    return audit


def apply_config_update(
    settings: Settings,
    updates: dict[str, Any],
    setter: Any | None = None,
) -> dict[str, tuple[Any, Any]]:
    """Validate and apply a config update to live *settings*.

    1. Calls :func:`validate_config_update` — raises on invalid input.
    2. Mutates *settings* in-place for each updated field.
    3. Calls *setter(settings)* if provided (e.g. to swap ``app.state.settings``).
    4. Audit-logs ``{key: (old, new)}`` via the ``robotsix_mill`` logger.
    5. Returns the audit map.

    NEVER mutates *settings* on invalid input — validation runs first.
    """
    audit = validate_config_update(settings, updates)

    # Apply: mutate settings in-place for each validated key.
    for key, (old_val, new_val) in audit.items():
        setattr(settings, key, new_val)
        logger.info(
            "config-set: %s = %r → %r",
            key,
            _redact_if_secret(key, old_val),
            _redact_if_secret(key, new_val),
        )

    # Propagate via optional setter (e.g. app.state.settings = settings).
    if setter is not None:
        setter(settings)

    return audit


def _redact_if_secret(key: str, value: Any) -> str:
    """Redact a value for audit-logging when *key* is a secret field."""
    if _is_secret_key(key):
        return "***"
    return repr(value)
