"""CI-monitor poll loop for the ``Worker`` — split out of ``poll_loops``.

Defines ``PollLoopsCiMixin``: target-branch CI status polling, failure
draft filing with log-fetch deferral, cancelled-run re-dispatch, and the
canonical-CI-ticket lookup used to dedupe failure drafts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...config import RepoConfig, get_repos_config, target_branch_for
from ...core.dedup import _ci_draft_fingerprint, find_prior_matching_ticket
from ...core.models import Comment, SourceKind, Ticket
from ...core.states import State
from ...forge import rate_limit
from ..transient_errors import _TRANSIENT_HTTPX_EXCEPTIONS, is_network_down_error

if TYPE_CHECKING:
    from ...core.service import TicketService
    from ...forge import Forge

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
_CI_LOG_FETCH_BACKOFF_CAP_SECONDS = 30.0
_CI_LOG_FETCH_MAX_DEFERRALS = 3

# Workflow-run conclusions the target-branch CI monitor files a ticket for.
# ``startup_failure`` is what GitHub records for a run it could not start
# (no jobs, no logs); before 2026-09-10 the monitor skipped it, so a caller
# whose ``permissions:`` starved its reusable workflow stayed red for 11 days
# with nothing filed (robotsix-mill-ros2 Release Please, 2026-08-30 → 09-10).
_CI_FAILING_CONCLUSIONS = frozenset({"failure", "startup_failure"})
# A ``cancelled`` run that never STARTED a job (empty ``jobs``) on the target
# branch is the signature of a self-hosted queue timeout: GitHub auto-cancels
# a self-hosted job left queued past its 24 h limit (robotsix-invest's
# ``Publish Image`` on a down bequiet runner, 2026-10-07 → 10-12). Nothing
# re-dispatches it when the runner returns, so the image/artifact for the
# current head stays stale. The monitor re-dispatches the workflow via
# ``workflow_dispatch`` once a runner is back online — bounded to
# ``_REDISPATCH_MAX_ATTEMPTS`` attempts spaced ``_REDISPATCH_MIN_INTERVAL``
# apart. A ticket is filed only if a re-dispatched run then *fails* (handled
# by the ordinary ``_CI_FAILING_CONCLUSIONS`` path).
_REDISPATCH_MIN_INTERVAL_SECONDS = 6 * 3600  # once per 6 h
_REDISPATCH_MAX_ATTEMPTS = 3
# Agents read ticket descriptions through tool calls whose results are
# hard-capped well below 100K chars — an embedded log tail beyond that
# makes the whole description unreadable to them (the full logs stay one
# click away via the run link, and ci_fix re-fetches them itself).
_CI_LOG_EMBED_MAX_CHARS = 20_000


_CI_TITLE_PREFIX_RE = re.compile(r"^\s*CI failure:\s*", re.IGNORECASE)


def _title_names_workflow(title: str, wf_name: str, target: str) -> bool:
    """True when *title* names *wf_name* and *target* as whole words.

    The monitor's own ``CI failure:`` prefix is stripped first so a workflow
    literally named ``CI`` does not match every CI-failure ticket's title.
    Word boundaries treat ``-``/``_``/alphanumerics as word characters, so
    ``CI`` matches ``CI on main`` and ``Fix CI lint on main`` but not
    ``CodeQL on main`` or ``ci-lint on main``.
    """
    core = _CI_TITLE_PREFIX_RE.sub("", title)

    def _word(needle: str) -> bool:
        return re.search(rf"(?<![\w-]){re.escape(needle)}(?![\w-])", core) is not None

    return _word(wf_name) and _word(target)


def _ci_log_body_parts(logs: str, ansi_re: re.Pattern[str]) -> list[str]:
    """Markdown body parts embedding a bounded, ANSI-stripped log tail."""
    stripped = ansi_re.sub("", logs)
    parts: list[str] = []
    if len(stripped) > _CI_LOG_EMBED_MAX_CHARS:
        stripped = stripped[-_CI_LOG_EMBED_MAX_CHARS:]
        parts.append(
            "_Log tail below (truncated to the last "
            f"{_CI_LOG_EMBED_MAX_CHARS:,} chars — full logs at the run "
            "link above)._"
        )
    parts.extend(["```", stripped, "```"])
    return parts


class PollLoopsCiMixin(_WorkerBase):
    """CI-monitor poll loop and its helpers, mixed into ``PollLoopsMixin``."""

    def _ticket_last_activity(self, service: TicketService, ticket: Ticket) -> datetime:
        """Return ``max(ticket.created_at, max(c.created_at for c in comments))``,
        normalising naïve datetimes to UTC.
        """
        last_activity = ticket.created_at
        if last_activity.tzinfo is None:
            last_activity = last_activity.replace(tzinfo=UTC)
        comments: list[Comment] = []
        try:
            comments = service.list_comments(ticket.id)
        except Exception:
            comments = []
        for c in comments:
            c_at = c.created_at
            if c_at.tzinfo is None:
                c_at = c_at.replace(tzinfo=UTC)
            last_activity = max(last_activity, c_at)
        return last_activity

    def _find_canonical_ci_ticket(
        self,
        existing: list[Ticket],
        service: TicketService,
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
                # typically survive in the (refined) title. Match them as
                # whole words after stripping the monitor's own
                # ``CI failure:`` prefix — a plain substring test folded
                # every workflow literally named ``CI`` into ANY open
                # ``CI failure: <other workflow> on main`` ticket
                # (robotsix-board 2026-10-06: the red ``CI`` workflow
                # became a comment on the blocked CodeQL ticket d370 and
                # stayed invisible for a week).
                if not _title_names_workflow(t.title or "", wf_name, target):
                    continue
            last_activity = self._ticket_last_activity(service, t)
            if canonical_activity is None or last_activity > canonical_activity:
                canonical = t
                canonical_activity = last_activity
        return canonical, canonical_activity

    # ------------------------------------------------------------------
    # _poll_one_repo_ci helpers — extracted from the original god-method
    # to keep the coordinator lean.  See the docstring on each helper.
    # ------------------------------------------------------------------

    @staticmethod
    def _load_ci_state(
        state_path: Path,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Load CI monitor dedup state from ``ci_monitor_state.json``.

        Returns ``(state, seen, deferred)`` where *state* is the
        full deserialised dict and *seen* / *deferred* are guaranteed
        to exist (defaulting to empty dicts on missing or corrupt
        files).
        """
        state: dict[str, Any] = {"seen": {}}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text("utf-8"))
            except json.JSONDecodeError, OSError:
                state = {"seen": {}}
        seen = state.setdefault("seen", {})
        # Per-key deferral bookkeeping for runs whose logs could not be
        # fetched yet: ``{key: {"n": <cycles deferred>, "ts": <epoch>}}``.
        deferred = state.setdefault("deferred", {})
        return state, seen, deferred

    @staticmethod
    def _prune_ci_state(
        seen: dict[str, Any],
        deferred: dict[str, Any],
        now: float,
        ttl_seconds: int,
        redispatch: dict[str, Any] | None = None,
    ) -> None:
        """Prune ``seen`` entries older than *ttl_seconds* and drop
        ``deferred`` / ``redispatch`` records that have been handled or
        aged out.

        Mutates *seen*, *deferred*, and (when given) *redispatch* in place.
        """
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
        # Drop re-dispatch records that resolved (now seen) or aged out.
        if redispatch is not None:
            for key in [
                k
                for k, v in redispatch.items()
                if k in seen
                or not isinstance(v, dict)
                or (now - v.get("ts", now)) > ttl_seconds
            ]:
                del redispatch[key]

    @staticmethod
    async def _latest_runs_by_workflow(forge: Forge, target: str) -> dict[str, Any]:
        """List completed workflow runs on *target* and return only
        the latest run per ``workflow_id``.

        The GitHub API returns runs newest-first, so the first run
        seen for each workflow is the latest.
        """
        runs = await asyncio.to_thread(
            forge.list_workflow_runs,
            branch=target,
        )
        latest_by_wf: dict[str, Any] = {}
        for run in runs:
            wf = run.get("workflow_id")
            if wf is not None and wf not in latest_by_wf:
                latest_by_wf[wf] = run
        return latest_by_wf

    async def _fetch_run_logs_with_deferral(
        self,
        forge: Forge,
        run_id_val: Any,
        key: str,
        deferred: dict[str, Any],
        now: float,
        repo_label: str,
        wf_name: str,
    ) -> tuple[str, str, bool, bool]:
        """Fetch job logs for *run_id_val* with retry/backoff.

        Returns ``(logs, fetch_error, deferred_flag, network_down)``.

        * ``network_down`` is ``True`` when every attempt failed with a
          transient / network-down error — the caller must ``continue``
          **without** consuming the deferral budget.
        * ``deferred_flag`` is ``True`` when a genuine fetch failure has
          been booked for a later poll cycle.
        * When both are ``False`` the caller proceeds to file a draft
          (with or without logs).
        """
        logs = ""
        fetch_error = ""
        network_down = False
        for attempt in range(1, _CI_LOG_FETCH_ATTEMPTS + 1):
            try:
                logs = await asyncio.to_thread(
                    forge.fetch_workflow_job_logs, run_id=run_id_val
                )
                fetch_error = ""
                break
            except Exception as exc:
                fetch_error = f"{type(exc).__name__}: {exc}"
                if is_network_down_error(exc) or isinstance(
                    exc, _TRANSIENT_HTTPX_EXCEPTIONS
                ):
                    network_down = True
                    log.warning(
                        "CI monitor (%s): log fetch for %s (run %s) hit "
                        "transient/network-down error — skipping without "
                        "consuming deferral budget: %s",
                        repo_label,
                        wf_name,
                        run_id_val,
                        fetch_error,
                    )
                    break
                log.warning(
                    "CI monitor: failed to fetch logs for run %s (attempt %d/%d): %s",
                    run_id_val,
                    attempt,
                    _CI_LOG_FETCH_ATTEMPTS,
                    fetch_error,
                )
                if attempt < _CI_LOG_FETCH_ATTEMPTS:
                    backoff = min(
                        _CI_LOG_FETCH_BACKOFF_CAP_SECONDS,
                        _CI_LOG_FETCH_BACKOFF_SECONDS * (2 ** (attempt - 1)),
                    )
                    await asyncio.sleep(backoff * random.uniform(0.5, 1.0))

        if network_down:
            return "", fetch_error, False, True

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
                return "", fetch_error, True, False
            log.warning(
                "CI monitor (%s): log fetch still failing for %s (run %s) "
                "after %d deferrals — filing draft without logs",
                repo_label,
                wf_name,
                run_id_val,
                _CI_LOG_FETCH_MAX_DEFERRALS,
            )
            deferred.pop(key, None)

        return logs, fetch_error, False, False

    def _is_account_blocked_run(
        self,
        forge: Forge,
        run: dict[str, Any],
        repo_label: str,
        wf_name: str,
    ) -> bool:
        """True when *run* was refused by GitHub's account/billing block.

        Reads the run's aggregate check conclusion (whose failing checks
        carry the annotations) and matches the billing / spending-limit
        signature. Best-effort: any lookup error means "not blocked", so a
        transient API hiccup never suppresses a genuine failure ticket.
        """
        from ...stages.ci_infra_block import INFRA_ACCOUNT_BLOCKED, classify_ci_run

        sha = run.get("head_sha", "")
        if not sha:
            return False
        try:
            conclusion = forge.commit_ci_conclusion(sha=sha)
        except Exception:
            return False
        if not conclusion or classify_ci_run(conclusion) != INFRA_ACCOUNT_BLOCKED:
            return False
        log.info(
            "CI monitor (%s): %s @ %s refused by GitHub (account/billing "
            "block) — suppressing per-repo ticket (covered by the "
            "fleet-wide escalation)",
            repo_label,
            wf_name,
            sha[:8] if isinstance(sha, str) else sha,
        )
        return True

    def _maybe_redispatch_cancelled(
        self,
        forge: Forge,
        run: dict[str, Any],
        wf: Any,
        key: str,
        redispatch: dict[str, Any],
        now: float,
        repo_label: str,
        target: str,
    ) -> bool:
        """Handle a ``cancelled`` latest run on *target*.

        A cancelled run that never started a job is treated as
        "image/artifact missing for the current head" and re-dispatched via
        ``workflow_dispatch`` once a runner is online, bounded to
        ``_REDISPATCH_MAX_ATTEMPTS`` attempts ``_REDISPATCH_MIN_INTERVAL``
        apart (state tracked in *redispatch* keyed by *key*).

        Returns ``True`` when the run is terminally handled (the caller
        should mark *key* seen and stop re-checking it) and ``False`` when
        the caller should leave *key* unseen so a later cycle can retry
        (runner still offline, or waiting out the re-dispatch interval).
        """
        run_id_val: Any = run.get("id")
        wf_name = run.get("name", "unknown")

        # Within the per-attempt interval — wait it out (keep unseen).
        record = redispatch.get(key)
        if isinstance(record, dict):
            if record.get("n", 0) >= _REDISPATCH_MAX_ATTEMPTS:
                # Budget exhausted — stop re-dispatching; do not spam a
                # ticket (spec: a ticket only when a re-dispatched run fails).
                log.info(
                    "CI monitor (%s): %s (run %s) still cancelled after %d "
                    "re-dispatch attempts — giving up",
                    repo_label,
                    wf_name,
                    run_id_val,
                    _REDISPATCH_MAX_ATTEMPTS,
                )
                return True
            if (now - record.get("ts", 0)) < _REDISPATCH_MIN_INTERVAL_SECONDS:
                return False

        # Did the run actually start a job? A concurrency-cancelled run
        # (started jobs) is routine churn, not a queue timeout — ignore it.
        try:
            jobs = forge.list_workflow_run_jobs(run_id=run_id_val)
        except Exception as exc:
            log.warning(
                "CI monitor (%s): could not list jobs for cancelled run %s "
                "(%s) — skipping re-dispatch: %s",
                repo_label,
                run_id_val,
                wf_name,
                exc,
            )
            return True
        if jobs:
            return True

        # Queue timeout: only re-dispatch once a runner is back online.
        if not forge.has_online_runner():
            log.info(
                "CI monitor (%s): %s (run %s) cancelled (queue timeout) but "
                "no runner online — deferring re-dispatch",
                repo_label,
                wf_name,
                run_id_val,
            )
            return False

        result = forge.dispatch_workflow(workflow_id=wf, ref=target)
        n = (record.get("n", 0) if isinstance(record, dict) else 0) + 1
        redispatch[key] = {"n": n, "ts": now}
        if result.get("dispatched"):
            log.info(
                "CI monitor (%s): re-dispatched %s (previous run %s "
                "cancelled: queue timeout)",
                repo_label,
                wf_name,
                run_id_val,
            )
        else:
            log.warning(
                "CI monitor (%s): failed to re-dispatch %s (previous run %s "
                "cancelled: queue timeout): %s",
                repo_label,
                wf_name,
                run_id_val,
                result.get("reason", "unknown"),
            )
        return False

    # ------------------------------------------------------------------
    # _poll_one_repo_ci — coordinator
    # ------------------------------------------------------------------

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

        # 1. Load + prune dedup state.
        state, seen, deferred = self._load_ci_state(state_path)
        # Per-key re-dispatch bookkeeping for queue-timeout cancellations:
        # ``{key: {"n": <attempts>, "ts": <epoch of last dispatch>}}``.
        redispatch = state.setdefault("redispatch", {})
        self._prune_ci_state(seen, deferred, now, ttl_seconds, redispatch)

        # 2. Latest run per workflow on the target branch.
        forge = get_forge(settings, repo_config=rc)
        try:
            latest_by_wf = await self._latest_runs_by_workflow(forge, target)
        except Exception as exc:
            if is_network_down_error(exc) or isinstance(
                exc, _TRANSIENT_HTTPX_EXCEPTIONS
            ):
                log.warning(
                    "CI monitor (%s): skipping cycle — could not reach "
                    "forge (connectivity/outage): %s",
                    repo_label,
                    exc,
                )
                return
            raise

        existing = service.list()

        for wf, run in latest_by_wf.items():
            conclusion = run.get("conclusion")
            # Queue-timeout re-dispatch: a ``cancelled`` latest run that never
            # started a job means the image/artifact for the current head is
            # missing. Re-dispatch it (once a runner is online) rather than
            # filing a ticket — a ticket is filed only if a re-dispatched run
            # then fails (handled by the failing-conclusions path below).
            if conclusion == "cancelled":
                key = f"{wf}:{run.get('head_sha')}"
                if key in seen:
                    continue
                if self._maybe_redispatch_cancelled(
                    forge, run, wf, key, redispatch, now, repo_label, target
                ):
                    seen[key] = now
                continue
            if conclusion not in _CI_FAILING_CONCLUSIONS:
                continue
            # ``startup_failure`` = GitHub could not START the run (workflow
            # parse error, invalid ``uses:`` pin, or a ``uses:`` job whose
            # caller-level ``permissions`` cannot grant what the reusable
            # workflow declares). It has no jobs and no logs, so fetching
            # them only burns the deferral budget; describe it instead.
            startup_failure = conclusion == "startup_failure"

            wf_name = run.get("name", "unknown")
            wf_path = run.get("path", "")
            run_id_val = run.get("id")
            title = f"CI failure: {wf_name} on {target}"

            # Cheap first guard: avoid re-filing or re-commenting
            # for the exact same failing commit.
            key = f"{wf}:{run.get('head_sha')}"
            if key in seen:
                continue

            # Account-block suppression: when GitHub refused to START the
            # hosted job (billing / spending-limit — empty steps, single
            # billing annotation), do NOT file a per-repo ticket. It would
            # duplicate the single fleet-wide escalation the account-block
            # recovery pass raises, and it is not a code defect. Mark the
            # commit seen so we don't re-check every cycle.
            if not startup_failure and self._is_account_blocked_run(
                forge, run, repo_label, wf_name
            ):
                seen[key] = now
                continue

            # 3. Canonical-ticket consolidation.
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

            # 4. Fetch job logs with retry/backoff + deferral.
            logs, fetch_error, deferred_flag, network_down = "", "", False, False
            if not startup_failure:
                (
                    logs,
                    fetch_error,
                    deferred_flag,
                    network_down,
                ) = await self._fetch_run_logs_with_deferral(
                    forge,
                    run_id_val,
                    key,
                    deferred,
                    now,
                    repo_label,
                    wf_name,
                )
            if network_down:
                # Transient / network-down — do NOT consume deferral budget
                # and do NOT mark seen: next poll retries this commit.
                continue
            if deferred_flag:
                # Do NOT mark seen: the next poll retries this commit.
                continue

            # 5. Build draft body.
            body_parts = [
                f"**Workflow:** {wf_name}",
                f"**Path:** {wf_path}",
                f"**Branch:** {target}",
                f"**Run:** [{run_id_val}]({run.get('html_url', '')})",
                f"**Commit:** `{run.get('head_sha', '')}`",
                f"**Created:** {run.get('created_at', '')}",
                "",
            ]
            if startup_failure:
                body_parts.append(
                    "⚠️ **Conclusion: `startup_failure`** — GitHub could not "
                    "start this run, so it has no jobs and no logs. Do NOT "
                    "look for a failing step. The cause is in the workflow "
                    "file at the commit above (or in a reusable workflow it "
                    "calls): a YAML/parse error, an invalid `uses:` ref, an "
                    "input the called workflow does not declare, or a "
                    "`uses:` job whose caller-level `permissions:` cannot "
                    "grant what the reusable workflow declares (fix: "
                    "`permissions: {}` at the top and the required scopes on "
                    "the job). Diff the caller against a green sibling repo."
                )
            elif logs:
                body_parts.extend(_ci_log_body_parts(logs, ansi_re))
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

            # 6. Content-fingerprint dedup.
            fingerprint = _ci_draft_fingerprint(body, path=wf_path)
            prior = find_prior_matching_ticket(
                service,
                rc.board_id,
                target_files=[wf_path] if wf_path else [],
                fingerprint_text=body,
                settings=settings,
                now=datetime.now(tz=UTC),
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

            # 7. Create ticket + set labels.
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

        # 8. Persist state.
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
            # Back off the whole tick when the GitHub quota is nearly
            # exhausted — CI monitoring is non-urgent and would otherwise add
            # a burst of actions/runs 403s each cycle.
            skip = rate_limit.should_skip(settings)
            if skip is not None:
                rate_limit.log_skip(log, "ci-monitor poll", skip)
                await asyncio.sleep(min_interval)
                continue
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
                except Exception:
                    log.exception("CI monitor poll failed for repo %s", repo_label)

                last_polled[repo_label] = time.time()

            await asyncio.sleep(min_interval)
