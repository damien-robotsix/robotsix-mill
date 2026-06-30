from __future__ import annotations

import asyncio
import hashlib
import logging
import random  # noqa: S311 — only used for startup jitter, not security-critical
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...config import RepoConfig, Settings, get_repos_config, target_branch_for
from .. import tracing

from ._base import _WorkerBase
from .epic import _branch_is_stale

log = logging.getLogger("robotsix_mill.worker")


# ---------------------------------------------------------------------------
# Member-sync event-trigger state helpers
# ---------------------------------------------------------------------------
#
# The supervisor fires member-sync out-of-band when a managed repo's vcs2l
# manifest (``repos.yaml``) content changes between cycles. There is no
# inotify/webhook infrastructure — change-detection is poll-cycle
# content-hash only. State persists at ``<data_dir>/<repo_id>/
# member_sync_repos_hash`` (mirrors trace_review_runner's tiny state-file
# helpers). A missing ``repos.yaml`` hashes to ``""`` (a sentinel that
# never triggers a fire).


def _member_sync_hash_path(settings: Settings, repo_id: str) -> Path:
    return settings.data_dir / repo_id / "member_sync_repos_hash"


def _hash_repos_yaml(clone_dir: Path) -> str:
    """Return the sha256 of ``<clone_dir>/repos.yaml``, or ``""`` when the
    file is absent/unreadable (the sentinel — never fires a sync)."""
    p = Path(clone_dir) / "repos.yaml"
    if not p.exists():
        return ""
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return ""


