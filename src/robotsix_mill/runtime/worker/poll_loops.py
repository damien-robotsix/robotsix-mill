"""Worker poll loops — CI monitor, Dependabot ingest, and DB maintenance.

Defines ``PollLoopsMixin``, a mixin for the ``Worker`` class that
provides event-driven background poll loops: CI status monitoring,
Dependabot PR ingestion, database vacuum/cleanup, credit-balance
checks, timeout escalation, and periodic-workflow scheduling.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ...config import RepoConfig, get_repos_config
from ..run_registry import RunRegistry
from ._base import _WorkerBase

# The underscore names are re-exports: these helpers moved to
# ``poll_loops_ci`` / ``poll_loops_dependabot`` but stay importable from the
# original ``poll_loops`` path.
from .poll_loops_ci import (  # noqa: F401
    _CI_FAILING_CONCLUSIONS,
    _CI_LOG_EMBED_MAX_CHARS,
    _CI_LOG_FETCH_ATTEMPTS,
    _CI_LOG_FETCH_BACKOFF_CAP_SECONDS,
    _CI_LOG_FETCH_BACKOFF_SECONDS,
    _CI_LOG_FETCH_MAX_DEFERRALS,
    _CI_TITLE_PREFIX_RE,
    _REDISPATCH_MAX_ATTEMPTS,
    _REDISPATCH_MIN_INTERVAL_SECONDS,
    PollLoopsCiMixin,
    _ci_log_body_parts,
    _title_names_workflow,
)
from .poll_loops_dependabot import (  # noqa: F401
    PollLoopsDependabotMixin,
    _dependabot_body,
    _dependabot_title,
)

log = logging.getLogger("robotsix_mill.worker")


class PollLoopsMixin(PollLoopsCiMixin, PollLoopsDependabotMixin, _WorkerBase):
    """Mixin that provides event-driven background poll loops for the ``Worker``.

    The mixin runs multiple periodic poll loops — CI monitor, Dependabot
    alert ingest, database maintenance, credit-balance checks, timeout
    escalation, and per-repo periodic-workflow scheduling — on an
    approximately 60-second tick via :meth:`_start_poll_loop_pass`. Each loop
    is independently gated by a settings flag and uses deterministic
    stagger/jitter so passes don't fire simultaneously after a restart.
    """

    def _registry_for(self, repo_config) -> RunRegistry | None:
        """The RunRegistry a periodic pass should read/write for *repo_config*.

        Per-repo registry (``run_registries[board_id]``) when available so the
        run is recorded in — and its cadence measured from — the same
        ``<data_dir>/<board_id>/runs.json`` the per-repo /runs API serves.
        Falls back to the default registry for board-less ticks.
        """
        if repo_config is not None and self.run_registries:
            return self.run_registries.get(repo_config.board_id, self.run_registry)
        return self.run_registry

    def _initial_delay(
        self, kind: str, interval: int, repo_id: str = "", registry=None
    ) -> float:
        """Return the seconds to sleep before the first periodic pass.

        Queries ``RunRegistry.most_recent(kind, repo_id)`` to decide:
        - No registry → full ``interval`` (preserves current behaviour).
        - Never run (``None``) → 1.0 s + jitter.
        - Last run overdue (elapsed >= interval) → 1.0 s + jitter.
        - Otherwise → ``interval - elapsed`` + jitter.

        A deterministic per-kind jitter is added to stagger periodic agents
        so they don't all fire simultaneously after a process restart.
        The jitter is derived from ``hashlib.md5(kind)`` modulo a cap so
        every agent kind gets a stable, different offset.

        *repo_id* scopes the lookup to one repo's own history. Per-repo loops
        (the periodic-workflow + bespoke supervisors) MUST pass it: without it
        ``most_recent`` returns the newest run of *kind* across ALL repos, so a
        repo that has never run the agent inherits another repo's recent
        timestamp and waits a near-full interval before its first run — every
        restart resetting that wait. With a 24 h interval + frequent restarts
        the first run then never fires (the symptom: audit never ran on
        robotsix-llmio because mill's daily audit kept the shared clock warm).

        *registry* selects which store to read (per-repo loops pass the repo's
        own registry so the cadence matches where the run is recorded); defaults
        to the worker's fallback registry.
        """
        reg = registry if registry is not None else self.run_registry
        if reg is None:
            base = float(interval)
        else:
            entry = reg.most_recent(kind, repo_id=repo_id or None)
            if entry is None:
                base = 1.0
            else:
                try:
                    last_ts = datetime.fromisoformat(entry["started_at"])
                    elapsed = (datetime.now(UTC) - last_ts).total_seconds()
                except Exception:
                    base = 1.0
                else:
                    base = 1.0 if elapsed >= interval else interval - elapsed

        # Deterministic per-kind stagger so periodic agents don't all fire
        # simultaneously after a restart.  Cap at min(interval//12, 1h) so the
        # offset is meaningful (spreads agents across minutes) but never exceeds
        # an hour.  Plus up to 60 s of random jitter so two agents that happen
        # to hash-close don't fire in lockstep.
        kind_hash = int(
            hashlib.md5(kind.encode(), usedforsecurity=False).hexdigest(), 16
        )
        stagger_cap = max(60, min(interval // 12, 3600))  # 1 min .. 1 hour
        stagger = (kind_hash % stagger_cap) + random.uniform(0, 60)
        return base + stagger

    _PERIODIC_POLL_TICK_SECONDS = 60

    def _find_config_clone_dir(self, repo_config) -> Path | None:
        """Return any existing clone of *repo_config* usable for
        scheduler-time YAML lookup, or ``None``.

        The scheduler needs to read ``<clone>/.robotsix-mill/agents/
        <name>.yaml`` to honour per-repo periodic overrides, but the
        scheduler does not own a clone — it piggybacks on whichever
        worker clone exists already (bespoke supervisor, or any
        ``<agent>_workspace/repo`` left behind by an earlier run).

        Priority: periodic_workspace (legacy: bespoke_workspace) > any
        *_workspace/repo. When no clone exists yet the loader falls back to
        the built-in YAML.
        """
        if repo_config is None:
            return None
        base = Path(self.ctx.settings.data_dir) / repo_config.repo_id
        if not base.is_dir():
            return None
        periodic = base / "periodic_workspace" / "repo"
        if (periodic / ".git").exists():
            return periodic
        bespoke = base / "bespoke_workspace" / "repo"  # legacy name (pre-rename)
        if (bespoke / ".git").exists():
            return bespoke
        try:
            for child in base.iterdir():
                if (
                    child.is_dir()
                    and child.name.endswith("_workspace")
                    and (child / "repo" / ".git").exists()
                ):
                    return child / "repo"
        except OSError:
            pass
        return None

    def _resolve_periodic_schedule(
        self,
        label: str,
        repo_config,
        settings_interval_attr: str,
    ) -> tuple[bool, int]:
        """Resolve ``(enabled, interval_seconds)`` for *label* on *repo_config*.

        Lookup order for each field:
          1. The agent YAML loaded via :func:`load_periodic_agent_definition`
             (clone-side override wins over built-in).
          2. The Settings field of the matching name as a fallback.

        A job is disabled when its interval is 0.  Interval is clamped
        to >= 60s when non-zero.
        """
        from ...agents.yaml_loader import load_periodic_agent_definition

        settings = self.ctx.settings
        yaml_name = label.replace("-", "_")
        repo_dir = self._find_config_clone_dir(repo_config)
        try:
            definition = load_periodic_agent_definition(yaml_name, repo_dir)
        except FileNotFoundError:
            definition = None

        # Interval: YAML > Settings.
        interval = None
        if definition and definition.interval_seconds is not None:
            interval = definition.interval_seconds
        else:
            interval = getattr(settings, settings_interval_attr, None)
        interval = int(interval or 86400)

        # interval == 0 disables the job.
        if interval <= 0:
            return False, 0

        # Enabled: YAML > Settings (interval_seconds > 0 is the
        # canonical on-switch; the YAML ``enabled`` flag is a
        # secondary override for per-repo opt-out).
        enabled = True
        if definition and definition.enabled is not None:
            enabled = bool(definition.enabled)
        return enabled, max(60, interval)

    async def _run_bespoke_loop(
        self,
        repo_config: RepoConfig,
        definition,
        clone_dir,
    ) -> None:
        """Periodic loop for one bespoke definition.

        Sleeps the YAML's ``interval_seconds`` between passes, then
        invokes :func:`~..bespoke_runner.run_bespoke_pass` against the
        supervisor's clone. Failures in one pass log + continue; the
        loop only exits via cancellation.
        """
        from ...agents.runners import bespoke_runner
        from .. import tracing

        interval = max(60, definition.interval_seconds)
        label = f"bespoke:{definition.name}"
        # Honour the persisted last-run timestamp so a restarted mill
        # doesn't re-fire every bespoke immediately. Scope to this repo so a
        # repo that has never run this bespoke fires promptly instead of
        # inheriting another repo's recent timestamp.
        initial = self._initial_delay(
            label,
            interval,
            repo_id=repo_config.repo_id,
            registry=self._registry_for(repo_config),
        )
        await asyncio.sleep(initial)
        while True:
            run_id = None
            session_id = tracing.make_session_id(label, repo_config)
            try:
                log.info(
                    "Starting bespoke pass %r for repo %s",
                    definition.name,
                    repo_config.repo_id,
                )
                if self.run_registry:
                    run_id = self.run_registry.start(
                        label,
                        repo_id=repo_config.repo_id,
                    )
                with tracing.start_ticket_root_span(
                    session_id,
                    label,
                    repo_config=repo_config,
                ):
                    result = await asyncio.to_thread(
                        bespoke_runner.run_bespoke_pass,
                        session_id=session_id,
                        definition=definition,
                        repo_config=repo_config,
                        repo_dir=clone_dir,
                    )
                log.info(
                    "Bespoke %s/%s completed, created %d draft(s)",
                    repo_config.repo_id,
                    definition.name,
                    len(result.drafts_created),
                )
                if self.run_registry and run_id:
                    summary = f"Created {len(result.drafts_created)} drafts"
                    self.run_registry.finish_ok(run_id, summary)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception(
                    "bespoke %s/%s pass failed",
                    repo_config.repo_id,
                    definition.name,
                )
                if self.run_registry and run_id:
                    self.run_registry.finish_error(run_id, str(e))
            await asyncio.sleep(interval)

    # Schedule-only periodic workflows (no prompt yaml / own runner). Their
    # module-level ``run_<name>_pass(session_id, repo_config)`` stub is used
    # directly — no definition override is applicable.
    _SCHEDULE_ONLY_RUNNERS: dict[str, str] = {
        "trace_review": "robotsix_mill.agents.runners.trace_review_runner:run_trace_review_pass",
        "config_sync": "robotsix_mill.agents.runners.periodic_runner:run_config_sync_pass",
        "member_sync": "robotsix_mill.agents.runners.member_sync_runner:run_member_sync_pass",
        "data_dir_gc": (
            "robotsix_mill.agents.runners.data_dir_gc:run_data_dir_gc_pass"
        ),
        "credit_balance": (
            "robotsix_mill.agents.runners.credit_balance_runner:run_credit_balance_pass"
        ),
        "diagnostic": "robotsix_mill.agents.runners.diagnostic_runner:run_diagnostic_pass",
        "pin_bump": "robotsix_mill.agents.runners.pin_bump_runner:run_pin_bump_pass",
        "roadmap_sync": "robotsix_mill.agents.runners.roadmap_sync_runner:run_roadmap_sync_pass",
    }

    # LLM-agent periodic workflows that use custom runners (not the
    # standard run_periodic_pass + run_agent_pass path) but still
    # benefit from definition_override for presence-file support.
    _CUSTOM_LLM_AGENT_RUNNERS: dict[str, str] = {
        "repo_description_sync": (
            "robotsix_mill.agents.runners.repo_description_sync_runner:run_repo_description_sync_pass"
        ),
        "ci_prevention_rules": (
            "robotsix_mill.agents.runners.ci_prevention_rules_runner:run_ci_prevention_rules_pass"
        ),
    }

    def _build_periodic_workflow_runner(self, wf):
        """Return a ``runner_fn(session_id, repo_config)`` for *wf*, or None.

        ``llm_agent`` → a closure that runs the matching periodic pass with
        the merged definition threaded in as ``definition_override``.
        Custom LLM-agent runners (``_CUSTOM_LLM_AGENT_RUNNERS``) receive
        ``definition_override`` as a keyword argument so that presence-file
        overrides take effect.
        ``schedule_only`` → the workflow's module-level pass stub.
        """
        if wf.kind in ("llm_agent", "mill_only"):
            # Check for custom LLM-agent runners first (e.g. repo_description_sync
            # which has its own runner outside the standard PERIODIC_PASS_CONFIGS
            # path but still needs definition_override for presence-file support).
            custom_path = self._CUSTOM_LLM_AGENT_RUNNERS.get(wf.name)
            if custom_path is not None:
                import importlib

                definition = wf.definition
                mod_path, attr = custom_path.rsplit(":", 1)
                runner = getattr(importlib.import_module(mod_path), attr)

                def _run(*, session_id: Any, repo_config: Any) -> Any:
                    return runner(
                        session_id, repo_config, definition_override=definition
                    )

                return _run

            from ...config import Settings

            definition = wf.definition

            from ...agents.runners.periodic_runner import (
                PERIODIC_PASS_CONFIGS,
                run_periodic_pass,
            )

            cfg = PERIODIC_PASS_CONFIGS.get(wf.name)
            if cfg is None:
                return None

            def _run(*, session_id, repo_config):
                return run_periodic_pass(
                    session_id,
                    repo_config,
                    cfg,
                    settings=Settings(),
                    definition_override=definition,
                )

            return _run
        if wf.kind == "schedule_only":
            import importlib

            path = self._SCHEDULE_ONLY_RUNNERS.get(wf.name)
            if path is None:
                return None
            mod_path, attr = path.rsplit(":", 1)
            return getattr(importlib.import_module(mod_path), attr)
        return None

    async def _run_periodic_workflow_loop(self, repo_config, wf, clone_dir) -> None:
        """Periodic loop for one resolved per-repo periodic workflow.

        Sleeps the resolved interval (file override > Settings fallback) and
        fires the matching runner. Failures log + continue; exits only via
        cancellation by the supervisor.
        """
        settings = self.ctx.settings
        label = wf.name
        interval = wf.interval_seconds
        if interval is None:
            interval = getattr(settings, f"{wf.name}_interval_seconds", 86400)
        interval = max(60, int(interval or 86400))

        runner_fn = self._build_periodic_workflow_runner(wf)
        if runner_fn is None:
            log.warning(
                "periodic workflow %s (%s): no runner for kind %r — not scheduling",
                wf.name,
                repo_config.repo_id,
                wf.kind,
            )
            return

        await asyncio.sleep(
            self._initial_delay(
                label,
                interval,
                repo_id=repo_config.repo_id,
                registry=self._registry_for(repo_config),
            )
        )
        while True:
            await self._fire_periodic_pass(label, runner_fn, repo_config)
            await asyncio.sleep(interval)

    def _has_periodic_presence(self, repo_config, label: str) -> bool:
        """True when *repo_config*'s clone ships ``.robotsix-mill/periodic/
        <label>.yaml`` — meaning the periodic supervisor owns that workflow
        for this repo and the legacy flag-based loop must NOT also fire it.
        """
        from ...agents.periodic_loader import PERIODIC_DIR

        clone = self._find_config_clone_dir(repo_config)
        if clone is None:
            return False
        name = label.replace("-", "_")
        return clone.joinpath(*PERIODIC_DIR, f"{name}.yaml").is_file()

    async def _langfuse_cleanup_poll_loop(self) -> None:
        """Periodic Langfuse trace cleanup: keeps the shared workspace project
        at most ``langfuse_cleanup_max_traces`` rows by deleting the oldest.

        Global pass (non-per-repo): all managed repos share one Langfuse project
        (credentials always come from global Secrets), so one pass per interval
        is sufficient. Gated by ``settings.langfuse_cleanup_interval_seconds`` (0 = disabled) via
        ``_start_poll_loop_pass``. Pure HTTP, no LLM.
        """
        settings = self.ctx.settings
        interval = max(3600, settings.langfuse_cleanup_interval_seconds)
        initial = self._initial_delay("langfuse-cleanup", interval)
        await asyncio.sleep(initial)
        while True:
            try:
                from ...agents.runners.langfuse_cleanup_runner import (
                    run_langfuse_cleanup_pass,
                )

                result = await asyncio.to_thread(
                    run_langfuse_cleanup_pass,
                    settings=settings,
                    repo_config=None,
                    max_traces=settings.langfuse_cleanup_max_traces,
                )
                if result.traces_deleted > 0:
                    log.info(
                        "langfuse-cleanup: deleted %d of %d traces (cap %d)",
                        result.traces_deleted,
                        result.traces_before,
                        settings.langfuse_cleanup_max_traces,
                    )
            except Exception:
                log.exception("langfuse-cleanup poll failed")
            await asyncio.sleep(interval)

    async def _token_metrics_aggregation_poll_loop(self) -> None:
        """Periodic token-metrics aggregation: computes stage×model per-call
        token percentiles from the shared Langfuse project and writes a
        compact daily JSON snapshot.

        Global pass (non-per-repo): all managed repos share one Langfuse
        project, so one pass per interval is sufficient. Gated by
        ``settings.token_metrics_aggregation_interval_seconds`` (0 = disabled) via
        ``_start_poll_loop_pass``. Pure HTTP + file I/O, no LLM.
        """
        settings = self.ctx.settings
        interval = max(3600, settings.token_metrics_aggregation_interval_seconds)
        initial = self._initial_delay("token-metrics-aggregation", interval)
        await asyncio.sleep(initial)
        while True:
            try:
                from ...agents.runners.token_metrics_runner import (
                    run_token_metrics_aggregation,
                )

                result = await asyncio.to_thread(
                    run_token_metrics_aggregation,
                    settings=settings,
                    window_seconds=settings.token_metrics_aggregation_window_seconds,
                )
                if result.written:
                    log.info(
                        "token-metrics: %d steps across %d traces -> %s",
                        result.steps_seen,
                        result.traces_seen,
                        result.path,
                    )
                else:
                    log.debug("token-metrics: pass produced no snapshot")
            except Exception:
                log.exception("token-metrics-aggregation poll failed")
            await asyncio.sleep(interval)

    async def _timeout_escalation_poll_loop(self) -> None:
        """Periodic timeout-escalation: detects AWAITING_USER_REPLY tickets
        stuck beyond the threshold and escalates them to BLOCKED.

        Pure DB query + state transition — no AI agent, no Langfuse tracing.
        Global pass (non-per-repo): AWAITING_USER_REPLY tickets are
        board-agnostic.
        """
        settings = self.ctx.settings
        interval = max(60, settings.timeout_escalation_interval_seconds)
        initial = self._initial_delay("timeout-escalation", interval)
        await asyncio.sleep(initial)
        while True:
            try:
                from ...agents.runners.timeout_escalation_runner import (
                    run_timeout_escalation,
                )

                result = await asyncio.to_thread(
                    run_timeout_escalation,
                    settings,
                )
                log.info(
                    "timeout-escalation: pass complete — escalated=%d skipped=%d",
                    result.get("escaped", 0),
                    result.get("skipped", 0),
                )
            except Exception:
                log.exception("timeout-escalation poll failed")
            await asyncio.sleep(interval)

    async def _upstream_ci_recovery_poll_loop(self) -> None:
        """Periodic upstream-CI recovery: resumes tickets that ci_fix parked
        because the target branch was red, once that branch is green again.

        Pure forge status reads + state transitions — no AI agent, no
        Langfuse tracing.  Global pass (non-per-repo): it scans every
        board's BLOCKED tickets and only touches the ones carrying the
        upstream-CI block marker.
        """
        settings = self.ctx.settings
        interval = max(60, settings.upstream_ci_recovery_interval_seconds)
        initial = self._initial_delay("upstream-ci-recovery", interval)
        await asyncio.sleep(initial)
        while True:
            try:
                from ...agents.runners.upstream_ci_recovery_runner import (
                    run_upstream_ci_recovery,
                )

                result = await asyncio.to_thread(
                    run_upstream_ci_recovery,
                    settings,
                )
                log.info(
                    "upstream-ci-recovery: pass complete — resumed=%d "
                    "still_parked=%d skipped=%d",
                    result.get("resumed", 0),
                    result.get("still_parked", 0),
                    result.get("skipped", 0),
                )
            except Exception:
                log.exception("upstream-ci-recovery poll failed")

            # Same mechanism, account-scoped: escalate ONCE fleet-wide and
            # auto-resume tickets parked on the GitHub account/billing block.
            try:
                from ...agents.runners.infra_account_block_runner import (
                    run_infra_account_block_recovery,
                )

                ab_result = await asyncio.to_thread(
                    run_infra_account_block_recovery,
                    settings,
                )
                log.info(
                    "infra-account-block: pass complete — escalated=%d "
                    "resumed=%d still_parked=%d skipped=%d",
                    ab_result.get("escalated", 0),
                    ab_result.get("resumed", 0),
                    ab_result.get("still_parked", 0),
                    ab_result.get("skipped", 0),
                )
            except Exception:
                log.exception("infra-account-block poll failed")
            await asyncio.sleep(interval)

    async def _blocked_auto_resume_poll_loop(self) -> None:
        """Periodic blocked auto-resume: retries BLOCKED tickets whose latest
        block note is resumable (provider failure, timeout, budget exhausted,
        missing clone…), once per ticket after a cooldown.

        Deterministic — history reads + ``resume_blocked`` — no AI agent, no
        tracing.  Global pass across every board.
        """
        settings = self.ctx.settings
        interval = max(60, settings.blocked_auto_resume_interval_seconds)
        initial = self._initial_delay("blocked-auto-resume", interval)
        await asyncio.sleep(initial)
        while True:
            try:
                from ...agents.runners.blocked_auto_resume_runner import (
                    run_blocked_auto_resume,
                )

                result = await asyncio.to_thread(run_blocked_auto_resume, settings)
                log.info(
                    "blocked-auto-resume: pass complete — resumed=%d cooling=%d "
                    "budget_exhausted=%d not_matched=%d cycles_escalated=%d",
                    result.get("resumed", 0),
                    result.get("cooling", 0),
                    result.get("budget_exhausted", 0),
                    result.get("not_matched", 0),
                    result.get("cycles_escalated", 0),
                )
            except Exception:
                log.exception("blocked-auto-resume poll failed")
            await asyncio.sleep(interval)

    async def _ci_auto_close_poll_loop(self) -> None:
        """Periodic ci auto-close: re-checks open source=ci tickets against
        the CURRENT main head and closes the ones whose workflow has since
        turned green (red-then-fixed). Deterministic — forge reads +
        service.transition/mark_done, no AI agent, no tracing. Global pass
        across every board.
        """
        settings = self.ctx.settings
        interval = max(60, settings.ci_auto_close_interval_seconds)
        initial = self._initial_delay("ci-auto-close", interval)
        await asyncio.sleep(initial)
        while True:
            try:
                from ...agents.runners.ci_auto_close_runner import (
                    run_ci_auto_close,
                )

                result = await asyncio.to_thread(run_ci_auto_close, settings)
                log.info(
                    "ci-auto-close: pass complete — checked=%d closed=%d skipped=%d",
                    result.get("checked", 0),
                    result.get("closed", 0),
                    result.get("skipped", 0),
                )
            except Exception:
                log.exception("ci-auto-close poll failed")
            await asyncio.sleep(interval)

    async def _config_pin_drift_poll_loop(self) -> None:
        """Periodic config pin-drift check: reports pinned settings that
        shadow a changed code default.

        Pure file read + model introspection — no AI agent, no tracing, and
        log-only by design. The board is already over-full; a generator that
        filed a ticket per drifting key would worsen the very problem this
        pass exists to surface.
        """
        settings = self.ctx.settings
        if settings.config_pin_drift_interval_seconds <= 0:
            return
        interval = max(60, settings.config_pin_drift_interval_seconds)
        initial = self._initial_delay("config-pin-drift", interval)
        await asyncio.sleep(initial)
        while True:
            try:
                from ...agents.runners.config_pin_drift_runner import (
                    run_config_pin_drift,
                )

                result = await asyncio.to_thread(run_config_pin_drift, settings)
                log.info(
                    "config-pin-drift: pass complete — checked=%d drifted=%d",
                    result.get("checked", 0),
                    result.get("drifted", 0),
                )
            except Exception:
                log.exception("config-pin-drift poll failed")
            await asyncio.sleep(interval)

    async def _db_maintenance_poll_loop(self) -> None:
        """Periodic DB maintenance: archive purge + per-ticket event cap +
        PRAGMA optimize.  Runs per-board across all registered repos.

        Pure DB — no LLM, no Langfuse tracing.
        """
        from ...core.service import TicketService

        settings = self.ctx.settings
        interval = max(3600, settings.db_maintenance_interval_seconds)
        initial = self._initial_delay("db-maintenance", interval)
        await asyncio.sleep(initial)
        while True:
            # Collect all board IDs.
            boards: list[str] = [self._META_BOARD]
            if self.ctx.service.board_id not in boards:
                boards.append(self.ctx.service.board_id)
            try:
                for rc in get_repos_config().repos.values():
                    if rc.board_id and rc.board_id not in boards:
                        boards.append(rc.board_id)
            except Exception:
                pass

            for board_id in boards:
                label = board_id
                try:
                    svc = (
                        self.ctx.service
                        if board_id == self.ctx.service.board_id
                        else TicketService(settings, board_id=board_id)
                    )
                    summary = await asyncio.to_thread(svc.db_maintenance_pass)
                    if any(summary.values()):
                        log.info(
                            "db-maintenance: %s — archived_purged=%d "
                            "events_pruned=%d tickets_pruned=%d "
                            "orphans_reconciled=%d",
                            label,
                            summary["archived_purged"],
                            summary["events_pruned"],
                            summary["tickets_pruned"],
                            summary["orphans_reconciled"],
                        )
                except Exception:
                    log.exception("db-maintenance poll failed for %s", label)
            await asyncio.sleep(interval)

    async def _credit_balance_poll_loop(self) -> None:
        """Periodic OpenRouter credit-balance poll.

        Queries ``GET /api/v1/credits`` and sets/clears the board-level
        low-credit warning.  Skips silently when no OpenRouter key is
        configured.  Hourly cadence with deterministic stagger + jitter.
        """
        settings = self.ctx.settings
        interval = max(60, settings.low_credit_poll_interval_seconds)
        initial = self._initial_delay("credit-balance", interval)
        await asyncio.sleep(initial)
        while True:
            try:
                from ...agents.runners.credit_balance_runner import (
                    run_credit_balance_check,
                )

                await asyncio.to_thread(run_credit_balance_check, settings)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("credit-balance poll failed")
            await asyncio.sleep(interval)

    def _start_poll_loop_pass(
        self,
        label: str,
        poll_loop_fn,
        task_attr: str,
        log_msg: str | None = None,
        log_args: tuple[Any, ...] = (),
    ) -> None:
        """Start a dedicated poll-loop periodic pass if its settings flag
        (derived from *label*) is on and the task attribute is still ``None``.

        ``poll_loop_fn`` is a zero-argument async callable (typically a
        bound method like ``self._trace_health_poll_loop``).
        """
        interval_attr = label.replace("-", "_") + "_interval_seconds"
        if (
            getattr(self.ctx.settings, interval_attr, 0) > 0
            and getattr(self, task_attr) is None
        ):
            setattr(
                self,
                task_attr,
                asyncio.create_task(poll_loop_fn()),
            )
            if log_msg is not None:
                log.info(log_msg, *log_args)
