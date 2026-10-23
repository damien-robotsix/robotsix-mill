"""Top-level bespoke-agent pass entry point.

Mirrors the audit-runner pattern: read memory, invoke agent, write
returned memory verbatim, create draft tickets with a per-agent
``source: bespoke:<name>`` so dedup scopes to the agent that filed
them.

The pass scope is **one definition at a time**. Discovery of which
definitions exist in ``<clone>/.robotsix-mill/agents/`` and scheduling
each one's cadence is owned by the worker (phase 4) — this module
just executes a definition when handed one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from ...agents import bespoke as _bespoke_agent
from ...agents.bespoke_loader import BespokeAgentDefinition
from ...config import RepoConfig, Settings
from ...core.service import TicketService
from .pass_runner import run_agent_pass

log = logging.getLogger("robotsix_mill.bespoke")


@dataclass
class BespokePassResult:
    """Result of running one bespoke-agent pass.

    ``source_label`` is the full ``bespoke:<name>`` string used as the
    source kind of created tickets; callers (worker / run_registry)
    use it as a stable identity for the pass.
    """

    source_label: str
    updated_memory: str
    drafts_created: list[dict[str, Any]]
    session_id: str = ""


def _memory_file_for(
    settings: Settings,
    board_id: str,
    name: str,
) -> Path:
    """Return the bespoke memory-ledger path for *name* on *board_id*.

    Lives at ``<data_dir>/<board_id>/bespoke_<name>_memory.md`` so each
    repo's bespoke ledger is isolated from mill core's per-agent
    ledgers (audit_memory.md, health_memory.md, …) and from other
    repos' ledgers.
    """
    base = settings.data_dir / board_id if board_id else settings.data_dir
    base.mkdir(parents=True, exist_ok=True)
    return base / f"bespoke_{name}_memory.md"


def _route_drafts(
    out: _bespoke_agent.BespokeResult,
    *,
    host_repo_id: str,
    agent_name: str,
) -> _bespoke_agent.BespokeResult:
    """Validate ``draft_repo_ids`` and stamp cross-board drafts.

    Unknown repo ids fall back to the host board (empty entry) with a
    warning. Drafts routed to another repo's board get an
    ``Audited from: <host> / <agent>`` line so the target board knows
    which audit filed them.
    """
    if not any(out.draft_repo_ids):
        return out
    from ...config import get_repos_config

    known = get_repos_config().repos.keys()
    routed: list[str] = []
    for i, raw in enumerate(out.draft_repo_ids):
        target = (raw or "").strip()
        title = out.draft_titles[i] if i < len(out.draft_titles) else ""
        if target and target not in known:
            log.warning(
                "periodic drafts: unknown target repo '%s' for '%s' — "
                "filing on host board",
                target,
                title,
            )
            target = ""
        if target == host_repo_id:
            target = ""
        if target and i < len(out.draft_bodies):
            out.draft_bodies[i] += f"\n\nAudited from: {host_repo_id} / {agent_name}"
        routed.append(target)
    out.draft_repo_ids = routed
    return out


def run_bespoke_pass(
    session_id: str,
    *,
    definition: BespokeAgentDefinition,
    repo_config: RepoConfig | None,
    repo_dir: Path | None,
) -> BespokePassResult:
    """Execute one bespoke-agent pass.

    Args:
        session_id: Langfuse session id from the scheduler.
        definition: The bespoke-agent definition (already loaded from
            ``<clone>/.robotsix-mill/agents/<name>.yaml``).
        repo_config: Per-repo config; drives the target board and
            memory-ledger location. ``None`` falls back to the default
            board (single-repo / legacy).
        repo_dir: Optional path to the local repository clone — the
            same clone the rest of the pass uses. The bespoke agent
            inspects this with read-only tools.

    Returns:
        A :class:`BespokePassResult` carrying the per-agent source
        label, the updated memory text, and any drafts created.
    """
    settings = Settings()
    if repo_config is None:
        raise ValueError(
            "run_bespoke_pass: repo_config is required — "
            "configure at least one repo in config/repos.yaml."
        )
    board_id = repo_config.board_id
    source_label = f"bespoke:{definition.name}"

    service = TicketService(settings, board_id=board_id)
    memory_file = _memory_file_for(settings, board_id, definition.name)

    log.info(
        "bespoke pass %r starting (session %s, repo %s)",
        definition.name,
        session_id,
        board_id,
    )
    run_agent_fn = partial(
        _bespoke_agent.run_bespoke_agent,
        definition=definition,
        repo_dir=repo_dir,
    )

    def agent_fn(**kwargs: Any) -> _bespoke_agent.BespokeResult:
        return _route_drafts(
            run_agent_fn(**kwargs),
            host_repo_id=repo_config.repo_id,
            agent_name=definition.name,
        )

    result = run_agent_pass(
        agent_fn=agent_fn,
        memory_file=memory_file,
        source_label=source_label,  # type: ignore[arg-type]
        service=service,
        settings=settings,
        origin_session=session_id,
        # Bespoke passes declare no cap of their own.
        max_drafts=settings.periodic_max_drafts_per_run,
        repo_dir=repo_dir,
    )

    return BespokePassResult(
        source_label=source_label,
        updated_memory=result.updated_memory,
        drafts_created=result.drafts_created,
        session_id=session_id,
    )
