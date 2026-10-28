"""One-shot operator escalation for a repository-setting CI block.

When ``stages/ci_fix`` detects a CI failure that only a GitHub repository
setting can fix (Dependency graph, code scanning — see
:func:`~robotsix_mill.stages.ci_infra_block.classify_repo_setting_block`),
it parks the ticket ``BLOCKED`` and calls :func:`escalate_repo_setting_block`.
The escalation goes through the same operator channel as the infra
account-block runner (``user_chat``, falling back to a WARNING log) and is
deduped per ``(repo, setting)`` via a marker file under ``data_dir`` so a
ticket that re-blocks on resume does not re-escalate.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ...stages.ci_infra_block import repo_settings_url

if TYPE_CHECKING:
    from ...config import Settings

log = logging.getLogger("robotsix_mill.repo_setting_block")

# escalate(repo, setting, ticket_id) — inject in tests.
EscalateFn = Callable[[str, str, str], None]


def _marker_path(settings: Settings) -> Any:
    return settings.data_dir / "repo_setting_block_escalations.json"


def _load_marker(settings: Settings) -> dict[str, Any]:
    try:
        data = json.loads(_marker_path(settings).read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _default_escalate(
    repo: str, setting: str, ticket_id: str, message: str | None = None
) -> None:
    message = message or (
        f"OPERATOR ESCALATION — repository setting required on {repo}: enable "
        f"'{setting}' at {repo_settings_url(repo)} (Settings > Code security). "
        f"CI cannot pass until it is on; ticket {ticket_id} is BLOCKED and "
        "will not auto-resume. After enabling it, resume the ticket "
        "(/transition ready) to re-run CI."
    )
    from ...notify.user_chat import escalate_to_user_chat

    if escalate_to_user_chat(
        message,
        severity="warning",
        category="repo_setting_block",
        dedup_key=f"repo_setting_block:{repo}:{setting}",
    ):
        log.info("repo_setting_block: operator escalation routed to user_chat")
        return
    log.warning("%s", message)


def escalate_repo_setting_block(
    settings: Settings,
    repo: str,
    setting: str,
    ticket_id: str,
    *,
    escalate: EscalateFn | None = None,
    message: str | None = None,
) -> bool:
    """Escalate *setting* on *repo* once; return True when an escalation fired.

    *message* overrides the default (Settings > Code security) wording.
    """
    key = f"{repo}|{setting}"
    marker = _load_marker(settings)
    if key in marker:
        return False
    try:
        if escalate is not None:
            escalate(repo, setting, ticket_id)
        elif message is not None:
            _default_escalate(repo, setting, ticket_id, message)
        else:
            _default_escalate(repo, setting, ticket_id)
    except Exception:
        log.exception("repo_setting_block: escalation failed for %s", key)
        return False
    marker[key] = {"at": datetime.now(UTC).isoformat(), "ticket": ticket_id}
    path = _marker_path(settings)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(marker))
    except Exception:
        log.exception("repo_setting_block: could not persist escalation marker")
    return True