def _load_repos_yaml_hash(settings: Settings, repo_id: str) -> str:
    p = _member_sync_hash_path(settings, repo_id)
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _save_repos_yaml_hash(settings: Settings, repo_id: str, value: str) -> None:
    p = _member_sync_hash_path(settings, repo_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(value, encoding="utf-8")


class PeriodicPassesMixin(_WorkerBase):
    async def _run_periodic_pass_per_repo(
        self,
        label: str,
        runner_fn,
        settings_interval_attr: str,
        per_repo_flag: str | None = None,
        settings_enabled_attr: str | None = None,
    ) -> None:
        """Shared per-repo periodic pass loop.

        Each tick (every :attr:`_PERIODIC_POLL_TICK_SECONDS`) the
        scheduler iterates registered repos and decides per-repo
        whether to fire — driven by the agent YAML's
        ``interval_seconds`` + ``enabled`` fields (with override at
        ``<clone>/.robotsix-mill/agents/<name>.yaml``) and the
        Settings field of the matching name as a fallback.

        Args:
            label: Pass identifier (``"audit"``, ``"agent_check"``).
                Also used as the YAML filename after hyphens →
                underscores: ``"copy-paste"`` → ``copy_paste.yaml``.
            runner_fn: Callable accepting ``session_id=`` and
                ``repo_config=`` keywords; returns a result with a
                ``drafts_created`` field.
            settings_interval_attr: Settings field name used as the
                interval fallback (e.g. ``"audit_interval_seconds"``).
            per_repo_flag: Name of the RepoConfig bool field that
                gates this agent for each repo (e.g.
                ``"audit_periodic"``). Repos whose flag is False are
                skipped entirely.
            settings_enabled_attr: Settings field name used as the
                ``enabled`` fallback (e.g. ``"audit_periodic"``).
                ``None`` → assume enabled.
        """
        last_run_by_board: dict[str, datetime] = {}
        # Unseeded boards default to epoch so the first tick fires
        # them as soon as the cadence sleep elapses (matching the
        # legacy ``_initial_delay``'s "fire ASAP when no prior run"
        # semantic). Boards with a registry entry are seeded from it.
        default_seed = datetime(1970, 1, 1, tzinfo=timezone.utc)

        # Seed last-run timestamps from the run registry so a restart
        # doesn't re-fire every repo's pass immediately.
        if self.run_registry is not None:
            for entry in self.run_registry.list_all():
                if entry.get("kind") != label or entry.get("status") != "ok":
                    continue
                board_id = entry.get("repo_id", "") or ""
                if board_id in last_run_by_board:
                    continue
                ts_iso = entry.get("finished_at") or entry.get("started_at")
                if not ts_iso:
                    continue
                try:
                    last_run_by_board[board_id] = datetime.fromisoformat(ts_iso)
                except ValueError:
                    continue

        first_tick = True
        while True:
            # Sleep before checking. The first tick uses a short
            # settling delay plus a bounded random jitter so the
            # per-repo pass batch doesn't land exactly on the ~1s
            # boot spike (post-restart thundering herd). Subsequent
            # ticks use the poll cadence.
            first_delay = 1.0 + random.uniform(  # noqa: S311
                0, self.ctx.settings.startup_jitter_seconds
            )
            await asyncio.sleep(
                first_delay if first_tick else self._PERIODIC_POLL_TICK_SECONDS
            )
            first_tick = False
            try:
                repos = get_repos_config()
                repo_configs = list(repos.repos.values())
                if not repo_configs:
                    # Single-repo / no repos.yaml: tick the default.
                    repo_configs = [None]  # type: ignore[list-item]
                if per_repo_flag:
                    repo_configs = [
                        rc
                        for rc in repo_configs
                        # Opt-in (9cc9): a registered repo runs this agent only
                        # if its per-repo flag is set. ``rc is None`` is the
                        # single-repo / no-repos.yaml mode, which has no
                        # per-repo config to opt in with, so it still ticks
                        # (governed by the Settings-level master switch).
                        if rc is None or getattr(rc, per_repo_flag, False)
                    ]

                for repo_config in repo_configs:
                    board_id = repo_config.repo_id if repo_config else ""
                    # Presence wins over flag: if the repo ships a
                    # .robotsix-mill/periodic/<label>.yaml the periodic
                    # supervisor owns this agent for this repo — skip here so
                    # it never double-fires during the migration window.
                    if self._has_periodic_presence(repo_config, label):
                        continue
                    enabled, interval = self._resolve_periodic_schedule(
                        label,
                        repo_config,
                        settings_interval_attr,
                        settings_enabled_attr,
                    )
                    if not enabled:
                        continue

                    now = datetime.now(timezone.utc)
                    last = last_run_by_board.get(board_id, default_seed)
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                    if (now - last).total_seconds() < interval:
                        continue

                    await self._fire_periodic_pass(
                        label,
                        runner_fn,
                        repo_config,
                    )
                    last_run_by_board[board_id] = datetime.now(timezone.utc)
            except Exception:  # noqa: BLE001 — never let the poll die
                log.exception("%s scheduler tick failed", label)

    async def _tracked_to_thread(self, fn, *args, **kwargs):
        """Run *fn* in the default thread pool, tracking the call so
        :meth:`stop` can wait for it before tearing the loop down.

        Difference vs ``asyncio.to_thread``: the underlying future is
        wrapped in a task that's registered in ``_inflight_passes`` and
        ``shield``-ed from cancellation. If the caller (a periodic
        loop) gets cancelled mid-run, the loop task still raises
        ``CancelledError`` immediately, but the thread keeps executing
        and ``stop()`` will await its completion (bounded by
        ``shutdown_grace_seconds``). Without this, a SIGTERM in the
        middle of a survey pass would kill the agent halfway through
        the run and lose the work.
        """
        task = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
        self._inflight_passes.add(task)
        task.add_done_callback(self._inflight_passes.discard)
        return await asyncio.shield(task)

    async def _fire_periodic_pass(
        self,
        label: str,
        runner_fn,
        repo_config,
    ) -> None:
        """Run one periodic pass for *repo_config* (or ``None``).

        Wraps the call with run-registry lifecycle + tracing root
        span, mirroring the previous in-line behaviour of
        ``_run_periodic_pass_per_repo``. Errors are logged but do not
        propagate — the caller's loop continues across other repos.
        """
        run_id = None
        # Record into the per-repo registry so the run shows up in that repo's
        # /runs list (not the lead repo's).
        reg = self._registry_for(repo_config)
        repo_label = repo_config.repo_id if repo_config else label
        session_id = tracing.make_session_id(label, repo_config)
        try:
            log.info(
                "Starting periodic %s pass for repo %s",
                label,
                repo_label,
            )
            if reg:
                run_id = reg.start(
                    label,
                    repo_id=repo_config.repo_id if repo_config else "",
                )
            with tracing.start_ticket_root_span(
                session_id,
                label,
                repo_config=repo_config,
            ):
                result = await self._tracked_to_thread(
                    runner_fn,
                    session_id=session_id,
                    repo_config=repo_config,
                )
            # Deterministic passes (e.g. member_sync) return results without a
            # ``drafts_created`` field — tolerate its absence so they record an
            # ``ok`` run rather than a spurious error.
            drafts = getattr(result, "drafts_created", None) or []
            log.info(
                "%s pass (%s) completed, created %d draft(s)",
                label.capitalize(),
                repo_label,
                len(drafts),
            )
            if reg and run_id:
                runner_summary = (getattr(result, "summary", "") or "").strip()
                n = len(drafts)
                if runner_summary:
                    # The agent's own account + the draft count, so the count is
                    # always visible alongside its reasoning.
                    summary = f"{runner_summary} | {n} draft(s) filed"
                else:
                    draft_ids = [d["id"] for d in drafts[:5]]
                    summary = (
                        f"Created {n} drafts: "
                        f"{', '.join(draft_ids)}"
                        f"{'…' if n > 5 else ''}"
                    )
                reg.finish_ok(run_id, summary)
        except Exception as e:  # noqa: BLE001 — periodic must survive
            log.exception(
                "%s poll failed for repo %s",
                label,
                repo_label,
            )
            if reg and run_id:
                reg.finish_error(run_id, str(e))

    async def _run_periodic_pass(
        self,
        label: str,
        runner_fn,
        interval: int,
    ) -> None:
        """Shared periodic pass loop for audit, agent-check, etc.

        Args:
            label: Pass identifier (``"audit"``, ``"agent_check"``).
            runner_fn: Callable accepting ``session_id=`` keyword that
                       returns a result with a ``drafts_created`` field.
            interval: Seconds between passes.
        """
        initial = self._initial_delay(label, interval)
        await asyncio.sleep(initial)
        while True:
            run_id = None
            session_id = tracing.make_session_id(label)
            try:
                log.info("Starting periodic %s pass", label)
                if self.run_registry:
                    run_id = self.run_registry.start(label)
                # runner_fn invokes pydantic-ai's ``agent.run_sync``,
                # which calls ``asyncio.run()`` internally and explodes
                # ("this event loop is already running") when invoked
                # from inside an async task. Offload to a worker thread
                # — same pattern stage handlers use.
                with tracing.start_ticket_root_span(
                    session_id, label, repo_config=None
                ):
                    result = await self._tracked_to_thread(
                        runner_fn,
                        session_id=session_id,
                    )
                log.info(
                    "%s pass completed, created %d draft(s)",
                    label.capitalize(),
                    len(result.drafts_created),
                )
                if self.run_registry and run_id:
                    runner_summary = (getattr(result, "summary", "") or "").strip()
                    if runner_summary:
                        summary = runner_summary
                    else:
                        draft_ids = [d["id"] for d in result.drafts_created[:5]]
                        summary = (
                            f"Created {len(result.drafts_created)} drafts: "
                            f"{', '.join(draft_ids)}"
                            f"{'…' if len(result.drafts_created) > 5 else ''}"
                        )
                    self.run_registry.finish_ok(run_id, summary)
            except Exception as e:  # noqa: BLE001 — never let the poll die
                log.exception("%s poll failed", label)
                if self.run_registry and run_id:
                    self.run_registry.finish_error(run_id, str(e))
            await asyncio.sleep(interval)

    async def _meta_pass_loop(self) -> None:
        """Global meta-agent loop — fires once per interval (not per-repo).

        The meta-agent surveys ALL registered repo clones, identifies
        extraction and alignment opportunities, and files drafts to the
        meta board and per-repo boards respectively.
        """
        from robotsix_mill.meta.runner import MetaPassResult, run_meta_pass

        interval = max(60, self.ctx.settings.meta_interval_seconds)
        initial = self._initial_delay("meta", interval)
        await asyncio.sleep(initial)
        while True:
            run_id = None
            session_id = tracing.make_session_id("meta")
            # Record into the dedicated meta-board registry (tagged
            # repo_id="meta") so the run shows on the meta board's runs
            # drawer, not the lead repo's. Falls back to the default
            # registry if the meta registry is somehow absent.
            registry = self.run_registries.get(self._META_BOARD) or self.run_registry
            # Route the trace to the meta board's dedicated Langfuse project
            # (config/repos.yaml ``meta:`` block) so meta passes are observable
            # just like per-repo pipelines. ``None`` when unconfigured → meta
            # traces nowhere, same as before (no regression).
            meta_repo_config = get_repos_config().meta
            try:
                log.info("Starting periodic meta pass")
                if registry:
                    run_id = registry.start("meta", repo_id=self._META_BOARD)
                with tracing.start_ticket_root_span(
                    session_id, "meta", repo_config=meta_repo_config
                ):
                    result: MetaPassResult = await self._tracked_to_thread(
                        run_meta_pass,
                        session_id=session_id,
                    )
                total_drafts = len(result.extraction_drafts_created) + len(
                    result.alignment_drafts_created
                )
                log.info(
                    "Meta pass completed, created %d extraction + %d alignment = %d total draft(s)",
                    len(result.extraction_drafts_created),
                    len(result.alignment_drafts_created),
                    total_drafts,
                )
                if registry and run_id:
                    extraction_ids = [
                        d["id"] for d in result.extraction_drafts_created[:3]
                    ]
                    alignment_ids = [
                        d["id"] for d in result.alignment_drafts_created[:3]
                    ]
                    parts = []
                    if extraction_ids:
                        parts.append(f"Extraction: {', '.join(extraction_ids)}")
                    if alignment_ids:
                        parts.append(f"Alignment: {', '.join(alignment_ids)}")
                    summary = "; ".join(parts) if parts else "No drafts created"
                    if total_drafts > 6:
                        summary += " …"
                    registry.finish_ok(run_id, summary)
            except Exception as e:  # noqa: BLE001 — never let the poll die
                log.exception("Meta pass failed")
                if registry and run_id:
                    registry.finish_error(run_id, str(e))
            await asyncio.sleep(interval)

    async def _run_health_pass_loop(self) -> None:
        """Global run-health loop — fires once per interval (not per-repo).

        Reads every board's run registry over the window, flags failed/
        degraded runs deterministically, runs one LLM pass to separate real
        failures from legitimate empties, and files high-confidence draft
        tickets to the mill board.
        """
        from robotsix_mill.runners.run_health_runner import (
            RunHealthPassResult,
            run_run_health_pass,
        )

        interval = max(60, self.ctx.settings.run_health_interval_seconds)
        initial = self._initial_delay("run-health", interval)
        await asyncio.sleep(initial)
        while True:
            run_id = None
            session_id = tracing.make_session_id("run_health")
            try:
                log.info("Starting periodic run-health pass")
                if self.run_registry:
                    run_id = self.run_registry.start("run_health")
                with tracing.start_ticket_root_span(
                    session_id, "run_health", repo_config=None
                ):
                    result: RunHealthPassResult = await self._tracked_to_thread(
                        run_run_health_pass,
                        session_id=session_id,
                    )
                log.info(
                    "Run-health pass completed, created %d draft(s)",
                    len(result.drafts_created),
                )
                if self.run_registry and run_id:
                    ids = [d["id"] for d in result.drafts_created[:3]]
                    summary = (
                        f"{len(result.drafts_created)} draft(s): {', '.join(ids)}"
                        if ids
                        else "No drafts created"
                    )
                    self.run_registry.finish_ok(run_id, summary)
            except Exception as e:  # noqa: BLE001 — never let the poll die
                log.exception("Run-health pass failed")
                if self.run_registry and run_id:
                    self.run_registry.finish_error(run_id, str(e))
            await asyncio.sleep(interval)

    async def _diagnostic_pass_loop(self) -> None:
        """Global daily diagnostic loop — fires once per interval.

        Delegates to the shared ``_run_periodic_pass`` helper, which owns
        the initial delay, the sleep loop, run-registry lifecycle, tracing
        root span, and per-pass error survival. The runner is the
        deterministic check orchestrator (no LLM).
        """
        from robotsix_mill.runners.diagnostic_runner import run_diagnostic_pass

        interval = max(60, self.ctx.settings.diagnostic_interval_seconds)
        await self._run_periodic_pass("diagnostic", run_diagnostic_pass, interval)

    async def _stale_branch_cleanup_loop(self) -> None:
        """Periodic stale-branch cleanup loop. Only runs when
        ``MILL_STALE_BRANCH_CLEANUP_PERIODIC=true``.

        Per-repo: iterates every registered repo, lists remote branches
        and open-PR branches, then deletes old unprotected branches that
        have no open PR and match the prefix/age guards.
        """
        from datetime import datetime, timezone

        from ...forge import get_forge

        settings = self.ctx.settings
        interval = max(3600, settings.stale_branch_cleanup_interval_seconds)
        initial = self._initial_delay("stale-branch-cleanup", interval)
        await asyncio.sleep(initial)
        while True:
            repos = get_repos_config()
            for repo_config in list(repos.repos.values()):
                repo_label = repo_config.repo_id
                try:
                    forge = get_forge(settings, repo_config)
                    branches = forge.list_branches()
                    open_pr = forge.list_open_pr_branches()
                    now = datetime.now(timezone.utc)
                    deleted = 0
                    for b in branches:
                        if _branch_is_stale(
                            b,
                            now=now,
                            max_age_days=settings.stale_branch_max_age_days,
                            target_branch=target_branch_for(settings, repo_config),
                            open_pr=open_pr,
                            prefix_only=settings.stale_branch_cleanup_prefix_only,
                            branch_prefix=settings.branch_prefix,
                        ):
                            if forge.delete_branch(branch=b.name):
                                log.info(
                                    "stale-branch cleanup: deleted %s on repo %s",
                                    b.name,
                                    repo_label,
                                )
                                deleted += 1
                    log.info(
                        "stale-branch cleanup: repo %s — %d branch(es) deleted",
                        repo_label,
                        deleted,
                    )
                except Exception:
                    log.exception(
                        "stale-branch cleanup failed for repo %s",
                        repo_label,
                    )
            await asyncio.sleep(interval)

    async def _orphaned_pr_check_loop(self) -> None:
        """Periodic orphaned-PR check loop.  Only runs when
        ``MILL_ORPHANED_PR_CHECK_PERIODIC=true`` (default off).

        Per-repo: iterates registered repos, lists open PRs, classifies
        mill-authored ones as orphaned when no active ticket drives them,
        and either auto-closes the PR (with a comment) or files a
        tracking ticket.
        """
        from robotsix_mill.runners.orphaned_pr_check import run_orphaned_pr_check_pass

        settings = self.ctx.settings
        interval = max(3600, settings.orphaned_pr_check_interval_seconds)
        initial = self._initial_delay("orphaned-pr-check", interval)
        await asyncio.sleep(initial)
        while True:
            repos = get_repos_config()
            for repo_config in list(repos.repos.values()):
                try:
                    result = run_orphaned_pr_check_pass(
                        repo_config=repo_config,
                    )
                    log.info(
                        "orphaned-pr-check: repo %s — scanned=%d closed=%d "
                        "filed=%d skipped=%d dry_run=%s",
                        repo_config.repo_id,
                        result.total_scanned,
                        result.closed,
                        result.filed,
                        result.skipped,
                        result.dry_run,
                    )
                except Exception:
                    log.exception(
                        "orphaned-pr-check: error on repo %s",
                        repo_config.repo_id,
                    )
            await asyncio.sleep(interval)

    async def _sandbox_reaper_loop(self) -> None:
        """Periodic orphan-sandbox reaper. Only runs when
        ``MILL_SANDBOX_REAPER_PERIODIC=true`` (default on).

        Force-removes leaked ``mill-sbx-*``/``mill-fetch-*`` containers
        whose uptime exceeds twice ``command_timeout`` — provably orphaned,
        since a live sandbox is bounded by ``command_timeout``. This catches
        containers orphaned by a mill crash/restart mid-run *without* having
        to wait for the next restart (the startup reaper handles those). See
        :func:`robotsix_mill.sandbox.reap_orphan_sandboxes`.
        """
        from ...sandbox import reap_orphan_sandboxes

        settings = self.ctx.settings
        interval = max(300, settings.sandbox_reaper_interval_seconds)
        threshold = max(settings.command_timeout * 2, 3600)
        initial = self._initial_delay("sandbox-reaper", interval)
        await asyncio.sleep(initial)
        while True:
            try:
                reaped = await asyncio.to_thread(
                    reap_orphan_sandboxes, max_age_seconds=threshold
                )
                if reaped:
                    log.warning(
                        "sandbox-reaper: force-removed %d orphan container(s) "
                        "older than %ds",
                        reaped,
                        threshold,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("sandbox-reaper poll failed")
            await asyncio.sleep(interval)

    async def _trace_health_poll_loop(self) -> None:
        """Periodic trace-health check loop. Only runs when
        ``MILL_TRACE_HEALTH_PERIODIC=true``.

        Multi-repo: fans out across all registered repos whose
        ``RepoConfig.trace_health_periodic`` flag is True. When no repos
        are registered, runs once with ``repo_config=None``.
        """
        settings = self.ctx.settings
        interval = max(3600, settings.trace_health_interval_seconds)
        initial = self._initial_delay("trace-health", interval)
        await asyncio.sleep(initial)
        while True:
            repos = get_repos_config()
            repo_configs = list(repos.repos.values())
            if not repo_configs:
                repo_configs = [None]  # type: ignore[list-item]
            else:
                repo_configs = [
                    rc
                    for rc in repo_configs
                    if self._has_periodic_presence(rc, "trace_health")
                ]
            for repo_config in repo_configs:
                repo_label = repo_config.repo_id if repo_config else "default"
                try:
                    log.info(
                        "Starting periodic trace-health check for repo %s",
                        repo_label,
                    )
                    from ...runners.trace_health_runner import run_trace_health_check

                    run_id = None
                    if self.run_registry:
                        run_id = self.run_registry.start(
                            "trace-health",
                            repo_id=repo_config.repo_id if repo_config else "",
                        )
                    result = await asyncio.to_thread(
                        run_trace_health_check,
                        repo_config=repo_config,
                    )
                    if result.draft_created:
                        log.info(
                            "Trace-health check (%s): draft created — "
                            "%d unsessioned, %d unnamed / %d traces",
                            repo_label,
                            result.unsessioned_count,
                            result.name_missing_count,
                            result.total_traces,
                        )
                    else:
                        log.info(
                            "Trace-health check (%s): no alert "
                            "(%d unsessioned, %d unnamed / %d traces)",
                            repo_label,
                            result.unsessioned_count,
                            result.name_missing_count,
                            result.total_traces,
                        )
                    if self.run_registry and run_id:
                        summary = (
                            f"Unsessoned: {result.unsessioned_count}, "
                            f"unnamed: {result.name_missing_count} / "
                            f"{result.total_traces} "
                            f"traces ({result.window_start} to "
                            f"{result.window_end}) — "
                            f"{'draft created' if result.draft_created else 'no alert'}"
                        )
                        self.run_registry.finish_ok(run_id, summary)
                except Exception as e:  # noqa: BLE001 — never let the poll die
                    log.exception(
                        "trace-health poll failed for repo %s",
                        repo_label,
                    )
                    if self.run_registry and run_id:
                        self.run_registry.finish_error(run_id, str(e))
            await asyncio.sleep(interval)

    async def _periodic_supervisor(self, repo_config: RepoConfig) -> None:
        """Per-repo periodic-workflow supervisor loop.

        Owns a clone of the managed repo at
        ``<data_dir>/<repo_id>/periodic_workspace/repo`` (legacy name:
        ``bespoke_workspace`` — auto-migrated below) and reconciles the
        set of running per-workflow loop tasks against the files the repo
        ships, on each cycle:

        - ``<clone>/.robotsix-mill/periodic/<name>.yaml`` (the unified
          per-repo periodic-workflow path): presence enables the workflow;
          it partial-merges over the built-in of the same name. ``llm_agent``
          and ``schedule_only`` kinds are scheduled here; ``maintenance`` is
          handled by the global poll loops; brand-new ``bespoke`` workflows
          via this dir are deferred to the legacy bespoke path below.
        - ``<clone>/.robotsix-mill/agents/<name>.yaml`` (legacy bespoke path,
          gated on ``settings.bespoke_periodic``): brand-new repo agents.

        Reconcile semantics per file: appear -> spawn a loop on its interval;
        disappear -> cancel; body change -> cancel + respawn (so the new
        prompt/model/interval take effect without operator intervention).
        Cancelling the supervisor cancels every child loop (worker.stop()).
        """
        from ...agents.bespoke_loader import load_bespoke_definitions
        from ...agents.periodic_loader import discover_periodic_workflows
        from ...runners.periodic_runner import _clone_token
        from ...runners.member_sync_runner import run_member_sync_pass
        from ...vcs import git_ops

        settings = self.ctx.settings
        interval = max(60, settings.bespoke_discovery_interval_seconds)
        board_id = repo_config.board_id
        target = target_branch_for(settings, repo_config)
        forge_url = repo_config.forge_remote_url or settings.forge_remote_url
        repo_data_dir = settings.data_dir / repo_config.repo_id
        periodic_ws = repo_data_dir / "periodic_workspace"
        clone_dir = periodic_ws / "repo"

        # One-time migration of the legacy ``bespoke_workspace`` name (the
        # supervisor was the "bespoke supervisor" before it was generalized to
        # own all per-repo periodic-workflow discovery). Rename rather than
        # re-clone so the existing fetch history is preserved.
        legacy_ws = repo_data_dir / "bespoke_workspace"
        if legacy_ws.is_dir() and not periodic_ws.exists():
            try:
                legacy_ws.rename(periodic_ws)
                log.info(
                    "periodic supervisor (%s): migrated bespoke_workspace -> "
                    "periodic_workspace",
                    board_id,
                )
            except OSError:
                log.exception(
                    "periodic supervisor (%s): workspace rename failed", board_id
                )

        # namespaced key -> (task, comparison object). The comparison object
        # (a ResolvedPeriodicWorkflow or BespokeAgentDefinition) drives
        # respawn-on-change via ``==``.
        running: dict[str, tuple[asyncio.Task, Any]] = {}

        def _cancel_running() -> None:
            for task, _ in running.values():
                task.cancel()
            running.clear()

        try:
            # Skip the random initial delay: spawning bespoke tasks
            # immediately after worker start makes the system feel
            # responsive when an operator commits a new YAML.
            while True:
                try:
                    if forge_url and not (clone_dir / ".git").exists():
                        try:
                            clone_dir.parent.mkdir(
                                parents=True,
                                exist_ok=True,
                            )
                            git_ops.clone(
                                forge_url,
                                clone_dir,
                                target,
                                _clone_token(settings, repo_config),
                            )
                        except Exception:  # noqa: BLE001 — supervisor must survive
                            log.exception(
                                "bespoke supervisor (%s): clone failed",
                                board_id,
                            )
                    elif forge_url and (clone_dir / ".git").exists():
                        try:
                            git_ops.fetch(
                                clone_dir,
                                remote_url=forge_url,
                                token=_clone_token(settings, repo_config),
                                branch=target,
                            )
                            # Hard-reset to the remote so newly committed
                            # .robotsix-mill/ YAMLs land immediately.
                            import subprocess

                            subprocess.run(
                                [
                                    "git",
                                    "-C",
                                    str(clone_dir),
                                    "reset",
                                    "--hard",
                                    f"origin/{target}",
                                ],
                                check=False,
                                capture_output=True,
                            )
                        except Exception:  # noqa: BLE001
                            log.exception(
                                "bespoke supervisor (%s): refresh failed",
                                board_id,
                            )

                    # Event-trigger: fire member-sync out-of-band when this
                    # repo's vcs2l manifest (repos.yaml) content changed since
                    # the last cycle. Poll-cycle content-hash only (no
                    # inotify/webhook). Best-effort — never crash the loop.
                    try:
                        if settings.member_sync_periodic:
                            current_hash = _hash_repos_yaml(clone_dir)
                            stored_hash = _load_repos_yaml_hash(
                                settings, repo_config.repo_id
                            )
                            # A missing manifest hashes to "" (sentinel) which
                            # never differs-into-a-fire; only a real content
                            # change triggers the out-of-band pass.
                            if current_hash and current_hash != stored_hash:
                                log.info(
                                    "periodic supervisor (%s): repos.yaml "
                                    "changed — firing member-sync out-of-band",
                                    board_id,
                                )
                                await self._fire_periodic_pass(
                                    "member_sync",
                                    run_member_sync_pass,
                                    repo_config,
                                )
                                # Persist only after a successful fire.
                                _save_repos_yaml_hash(
                                    settings,
                                    repo_config.repo_id,
                                    current_hash,
                                )
                    except Exception:  # noqa: BLE001 — must not crash supervisor
                        log.exception(
                            "periodic supervisor (%s): member-sync trigger failed",
                            board_id,
                        )

                    # Build the DESIRED set of loops keyed by a namespaced
                    # id, each carrying a comparison object (for respawn) and
                    # a zero-arg spawn closure.
                    desired: dict[str, tuple[Any, Any]] = {}

                    # (a) Unified per-repo periodic workflows.
                    for wf in discover_periodic_workflows(clone_dir):
                        if not wf.enabled:
                            continue
                        if wf.kind in ("llm_agent", "schedule_only", "mill_only"):
                            # Global per-agent kill-switch (fleet-wide off).
                            if not getattr(settings, f"{wf.name}_periodic", True):
                                continue
                            key = f"periodic:{wf.name}"
                            desired[key] = (
                                wf,
                                (
                                    lambda wf=wf: self._run_periodic_workflow_loop(
                                        repo_config, wf, clone_dir
                                    )
                                ),
                            )
                        elif wf.kind == "bespoke":
                            # Brand-new agent via the unified dir — deferred to
                            # the legacy bespoke path for now (bespoke
                            # unification into .robotsix-mill/periodic/ is a
                            # follow-up).
                            log.debug(
                                "periodic %s/%s: bespoke kind via periodic dir "
                                "not yet scheduled here — use .robotsix-mill/"
                                "agents/ for now",
                                board_id,
                                wf.name,
                            )
                        # maintenance kind: handled by the global poll loops.

                    # (b) Legacy bespoke definitions (gated on the master switch).
                    if settings.bespoke_periodic:
                        for defn in load_bespoke_definitions(clone_dir):
                            key = f"bespoke:{defn.name}"
                            desired[key] = (
                                defn,
                                (
                                    lambda defn=defn: self._run_bespoke_loop(
                                        repo_config, defn, clone_dir
                                    )
                                ),
                            )

                    # Drop tasks whose source file disappeared.
                    for key in list(running):
                        if key not in desired:
                            task, _ = running.pop(key)
                            task.cancel()
                            log.info(
                                "periodic %s/%s: removed — cancelled", board_id, key
                            )

                    # Spawn / respawn tasks for the current desired set.
                    for key, (cmp_obj, spawn) in desired.items():
                        existing = running.get(key)
                        if existing is not None and existing[1] == cmp_obj:
                            continue  # unchanged
                        if existing is not None:
                            existing[0].cancel()
                            log.info(
                                "periodic %s/%s: changed — respawning", board_id, key
                            )
                        task = asyncio.create_task(spawn())
                        running[key] = (task, cmp_obj)
                        log.info("periodic %s/%s: scheduled", board_id, key)
                except Exception:  # noqa: BLE001
                    log.exception(
                        "bespoke supervisor (%s) cycle failed",
                        board_id,
                    )

                await asyncio.sleep(interval)
        finally:
            # Supervisor cancelled (worker.stop() or unexpected) ->
            # tear down every child loop so nothing keeps running.
            _cancel_running()
