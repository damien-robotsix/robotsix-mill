"""GitHub CI/checks mixin — workflow runs, job log fetching, check status.

Split from ``github.py``.  Defines ``GitHubForgeCIMixin`` that
``GitHubForge`` inherits from.
"""

from __future__ import annotations

import base64
import fnmatch
import functools
from typing import Any

import httpx
import yaml

from ._log_utils import (
    _capture_failure_window,
    _failing_step,
    _slice_log_to_step,
    _strip_runner_noise,
)
from .github_ci_status import (
    _ANSI_RE,
    _LOG_FAILURE_RE,
    _LOG_FAILURE_TAIL_CONTEXT,
    _MAX_FAILED_JOBS,
    _derive_check_conclusion,
    _fetch_run_job_failures,
    _merge_workflow_run_failures,
    _note_unreadable,
    _statuses_to_check_runs,
    _trigger_event_names,
)


class GitHubForgeCIMixin:
    """CI/checks operations for GitHub — mixed into ``GitHubForge``.

    Expects ``self._http``, ``self._owner_repo``, ``self.settings``,
    ``self._repo_config``, ``self._get_pr`` to exist on the final class.
    """

    def check_status(
        self, *, source_branch: str, require_checks: bool = False
    ) -> dict[str, Any] | None:
        """Return the aggregate CI check status for *source_branch*'s PR head.

        Returns a ``dict`` with ``conclusion`` (``"success"`` /
        ``"failure"`` / ``"pending"``) and a ``failing`` list of failing-
        check detail dicts, or ``None`` when there is no PR / head SHA to
        gate on. A repo with no CI configured reports ``"success"`` so the
        merge pipeline does not wait forever.

        When *require_checks* is ``True``, an empty check-run list (no CI
        registered yet for the commit) is classified as ``"pending"``
        rather than ``"success"`` — callers that know the repo MUST have CI
        (e.g. the ci-fix agent after a push) should pass ``True`` to avoid
        a false ``CI_PASSED`` before the first check run materialises.
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        return self._check_status(
            owner=owner, repo=repo, head=source_branch, require_checks=require_checks
        )

    def commit_ci_conclusion(self, *, sha: str) -> dict[str, Any] | None:
        """Aggregate CI conclusion for an arbitrary commit SHA (no PR).

        Same return shape as check_status: {"conclusion": "success"|"failure"|
        "pending"|None, "failing": [...], "pending": [...]} or None when the
        status cannot be determined (auth/permission/transport error).
        """
        try:
            owner, repo = self._owner_repo  # type: ignore[attr-defined]
            return self._check_status(owner=owner, repo=repo, head="", sha=sha)
        except Exception:
            return None

    def list_workflow_runs(
        self, *, branch: str | None = None, head_sha: str | None = None
    ) -> list[dict[str, Any]]:
        """Return completed GitHub Actions workflow runs.

        :param branch: when set, filter runs to this branch.
        :param head_sha: when set, filter runs to this head commit SHA.
        Returns a ``list[dict]`` (one per run) with ``id``, ``name``,
        ``workflow_id``, ``head_sha``, ``conclusion``, ``html_url``,
        ``created_at``, ``event``, and ``head_branch``.
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        return self._list_workflow_runs(
            owner=owner,
            repo=repo,
            branch=branch,
            head_sha=head_sha,
        )

    def list_tag_workflow_runs(self, *, tag_glob: str) -> list[dict[str, Any]]:
        """Return completed ``push`` runs whose ``head_branch`` (the tag
        name, for a tag push) matches the fnmatch *tag_glob*.
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        runs = self._list_workflow_runs(
            owner=owner, repo=repo, branch=None, head_sha=None, event="push"
        )
        return [
            r
            for r in runs
            if r.get("event") == "push"
            and fnmatch.fnmatchcase(r.get("head_branch") or "", tag_glob)
        ]

    def get_workflow_run(self, *, run_id: int) -> dict[str, Any] | None:
        """Return a single GitHub Actions workflow run by *run_id*.

        Best-effort: returns a dict with ``id``, ``name``, ``path``,
        ``head_branch``, ``head_sha``, ``conclusion``, ``run_attempt``,
        ``html_url``, and ``created_at`` on success, or ``None`` on any
        error (missing run, transport failure, permission denial).
        """
        try:
            owner, repo = self._owner_repo  # type: ignore[attr-defined]
            return self._get_workflow_run(owner=owner, repo=repo, run_id=run_id)
        except Exception:
            return None

    def fetch_workflow_job_logs(self, *, run_id: int, full_log: bool = False) -> str:
        """Return the logs of the failed jobs in workflow run *run_id*.

        :param run_id: GitHub Actions workflow-run id whose jobs to fetch.
        :param full_log: when ``False`` (default), size-caps and windows
            the log around the first failure marker; ``True`` returns the
            complete job logs (still ANSI-stripped and runner-noise-stripped).
        Concatenates logs for up to the first few failed-like jobs of the run
        into a single string; returns ``""`` when the run has no failed jobs.
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        return self._fetch_workflow_job_logs(
            owner=owner,
            repo=repo,
            run_id=run_id,
            full_log=full_log,
        )

    def rerun_workflow(self, *, run_id: int) -> dict[str, Any]:
        """Re-run a GitHub Actions workflow run by id.

        Returns ``{"rerun": True}`` on success, ``{"rerun": False,
        "reason": ...}`` on any failure.  Must NEVER raise.
        """
        try:
            owner, repo = self._owner_repo  # type: ignore[attr-defined]
            return self._rerun_workflow(owner=owner, repo=repo, run_id=run_id)
        except Exception as e:
            return {"rerun": False, "reason": str(e)}

    def approve_workflow(self, *, run_id: int) -> dict[str, Any]:
        """Approve a GitHub Actions workflow run awaiting approval.

        Returns ``{"approved": True}`` on success, ``{"approved": False,
        "reason": ...}`` on any failure.  When the approval is refused
        (HTTP 403), the result additionally carries ``"forbidden": True``
        so callers can distinguish a permission denial from a transient
        error.  Must NEVER raise.
        """
        try:
            owner, repo = self._owner_repo  # type: ignore[attr-defined]
            return self._approve_workflow(owner=owner, repo=repo, run_id=run_id)
        except httpx.HTTPStatusError as e:
            forbidden = e.response is not None and e.response.status_code == 403
            return {
                "approved": False,
                **({"forbidden": True} if forbidden else {}),
                "reason": str(e),
            }
        except Exception as e:
            return {"approved": False, "reason": str(e)}

    def list_workflow_run_jobs(self, *, run_id: int) -> list[dict[str, Any]]:
        """List the jobs of workflow run *run_id*.

        Returns a list of ``{"id", "name", "status", "conclusion"}`` dicts
        (empty when the run never started a job — the queue-timeout
        signature).  May raise on transport error so a caller can tell
        "no jobs" apart from "could not tell".
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        return self._list_workflow_run_jobs(owner=owner, repo=repo, run_id=run_id)

    def has_online_runner(self) -> bool:
        """True when any self-hosted runner for the repo is online.

        Best-effort: any lookup error returns ``False``.  Must NEVER raise.
        """
        try:
            owner, repo = self._owner_repo  # type: ignore[attr-defined]
            return self._has_online_runner(owner=owner, repo=repo)
        except Exception:
            return False

    def get_workflow_triggers(self, *, path: str) -> list[str] | None:
        """Return the top-level ``on:`` trigger event names of the workflow
        file at *path* (e.g. ``.github/workflows/refresh-deps.yml``).

        Returns a list such as ``["schedule", "workflow_dispatch"]``, or
        ``None`` on any error (missing file, transport failure, unparseable
        YAML).  Must NEVER raise.
        """
        try:
            owner, repo = self._owner_repo  # type: ignore[attr-defined]
            return self._get_workflow_triggers(owner=owner, repo=repo, path=path)
        except Exception:
            return None

    def get_workflow_file(self, *, path: str) -> str | None:
        """Return the raw text of the workflow file at *path*, or ``None``
        on any error.  Must NEVER raise.
        """
        try:
            owner, repo = self._owner_repo  # type: ignore[attr-defined]
            return self._get_workflow_file(owner=owner, repo=repo, path=path)
        except Exception:
            return None

    def list_path_commits_since(
        self, *, path: str, branch: str, since: str
    ) -> list[str] | None:
        """Return SHAs of commits on *branch* touching *path* after *since*,
        or ``None`` on any error.  Must NEVER raise.
        """
        try:
            owner, repo = self._owner_repo  # type: ignore[attr-defined]
            return self._list_path_commits_since(
                owner=owner, repo=repo, path=path, branch=branch, since=since
            )
        except Exception:
            return None

    def dispatch_workflow(
        self,
        *,
        workflow_id: int | None = None,
        workflow_file: str | None = None,
        ref: str = "main",
    ) -> dict[str, Any]:
        """Trigger a ``workflow_dispatch`` run of a workflow on *ref*.

        Identify the workflow by numeric *workflow_id* (CI-monitor
        queue-timeout re-dispatch) or by *workflow_file* name
        (e.g. ``refresh-deps.yml``, ci-auto-close) — GitHub's dispatch
        endpoint accepts either form in the same path slot.

        Returns ``{"dispatched": True}`` on success, ``{"dispatched":
        False, "reason": ...}`` on any failure (including HTTP 422 when the
        workflow has no ``workflow_dispatch`` trigger).  Must NEVER raise.
        """
        try:
            owner, repo = self._owner_repo  # type: ignore[attr-defined]
            return self._dispatch_workflow(
                owner=owner,
                repo=repo,
                workflow_id=workflow_id,
                workflow_file=workflow_file,
                ref=ref,
            )
        except Exception as e:
            return {"dispatched": False, "reason": str(e)}

    # --- HTTP seams (monkeypatched in tests) ---

    def _check_status(
        self,
        *,
        owner: str,
        repo: str,
        head: str,
        sha: str | None = None,
        require_checks: bool = False,
    ) -> dict[str, Any] | None:
        if sha is None:
            pr = self._get_pr(owner=owner, repo=repo, head=head)  # type: ignore[attr-defined]
            if pr is None:
                return None

            sha = pr.get("sha", "")
            if not sha:
                return None

        for _retry, c, api, headers in self._http.retrying_client():  # type: ignore[attr-defined]
            # 1. Fetch check runs (any status — completed, in_progress,
            # queued — so a brand-new SHA with a workflow that's been
            # queued but not started is correctly classified "pending"
            # rather than "no CI configured" below.
            #
            # A 403 here means the App installation lacks ``checks: read``
            # for this repo. That's a config gap, not a transient error
            # — treat it as "no check_runs visible" and fall through to
            # statuses + no-CI handling, but WARN once and flag the result
            # ``checks_unreadable`` so the billing-block gate can still fire.
            unreadable: set[str] = set()
            check_runs: list[dict[str, Any]] = []
            cr_resp = c.get(
                f"{api}/repos/{owner}/{repo}/commits/{sha}/check-runs",
                headers=headers,
                params={"per_page": 100},
            )
            if cr_resp.status_code == 401:
                continue
            if cr_resp.status_code == 403:
                _note_unreadable(owner, repo, "check-runs", unreadable)
            else:
                cr_resp.raise_for_status()
                check_runs = cr_resp.json().get("check_runs", [])

            # 2. Always probe combined statuses too. A repo without
            # any CI returns empty check_runs AND empty
            # statuses_data["statuses"] — we use that to distinguish
            # "no CI configured" (pass-through) from "CI pending"
            # (wait). 403 on statuses follows the same logic.
            status_runs: list[dict[str, Any]] = []
            st_resp = c.get(
                f"{api}/repos/{owner}/{repo}/commits/{sha}/status",
                headers=headers,
            )
            if st_resp.status_code == 401:
                continue
            if st_resp.status_code == 403:
                _note_unreadable(owner, repo, "status", unreadable)
            else:
                st_resp.raise_for_status()
                statuses_data = st_resp.json()
                status_runs = _statuses_to_check_runs(statuses_data)
            if not check_runs:
                check_runs = status_runs

            # No checks AND no statuses (either truly empty or the
            # App lacks read permission for both endpoints) → there
            # is nothing meaningful to gate on. Treat as success so
            # the merge stage doesn't loop forever.
            #
            # When *require_checks* is True the caller knows the repo
            # MUST have CI (e.g. ci-fix after a push).  An empty list
            # here means check runs haven't been registered YET, not
            # that the repo lacks CI — classify as "pending" so the
            # caller keeps waiting rather than false-passing.
            wf_runs: list[dict[str, Any]] | None = None
            if not check_runs and not status_runs:
                if require_checks:
                    result = {
                        "conclusion": "pending",
                        "failing": [],
                        "pending": [],
                        "jobs": [],
                        "_no_checks": True,
                    }
                else:
                    # Empty check-runs is NOT yet proof of "no CI": a
                    # just-pushed SHA has a window where its workflow RUN
                    # exists but no check-run is registered, and reading
                    # that window as no-CI green-lit red merges (hexarchy
                    # #286/#287, 2026-09-05 — the merge stage's own
                    # refresh push raced its CI scan). Probe the Actions
                    # API including in-flight runs; only a SHA with zero
                    # runs is treated as a repo without CI.
                    wf_runs = self._safe_list_workflow_runs(
                        owner, repo, sha, status=None
                    )
                    in_flight = [r for r in wf_runs if r.get("conclusion") is None]
                    if in_flight:
                        result = {
                            "conclusion": "pending",
                            "failing": [],
                            "pending": [
                                (r.get("name") or r.get("path") or "workflow")
                                for r in in_flight
                            ],
                            "jobs": [],
                            "_no_checks": True,
                        }
                    else:
                        result = {
                            "conclusion": "success",
                            "failing": [],
                            "pending": [],
                            "jobs": [],
                        }
            else:
                result = _derive_check_conclusion(
                    c, api, owner, repo, headers, check_runs
                )

            # A workflow that fails to PARSE (invalid ``uses:``, malformed
            # YAML) registers ZERO check-runs and posts NO commit status, so
            # its failure is invisible to the aggregation above — the gate
            # reads "success" while the workflow ran not one job (live:
            # cost-monitor 27a2). Cross-check the Actions API: any workflow
            # RUN for this head SHA that concluded failure/startup_failure
            # fails the gate, including runs whose name is the workflow file
            # path (the parse-failure signature).
            _merge_workflow_run_failures(
                result,
                wf_runs
                if wf_runs is not None
                else self._safe_list_workflow_runs(owner, repo, sha),
                functools.partial(
                    _fetch_run_job_failures,
                    c,
                    api,
                    owner,
                    repo,
                    headers,
                    unreadable=unreadable,
                ),
            )
            if unreadable:
                result["checks_unreadable"] = True
                # ci_fix classifies on the ``failing`` list alone.
                failing_entries: Any = result.get("failing") or []
                for entry in failing_entries:
                    entry["checks_unreadable"] = True
            result["_sha"] = sha
            return result
        return None

    def _safe_list_workflow_runs(
        self, owner: str, repo: str, sha: str, status: str | None = "completed"
    ) -> list[dict[str, Any]]:
        """Best-effort ``_list_workflow_runs`` for the parse-failure gate.

        Any error (App missing ``actions: read``, transport failure, an
        empty SHA) resolves to an empty list, so a flaky Actions API can
        never *add* a false failure to the CI gate. ``status=None`` lists
        runs in ANY state (in-flight included) for the no-check-runs probe.
        """
        if not sha:
            return []
        try:
            return self._list_workflow_runs(
                owner=owner, repo=repo, branch=None, head_sha=sha, status=status
            )
        except Exception:
            return []

    def _list_workflow_runs(
        self,
        *,
        owner: str,
        repo: str,
        branch: str | None,
        head_sha: str | None,
        status: str | None = "completed",
        event: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"per_page": 30}
        if status is not None:
            params["status"] = status
        if event is not None:
            params["event"] = event
        if branch is not None:
            params["branch"] = branch
        if head_sha is not None:
            params["head_sha"] = head_sha

        r = self._http.get(  # type: ignore[attr-defined]
            f"/repos/{owner}/{repo}/actions/runs",
            params=params,
        )
        r.raise_for_status()
        raw = r.json().get("workflow_runs", [])
        return [
            {
                "id": run["id"],
                "name": run.get("name", ""),
                "workflow_id": run.get("workflow_id"),
                "head_sha": run.get("head_sha", ""),
                "conclusion": run.get("conclusion"),
                "run_attempt": run.get("run_attempt"),
                "html_url": run.get("html_url", ""),
                "created_at": run.get("created_at", ""),
                "event": run.get("event", ""),
                "head_branch": run.get("head_branch"),
                "path": run.get("path", ""),
            }
            for run in raw
        ]

    def _get_workflow_run(
        self, *, owner: str, repo: str, run_id: int
    ) -> dict[str, Any]:
        r = self._http.get(  # type: ignore[attr-defined]
            f"/repos/{owner}/{repo}/actions/runs/{run_id}",
        )
        r.raise_for_status()
        run = r.json()
        return {
            "id": run["id"],
            "name": run.get("name", ""),
            "path": run.get("path", ""),
            "head_sha": run.get("head_sha", ""),
            "head_branch": run.get("head_branch"),
            "conclusion": run.get("conclusion"),
            "run_attempt": run.get("run_attempt"),
            "html_url": run.get("html_url", ""),
            "created_at": run.get("created_at", ""),
        }

    def _fetch_workflow_job_logs(
        self,
        *,
        owner: str,
        repo: str,
        run_id: int,
        full_log: bool = False,
    ) -> str:
        s = self.settings  # type: ignore[attr-defined]

        # 1. List jobs for the run (with 401 retry).
        jobs: list[Any] = []
        for _retry, c, api, headers in self._http.retrying_client():  # type: ignore[attr-defined]
            jobs_resp = c.get(
                f"{api}/repos/{owner}/{repo}/actions/runs/{run_id}/jobs",
                headers=headers,
                params={"status": "completed"},
            )
            if jobs_resp.status_code == 401:
                continue
            jobs_resp.raise_for_status()
            jobs = jobs_resp.json().get("jobs", [])
            break

        # 2. Filter to failed-like jobs.
        failed_conclusions = frozenset(
            {
                "failure",
                "cancelled",
                "timed_out",
                "action_required",
            }
        )
        failed_jobs = [j for j in jobs if j.get("conclusion") in failed_conclusions][
            :_MAX_FAILED_JOBS
        ]

        if not failed_jobs:
            return ""

        parts: list[str] = []
        log_max = s.ci_log_max_bytes

        # 3. Fetch logs for each failed job (with 401 retry per fetch).
        for j in failed_jobs:
            job_id = j["id"]
            job_name = j.get("name", f"job-{job_id}")
            # Each job-log fetch gets its own 401 retry.
            raw: str = ""
            for _retry, c, api, headers in self._http.retrying_client(  # type: ignore[attr-defined]
                max_retries=2,
            ):
                try:
                    log_resp = c.get(
                        f"{api}/repos/{owner}/{repo}/actions/jobs/{job_id}/logs",
                        headers=headers,
                        follow_redirects=True,
                    )
                    if log_resp.status_code == 401:
                        continue
                    log_resp.raise_for_status()
                    raw = log_resp.text
                except httpx.HTTPStatusError:
                    sc = log_resp.status_code
                    if sc == 403:
                        raw = f"[log fetch failed for job {job_id}: HTTP 403 — App likely missing Actions:Read permission]"
                    else:
                        raw = f"[log fetch failed for job {job_id}: HTTP {sc}]"
                except Exception as exc:
                    raw = f"[log fetch failed for job {job_id}: {type(exc).__name__}]"
                else:
                    if not raw:
                        raw = f"[log fetch returned empty body for job {job_id}]"
                break  # success or final attempt

            # Strip ANSI.
            clean = _ANSI_RE.sub("", raw)
            # Strip runner preamble boilerplate (OS version, runner
            # image, git config, etc.) — pure token saving with zero
            # diagnostic loss.
            clean = _strip_runner_noise(clean)
            # Narrow to the FAILING step (conclusion ``failure``) when its
            # time range maps onto the log: the last N lines of a job are
            # often post-step cleanup noise (setup-uv cache saves) that hide
            # the step that actually failed. Falls back to the whole job log.
            step = _failing_step(j)
            if step is not None and not full_log:
                sliced = _slice_log_to_step(clean, step)
                if sliced.strip():
                    clean = sliced
            # Capture the window around the FIRST failure marker (not a
            # blind tail-cap) so an ``if: always()`` cascade — where a
            # downstream always-step re-errors with misleading input —
            # can't mask the step that actually failed first.
            if not full_log:
                clean = _capture_failure_window(
                    clean,
                    log_max,
                    failure_re=_LOG_FAILURE_RE,
                    tail_context=_LOG_FAILURE_TAIL_CONTEXT,
                )

            header = f"### Job: {job_name} (id={job_id})"
            if step is not None:
                header += f" — failing step: {step.get('name') or '?'}"
            parts.append(header + "\n")
            if j.get("html_url"):
                parts.append(f"Job URL: {j['html_url']}\n")
            parts.append(clean)
            parts.append("\n")

        return "\n".join(parts)

    def _rerun_workflow(self, *, owner: str, repo: str, run_id: int) -> dict[str, Any]:
        """POST /repos/{owner}/{repo}/actions/runs/{run_id}/rerun."""
        for _retry, c, api, headers in self._http.retrying_client():  # type: ignore[attr-defined]
            r = c.post(
                f"{api}/repos/{owner}/{repo}/actions/runs/{run_id}/rerun",
                headers=headers,
            )
            if r.status_code == 401:
                continue
            r.raise_for_status()
            return {"rerun": True}
        return {"rerun": False, "reason": "max retries exhausted (401 loop)"}

    def _approve_workflow(
        self, *, owner: str, repo: str, run_id: int
    ) -> dict[str, Any]:
        """POST /repos/{owner}/{repo}/actions/runs/{run_id}/approve.

        A 403 (approval refused) raises through ``raise_for_status`` so the
        public wrapper can flag it as ``forbidden``.
        """
        for _retry, c, api, headers in self._http.retrying_client():  # type: ignore[attr-defined]
            r = c.post(
                f"{api}/repos/{owner}/{repo}/actions/runs/{run_id}/approve",
                headers=headers,
            )
            if r.status_code == 401:
                continue
            r.raise_for_status()
            return {"approved": True}
        return {"approved": False, "reason": "max retries exhausted (401 loop)"}

    def _list_workflow_run_jobs(
        self, *, owner: str, repo: str, run_id: int
    ) -> list[dict[str, Any]]:
        """GET /repos/{owner}/{repo}/actions/runs/{run_id}/jobs."""
        for _retry, c, api, headers in self._http.retrying_client():  # type: ignore[attr-defined]
            r = c.get(
                f"{api}/repos/{owner}/{repo}/actions/runs/{run_id}/jobs",
                headers=headers,
            )
            if r.status_code == 401:
                continue
            r.raise_for_status()
            jobs = r.json().get("jobs", [])
            return [
                {
                    "id": j.get("id"),
                    "name": j.get("name", ""),
                    "status": j.get("status"),
                    "conclusion": j.get("conclusion"),
                    "steps": [
                        {"name": st.get("name", ""), "conclusion": st.get("conclusion")}
                        for st in j.get("steps") or []
                    ],
                }
                for j in jobs
            ]
        raise RuntimeError("max retries exhausted (401 loop)")

    def _has_online_runner(self, *, owner: str, repo: str) -> bool:
        """GET /repos/{owner}/{repo}/actions/runners → any status==online."""
        for _retry, c, api, headers in self._http.retrying_client():  # type: ignore[attr-defined]
            r = c.get(
                f"{api}/repos/{owner}/{repo}/actions/runners",
                headers=headers,
            )
            if r.status_code == 401:
                continue
            r.raise_for_status()
            runners = r.json().get("runners", [])
            return any(runner.get("status") == "online" for runner in runners)
        return False

    def _get_workflow_triggers(
        self, *, owner: str, repo: str, path: str
    ) -> list[str] | None:
        """GET /repos/{owner}/{repo}/contents/{path}; parse the ``on:`` block.

        Reads the workflow file on the default branch and returns its
        top-level trigger event names.
        """
        r = self._http.get(  # type: ignore[attr-defined]
            f"/repos/{owner}/{repo}/contents/{path}",
        )
        r.raise_for_status()
        payload = r.json()
        raw = payload.get("content") or ""
        if payload.get("encoding") == "base64":
            text = base64.b64decode(raw).decode("utf-8", "replace")
        else:
            text = raw
        data = yaml.safe_load(text)
        if not isinstance(data, dict):
            return None
        # YAML 1.1 (PyYAML) parses the bare key ``on`` as the boolean True,
        # so a workflow's ``on:`` block lands under the True key, not "on".
        on = data.get("on")
        if on is None:
            on = data.get(True)
        return _trigger_event_names(on)

    def _get_workflow_file(self, *, owner: str, repo: str, path: str) -> str:
        """GET /repos/{owner}/{repo}/contents/{path}; return the decoded text."""
        r = self._http.get(  # type: ignore[attr-defined]
            f"/repos/{owner}/{repo}/contents/{path}",
        )
        r.raise_for_status()
        payload = r.json()
        raw = payload.get("content") or ""
        if payload.get("encoding") == "base64":
            return base64.b64decode(raw).decode("utf-8", "replace")
        return raw

    def _list_path_commits_since(
        self, *, owner: str, repo: str, path: str, branch: str, since: str
    ) -> list[str]:
        """GET /repos/{owner}/{repo}/commits?path=&sha=&since= → commit SHAs."""
        r = self._http.get(  # type: ignore[attr-defined]
            f"/repos/{owner}/{repo}/commits",
            params={"path": path, "sha": branch, "since": since, "per_page": 20},
        )
        r.raise_for_status()
        return [c.get("sha", "") for c in r.json() if c.get("sha")]

    def _dispatch_workflow(
        self,
        *,
        owner: str,
        repo: str,
        workflow_id: int | None = None,
        workflow_file: str | None = None,
        ref: str,
    ) -> dict[str, Any]:
        """POST /repos/{owner}/{repo}/actions/workflows/{id_or_file}/dispatches."""
        identifier = workflow_file if workflow_file is not None else workflow_id
        for _retry, c, api, headers in self._http.retrying_client():  # type: ignore[attr-defined]
            r = c.post(
                f"{api}/repos/{owner}/{repo}/actions/workflows/{identifier}/dispatches",
                headers=headers,
                json={"ref": ref},
            )
            if r.status_code == 401:
                continue
            r.raise_for_status()
            return {"dispatched": True}
        return {"dispatched": False, "reason": "max retries exhausted (401 loop)"}
