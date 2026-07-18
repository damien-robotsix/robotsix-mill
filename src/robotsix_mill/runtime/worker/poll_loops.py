"""Worker poll loops — CI monitor, Dependabot ingest, and DB maintenance.

Defines ``PollLoopsMixin``, a mixin for the ``Worker`` class that
provides event-driven background poll loops: CI status monitoring,
Dependabot PR ingestion, database vacuum/cleanup, credit-balance
checks, timeout escalation, and periodic-workflow scheduling.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...config import RepoConfig, get_repos_config, target_branch_for
from ...core.dedup import _ci_draft_fingerprint, find_prior_matching_ticket
from ...core.models import Comment, SourceKind, Ticket
from ...core.states import State
from ..run_registry import RunRegistry

if TYPE_CHECKING:
    from ...core.service import TicketService

from ._base import _WorkerBase

log = logging.getLogger("robotsix_mill.worker")

# CI monitor log-fetch resilience. A transient fetch failure (e.g. a
# ConnectError) must not produce an empty draft: an empty failure draft
# strips the error text triage relies on, which has misrouted real code
# bugs to a diagnostic agent. Instead we retry within the
# poll, then DEFER to the next poll cycle (without marking the commit
# seen) so a later poll can capture the real logs. Only after the failure
# has survived ``_CI_LOG_FETCH_MAX_DEFERRALS`` poll cycles do we file the
# draft anyway — with the fetch error surfaced in the body so it is still
# actionable rather than silently empty.
_CI_LOG_FETCH_ATTEMPTS = 3
_CI_LOG_FETCH_BACKOFF_SECONDS = 2.0
_CI_LOG_FETCH_MAX_DEFERRALS = 3


class PollLoopsMixin(_WorkerBase):
    """Mixin that provides event-driven background poll loops for the ``Worker``.

    The mixin runs multiple periodic poll loops — CI monitor, Dependabot
    alert ingest, database maintenance, credit-balance checks, timeout
    escalation, and per-repo periodic-workflow scheduling — on an
    approximately 60-second tick via :meth:`_start_poll_loop_pass`. Each loop
    is independently gated by a settings flag and uses deterministic
    stagger/jitter so passes don't fire simultaneously after a restart.
    """

    def _registry_for(self, repo_config) -> "RunRegistry | None":
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
                    elapsed = (datetime.now(timezone.utc) - last_ts).total_seconds()
                except Exception:
                    base = 1.0
                else:
                    if elapsed >= interval:
                        base = 1.0
                    else:
                        base = interval - elapsed

        # Deterministic per-kind stagger so periodic agents don't all fire
        # simultaneously after a restart.  Cap at min(interval//12, 1h) so the
        # offset is meaningful (spreads agents across minutes) but never exceeds
        # an hour.  Plus up to 60 s of random jitter so two agents that happen
        # to hash-close don't fire in lockstep.
        kind_hash = int(
            hashlib.md5(kind.encode(), usedforsecurity=False).hexdigest(), 16
        )
        stagger_cap = max(60, min(interval // 12, 3600))  # 1 min .. 1 hour
        stagger = (kind_hash % stagger_cap) + random.uniform(0, 60)  # noqa: S311
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
        settings_enabled_attr: str | None = None,
    ) -> tuple[bool, int]:
        """Resolve ``(enabled, interval_seconds)`` for *label* on *repo_config*.

        Lookup order for each field:
          1. The agent YAML loaded via :func:`load_periodic_agent_definition`
             (clone-side override wins over built-in).
          2. The Settings field of the matching name as a fallback.

        Interval is clamped to >= 60s.
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
        interval = max(60, int(interval or 86400))

        # Enabled: YAML > Settings.
        enabled = True
        if definition and definition.enabled is not None:
            enabled = bool(definition.enabled)
        elif settings_enabled_attr is not None:
            enabled = bool(getattr(settings, settings_enabled_attr, True))
        return enabled, interval

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
        from ...runners import bespoke_runner
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
            except Exception as e:  # noqa: BLE001 — loop must survive
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
        "trace_review": "robotsix_mill.runners.trace_review_runner:run_trace_review_pass",
        "config_sync": "robotsix_mill.runners.periodic_runner:run_config_sync_pass",
        "member_sync": "robotsix_mill.runners.member_sync_runner:run_member_sync_pass",
        "data_dir_gc": ("robotsix_mill.runners.data_dir_gc:run_data_dir_gc_pass"),
        "credit_balance": (
            "robotsix_mill.runners.credit_balance_runner:run_credit_balance_check"
        ),
        "changelog_autofill": "robotsix_mill.runners.changelog_autofill_runner:run_changelog_autofill_pass",
        "pin_bump": "robotsix_mill.runners.pin_bump_runner:run_pin_bump_pass",
        "repo_description_sync": "robotsix_mill.runners.repo_description_sync_runner:run_repo_description_sync_pass",
    }

    def _build_periodic_workflow_runner(self, wf):
        """Return a ``runner_fn(session_id, repo_config)`` for *wf*, or None.

        ``llm_agent`` → a closure that runs the matching periodic pass with
        the merged definition threaded in as ``definition_override``.
        ``schedule_only`` → the workflow's module-level pass stub.
        """
        if wf.kind in ("llm_agent", "mill_only"):
            from ...config import Settings

            definition = wf.definition

            from ...runners.periodic_runner import (
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
        is sufficient. Gated by ``settings.langfuse_cleanup_periodic`` via
        ``_start_poll_loop_pass``. Pure HTTP, no LLM.
        """
        settings = self.ctx.settings
        interval = max(3600, settings.langfuse_cleanup_interval_seconds)
        initial = self._initial_delay("langfuse-cleanup", interval)
        await asyncio.sleep(initial)
        while True:
            try:
                from ...runners.langfuse_cleanup_runner import (
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
            except Exception:  # noqa: BLE001 — periodic sweep must not die
                log.exception("langfuse-cleanup poll failed")
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
                from ...runners.timeout_escalation_runner import run_timeout_escalation

                result = await asyncio.to_thread(
                    run_timeout_escalation,
                    settings,
                )
                log.info(
                    "timeout-escalation: pass complete — escalated=%d skipped=%d",
                    result.get("escaped", 0),
                    result.get("skipped", 0),
                )
            except Exception:  # noqa: BLE001 — never let the poll die
                log.exception("timeout-escalation poll failed")
            await asyncio.sleep(interval)

    def _ticket_last_activity(
        self, service: "TicketService", ticket: Ticket
    ) -> datetime:
        """Return ``max(ticket.created_at, max(c.created_at for c in comments))``,
        normalising naïve datetimes to UTC.
        """
        last_activity = ticket.created_at
        if last_activity.tzinfo is None:
            last_activity = last_activity.replace(tzinfo=timezone.utc)
        comments: list[Comment] = []
        try:
            comments = service.list_comments(ticket.id)
        except Exception:
            comments = []
        for c in comments:
            c_at = c.created_at
            if c_at.tzinfo is None:
                c_at = c_at.replace(tzinfo=timezone.utc)
            if c_at > last_activity:
                last_activity = c_at
        return last_activity

    def _find_canonical_ci_ticket(
        self,
        existing: list[Ticket],
        service: "TicketService",
        wf_name: str,
        target: str,
    ) -> tuple[Ticket | None, datetime | None]:
        """Scan *existing* for a non-terminal CI ticket matching the workflow
        + branch markers; return the most-recently-active match.
        """
        wf_marker = f"**Workflow:** {wf_name}"
        branch_marker = f"**Branch:** {target}"
        canonical: Ticket | None = None
        canonical_activity: datetime | None = None
        for t in existing:
            if t.source != SourceKind.CI:
                continue
            if t.state.value in (State.CLOSED.value, State.DONE.value):
                continue
            body_text = service.workspace(t).read_description() or ""
            if wf_marker in body_text and branch_marker in body_text:
                pass  # matched via body markers
            else:
                # Title-based fallback: when the predecessor's body has been
                # overwritten by refinement, the workflow name and branch
                # typically survive in the (refined) title.
                title = t.title or ""
                if wf_name not in title or target not in title:
                    continue
            last_activity = self._ticket_last_activity(service, t)
            if canonical_activity is None or last_activity > canonical_activity:
                canonical = t
                canonical_activity = last_activity
        return canonical, canonical_activity

    async def _poll_one_repo_ci(
        self,
        rc: RepoConfig,
        target: str,
        now: float,
        ttl_seconds: int,
        ansi_re: re.Pattern[str],
    ) -> None:
        """Execute one CI monitor poll cycle for a single repo."""
        from ...core.service import TicketService
        from ...forge import get_forge

        settings = self.ctx.settings
        repo_label = rc.repo_id

        state_dir = settings.data_dir / rc.repo_id
        service = TicketService(settings, board_id=rc.board_id)

        state_dir.mkdir(parents=True, exist_ok=True)
        state_path = state_dir / "ci_monitor_state.json"
        log.info("CI monitor poll starting for repo %s", repo_label)

        # 1. Load dedup state.
        state: dict = {"seen": {}}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text("utf-8"))
            except json.JSONDecodeError, OSError:
                state = {"seen": {}}
        seen = state.setdefault("seen", {})
        # Per-key deferral bookkeeping for runs whose logs could not be
        # fetched yet: ``{key: {"n": <cycles deferred>, "ts": <epoch>}}``.
        deferred = state.setdefault("deferred", {})

        # 2. Prune entries older than TTL.
        stale = [
            key
            for key, val in seen.items()
            if isinstance(val, (int, float)) and (now - val) > ttl_seconds
        ]
        for key in stale:
            del seen[key]
        # Drop deferral records that resolved (now seen) or aged out.
        for key in [
            k
            for k, v in deferred.items()
            if k in seen
            or not isinstance(v, dict)
            or (now - v.get("ts", now)) > ttl_seconds
        ]:
            del deferred[key]

        # 3. List completed workflow runs on the target branch.
        forge = get_forge(settings, repo_config=rc)
        runs = await asyncio.to_thread(
            forge.list_workflow_runs,
            branch=target,
        )

        # 4. Only the LATEST run per workflow reflects current
        # state (the GitHub API returns runs newest-first). Take
        # one run per workflow_id and act only on that — never
        # backfill every historical failed run.
        latest_by_wf: dict = {}
        for run in runs:
            wf = run.get("workflow_id")
            if wf is not None and wf not in latest_by_wf:
                latest_by_wf[wf] = run

        existing = service.list()

        for wf, run in latest_by_wf.items():
            if run.get("conclusion") != "failure":
                continue

            wf_name = run.get("name", "unknown")
            wf_path = run.get("path", "")
            run_id_val = run.get("id")
            title = f"CI failure: {wf_name} on {target}"

            # Cheap first guard: avoid re-filing or re-commenting
            # for the exact same failing commit.
            key = f"{wf}:{run.get('head_sha')}"
            if key in seen:
                continue

            # Robust (workflow, branch) dedup: consolidate
            # recurring failures into the canonical ticket via a
            # comment instead of filing a new ticket.
            canonical, _ = self._find_canonical_ci_ticket(
                existing, service, wf_name, target
            )

            if canonical is not None:
                log.info(
                    "CI monitor (%s): recurring failure — %s "
                    "(run %s) on %s, consolidating into %s",
                    repo_label,
                    wf_name,
                    run_id_val,
                    target,
                    canonical.id,
                )
                comment_body = (
                    f"Run [{run_id_val}]({run.get('html_url', '')}) "
                    f"also failed at commit "
                    f"`{run.get('head_sha', '')}` on "
                    f"{run.get('created_at', '')} "
                    f"({wf_name} on {target})."
                )
                try:
                    service.add_comment(canonical.id, body=comment_body)
                except Exception:
                    log.warning(
                        "CI monitor: failed to add consolidation "
                        "comment to %s for run %s",
                        canonical.id,
                        run_id_val,
                    )
                # Mark the commit as handled regardless, so a
                # transient comment failure does not loop-spam.
                seen[key] = now
                continue

            log.info(
                "CI monitor (%s): new failure — %s (run %s) on %s",
                repo_label,
                wf_name,
                run_id_val,
                target,
            )

            # Fetch job logs, retrying within this poll to ride out a
            # momentary blip before giving up for this cycle.
            logs = ""
            fetch_error = ""
            for attempt in range(1, _CI_LOG_FETCH_ATTEMPTS + 1):
                try:
                    logs = await asyncio.to_thread(
                        forge.fetch_workflow_job_logs, run_id=run_id_val
                    )
                    fetch_error = ""
                    break
                except Exception as exc:  # noqa: BLE001 — captured for the draft
                    fetch_error = f"{type(exc).__name__}: {exc}"
                    log.warning(
                        "CI monitor: failed to fetch logs for run %s "
                        "(attempt %d/%d): %s",
                        run_id_val,
                        attempt,
                        _CI_LOG_FETCH_ATTEMPTS,
                        fetch_error,
                    )
                    if attempt < _CI_LOG_FETCH_ATTEMPTS:
                        await asyncio.sleep(_CI_LOG_FETCH_BACKOFF_SECONDS * attempt)

            # Logs could not be FETCHED (the call errored on every
            # attempt): defer to a later poll rather than filing an empty
            # draft, unless we have already deferred this commit too many
            # times — then file with the error surfaced. A successful fetch
            # that simply returned no text is not an error and files now.
            if not logs and fetch_error:
                record = deferred.get(key)
                count = record.get("n", 0) if isinstance(record, dict) else 0
                count += 1
                if count <= _CI_LOG_FETCH_MAX_DEFERRALS:
                    deferred[key] = {"n": count, "ts": now}
                    log.warning(
                        "CI monitor (%s): log fetch failed for %s (run %s) — "
                        "deferring to next poll (%d/%d): %s",
                        repo_label,
                        wf_name,
                        run_id_val,
                        count,
                        _CI_LOG_FETCH_MAX_DEFERRALS,
                        fetch_error or "no logs returned",
                    )
                    # Do NOT mark seen: the next poll retries this commit.
                    continue
                log.warning(
                    "CI monitor (%s): log fetch still failing for %s (run %s) "
                    "after %d deferrals — filing draft without logs",
                    repo_label,
                    wf_name,
                    run_id_val,
                    _CI_LOG_FETCH_MAX_DEFERRALS,
                )
                deferred.pop(key, None)

            # Build draft body.
            body_parts = [
                f"**Workflow:** {wf_name}",
                f"**Path:** {wf_path}",
                f"**Branch:** {target}",
                f"**Run:** [{run_id_val}]({run.get('html_url', '')})",
                f"**Commit:** `{run.get('head_sha', '')}`",
                f"**Created:** {run.get('created_at', '')}",
                "",
            ]
            if logs:
                stripped = ansi_re.sub("", logs)
                if len(stripped) > 200_000:
                    stripped = stripped[-200_000:]
                body_parts.append("```")
                body_parts.append(stripped)
                body_parts.append("```")
            elif fetch_error:
                body_parts.append(
                    "⚠️ **Could not fetch the run logs** after "
                    f"{_CI_LOG_FETCH_ATTEMPTS} attempts across "
                    f"{_CI_LOG_FETCH_MAX_DEFERRALS} poll cycles "
                    f"(last error: `{fetch_error or 'no logs returned'}`). "
                    "This is a genuine workflow **failure** on the target "
                    "branch — open the run link above for the error detail. "
                    "Do NOT treat the missing logs as a connectivity/"
                    "operational problem."
                )

            body = "\n".join(body_parts)

            # Content-based dedup: hash the error content (repo,
            # workflow path, error message prefix, affected file)
            # and check for a recent (≤ lookback_days) ticket with
            # the same signature.  When matched, add a consolidation
            # comment instead of filing a duplicate draft.
            fingerprint = _ci_draft_fingerprint(body, path=wf_path)
            prior = find_prior_matching_ticket(
                service,
                rc.board_id,
                target_files=[wf_path] if wf_path else [],
                fingerprint_text=body,
                settings=settings,
                now=datetime.now(tz=timezone.utc),
                sources=[SourceKind.CI],
                lookback_days=settings.dedup_lookback_days,
                dedup_labels=[f"ci_fp:{fingerprint}"],
                suppress_title_only_match=True,
            )
            if prior is not None:
                log.info(
                    "CI monitor (%s): content-dedup match — %s "
                    "(run %s) on %s, consolidating into %s "
                    "(fingerprint %s)",
                    repo_label,
                    wf_name,
                    run_id_val,
                    target,
                    prior.id,
                    fingerprint,
                )
                comment_body = (
                    f"Run [{run_id_val}]({run.get('html_url', '')}) "
                    f"also failed at commit "
                    f"`{run.get('head_sha', '')}` on "
                    f"{run.get('created_at', '')} "
                    f"({wf_name} on {target}).\n\n"
                    f"_Fingerprint: `{fingerprint}`_"
                )
                try:
                    service.add_comment(prior.id, body=comment_body)
                except Exception:
                    log.warning(
                        "CI monitor: failed to add content-dedup "
                        "consolidation comment to %s for run %s",
                        prior.id,
                        run_id_val,
                    )
                seen[key] = now
                deferred.pop(key, None)
                continue

            try:
                ticket = service.create(
                    title=title,
                    description=body,
                    source=SourceKind.CI,
                    priority=True,
                )
                try:
                    service.set_labels(ticket.id, [f"ci_fp:{fingerprint}"])
                except Exception:
                    log.warning(
                        "CI monitor: failed to set ci_fp label on %s",
                        ticket.id,
                    )
            except Exception:
                log.exception(
                    "CI monitor: failed to create draft for run %s",
                    run_id_val,
                )
                continue

            # Mark as seen and clear any deferral bookkeeping.
            seen[key] = now
            deferred.pop(key, None)

        # 5. Persist state.
        state_path.write_text(json.dumps(state), "utf-8")

        log.info("CI monitor poll completed for repo %s", repo_label)

    async def _ci_monitor_poll_loop(self) -> None:
        """Periodic CI monitor poll: watch the forge target branch for
        completed workflow-run failures and file a ``source="ci"`` draft
        for each new one.

        Per-repo enabled/interval are controlled via ``RepoConfig``
        fields in ``config/repos.yaml``.  The loop runs when *any*
        registered repo has ``ci_monitor_enabled=True``.
        """
        settings = self.ctx.settings
        ttl_seconds = 30 * 86400  # 30 days

        # ANSI strip for log text (same pattern as forge/github.py).
        _ansi_re = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

        # Per-repo tracking: last polled timestamp (epoch seconds).
        last_polled: dict[str, float] = {}

        # Determine the minimum interval across all enabled repos so
        # the loop ticks frequently enough to honour the fastest one,
        # but only poll each repo when its own interval has elapsed.
        repos = get_repos_config()
        repo_configs = [rc for rc in repos.repos.values() if rc.ci_monitor_enabled]

        min_interval = 60
        if repo_configs:
            min_interval = max(
                60, min(rc.ci_monitor_interval_seconds for rc in repo_configs)
            )

        await asyncio.sleep(self._initial_delay("ci_monitor", min_interval))
        while True:
            for rc in repo_configs:
                # Skip repos that opt out of forge-CI gating.
                from ...config.repo_settings import load_repo_skip_ci

                if load_repo_skip_ci(self._find_config_clone_dir(rc)):
                    continue

                repo_label = rc.repo_id
                target = target_branch_for(settings, rc)
                interval = max(60, rc.ci_monitor_interval_seconds)

                # Honour per-repo interval.
                now = time.time()
                if (
                    repo_label in last_polled
                    and (now - last_polled[repo_label]) < interval
                ):
                    continue

                try:
                    await self._poll_one_repo_ci(rc, target, now, ttl_seconds, _ansi_re)
                except Exception:  # noqa: BLE001 — never let the poll die
                    log.exception("CI monitor poll failed for repo %s", repo_label)

                last_polled[repo_label] = time.time()

            await asyncio.sleep(min_interval)

    async def _poll_one_repo_dependabot(
        self,
        rc: RepoConfig,
        now: float,
        ttl_seconds: int,
        remaining_cap: int,
    ) -> int:
        """Ingest one repo's OPEN Dependabot alerts, filing deduped drafts.

        Returns the number of drafts created (so the caller can enforce a
        per-pass cap across repos).  *remaining_cap* is the number of drafts
        still allowed this pass (``<= 0`` disables filing but still refreshes
        the dedup state).
        """
        from ...core.service import TicketService
        from ...forge import get_forge

        settings = self.ctx.settings
        repo_label = rc.repo_id

        state_dir = settings.data_dir / rc.repo_id
        state_dir.mkdir(parents=True, exist_ok=True)
        state_path = state_dir / "dependabot_ingest_state.json"

        # 1. Load dedup state: {"seen": {alert_key: epoch}}.
        state: dict[str, Any] = {"seen": {}}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text("utf-8"))
            except json.JSONDecodeError, OSError:
                state = {"seen": {}}
        seen = state.setdefault("seen", {})

        # 2. Prune entries older than TTL (alert fixed/dismissed long ago).
        for key in [
            k
            for k, v in seen.items()
            if isinstance(v, (int, float)) and (now - v) > ttl_seconds
        ]:
            del seen[key]

        # 3. List OPEN Dependabot alerts (best-effort; [] on any error).
        forge = get_forge(settings, repo_config=rc)
        alerts = await asyncio.to_thread(forge.list_dependabot_alerts)

        created = 0
        if alerts:
            service = TicketService(settings, board_id=rc.board_id)
            for alert in alerts:
                # Dedup key: ghsa_id+package is stable across alert renumbering
                # (default-setup re-numbers alerts); fall back to the number.
                ghsa = alert.get("ghsa_id") or ""
                package = alert.get("package") or ""
                number = alert.get("number")
                key = f"{ghsa}:{package}" if ghsa else f"num:{number}"
                if key in seen:
                    continue

                if remaining_cap - created <= 0:
                    # Cap reached — do NOT mark seen, so it's filed next pass.
                    log.info(
                        "Dependabot ingest: per-pass cap reached, deferring "
                        "remaining alerts for repo %s",
                        repo_label,
                    )
                    break

                title = _dependabot_title(alert)
                body = _dependabot_body(alert)
                try:
                    await asyncio.to_thread(
                        service.create,
                        title=title,
                        description=body,
                        source=SourceKind.DEPENDABOT_ALERTS,
                    )
                except Exception:  # noqa: BLE001 — never let the poll die
                    log.exception(
                        "Dependabot ingest: failed to create draft for "
                        "alert %s in repo %s",
                        number,
                        repo_label,
                    )
                    continue

                seen[key] = now
                created += 1
                log.info(
                    "Dependabot ingest (%s): filed draft for %s (%s in %s)",
                    repo_label,
                    ghsa or f"#{number}",
                    alert.get("severity", "?"),
                    package or "?",
                )

        # 4. Persist state.
        state_path.write_text(json.dumps(state), "utf-8")
        return created

    async def _dependabot_ingest_poll_loop(self) -> None:
        """Periodic Dependabot vulnerability-alert ingest.

        Iterates every registered repo, lists its OPEN GitHub Dependabot
        alerts via the forge, and files one deduped ``source="dependabot_alerts"``
        draft per new alert so the normal pipeline picks up the dependency
        bump.  Deterministic — no LLM, no Langfuse tracing.

        Gated by ``settings.dependabot_ingest_periodic``; cadence by
        ``dependabot_ingest_interval_seconds`` (min 60 s); per-pass draft
        volume by ``dependabot_ingest_max_drafts_per_pass``.
        """
        settings = self.ctx.settings
        interval = max(60, settings.dependabot_ingest_interval_seconds)
        ttl_seconds = 90 * 86400  # 90 days — alerts are long-lived.

        await asyncio.sleep(self._initial_delay("dependabot-ingest", interval))
        while True:
            cap = settings.dependabot_ingest_max_drafts_per_pass
            # cap <= 0 means unlimited; track a large remaining budget.
            remaining = cap if cap > 0 else 1_000_000
            now = time.time()
            try:
                for rc in get_repos_config().repos.values():
                    try:
                        filed = await self._poll_one_repo_dependabot(
                            rc, now, ttl_seconds, remaining
                        )
                        remaining -= filed
                        if remaining <= 0 and cap > 0:
                            break
                    except Exception:  # noqa: BLE001 — never let the poll die
                        log.exception(
                            "Dependabot ingest poll failed for repo %s", rc.repo_id
                        )
            except Exception:  # noqa: BLE001 — repo enumeration failure
                log.exception("Dependabot ingest poll: could not enumerate repos")
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
                            "events_pruned=%d tickets_pruned=%d",
                            label,
                            summary["archived_purged"],
                            summary["events_pruned"],
                            summary["tickets_pruned"],
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
                from ...runners.credit_balance_runner import run_credit_balance_check

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
        log_args: tuple = (),
    ) -> None:
        """Start a dedicated poll-loop periodic pass if its settings flag
        (derived from *label*) is on and the task attribute is still ``None``.

        ``poll_loop_fn`` is a zero-argument async callable (typically a
        bound method like ``self._trace_health_poll_loop``).
        """
        flag = label.replace("-", "_") + "_periodic"
        if getattr(self.ctx.settings, flag) and getattr(self, task_attr) is None:
            setattr(
                self,
                task_attr,
                asyncio.create_task(poll_loop_fn()),
            )
            if log_msg is not None:
                log.info(log_msg, *log_args)


# ---------------------------------------------------------------------------
# Dependabot draft formatting helpers
# ---------------------------------------------------------------------------


def _dependabot_title(alert: dict[str, Any]) -> str:
    """Build a concise ticket title for a Dependabot alert."""
    severity = (alert.get("severity") or "unknown").capitalize()
    package = alert.get("package") or "dependency"
    return f"Dependabot: {severity} vulnerability in {package}"


def _dependabot_body(alert: dict[str, Any]) -> str:
    """Build the draft body for a Dependabot alert."""
    lines = [
        f"**Package:** `{alert.get('package', '')}` ({alert.get('ecosystem', '')})",
        f"**Severity:** {alert.get('severity', '')}",
    ]
    if alert.get("ghsa_id"):
        lines.append(f"**Advisory:** {alert['ghsa_id']}")
    if alert.get("cve_id"):
        lines.append(f"**CVE:** {alert['cve_id']}")
    if alert.get("manifest_path"):
        lines.append(f"**Manifest:** `{alert['manifest_path']}`")
    if alert.get("url"):
        lines.append(f"**Alert:** {alert['url']}")
    lines.append("")
    if alert.get("summary"):
        lines.append(alert["summary"])
    lines.append("")
    lines.append(
        "Resolve by upgrading the affected dependency to a non-vulnerable "
        "version (update the lockfile / manifest), then verify the build and "
        "tests pass."
    )
    return "\n".join(lines)
