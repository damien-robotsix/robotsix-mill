"""Deterministic stale-CI gate for ``CI failure: <workflow> on <branch>`` tickets.

The ``ci`` ticket source keeps filing failure tickets whose problem is
already fixed on main; they used to travel refine → approval → implement
only to close as "no change needed" (cost-monitor, 5/5 in a month).  This
gate runs the forge checks *before* a ticket can reach ``ready`` — at filing
time (``poll_loops_ci``), at refine entry and on human approval:

* **green-since** — the workflow has completed green on the target branch
  at or after the failing commit (same rule as ci-auto-close) → close;
* **deleted-workflow** — the workflow file is absent at the target branch
  HEAD → close;
* **stale-run** — the failing run is older than ``ci_stale_failure_days``
  and the workflow file changed on the target branch since → close;
* **upstream-only** — every failing job/step lives inside a reusable
  workflow / composite action from ``robotsix-github-workflows`` → block,
  ci-auto-close closes it once the workflow goes green.  Refused when the
  job log / reusable contract shows the caller starved it of a required
  secret/input (→ ``repo setting required:`` block + one escalation), or
  when a sibling caller of the same reusable ran green within the hour.

No LLM; any forge error degrades to "no verdict" (the ticket proceeds).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import yaml

from ..agents.runners.ci_auto_close_runner import (
    _green_since_failure,
    _resolve_anchors,
    _run_url_id,
    deleted_workflow_note,
    workflow_path_deleted,
)
from ..core.constants import STALE_CI_FAILURE_PREFIX
from ..core.models import SourceKind
from ..core.states import DONE_OR_CLOSED
from .ci_infra_block import REPO_SETTING_BLOCK_MARKER

log = logging.getLogger("robotsix_mill.ci_stale_gate")

_TITLE_RE = re.compile(r"^\s*CI failure: .+ on \S+\s*$", re.IGNORECASE)
_BODY_PATH_RE = re.compile(r"\*\*Path:\*\*\s*(\S+)")

# Shared workflow repo whose reusable workflows / composite actions a caller
# cannot fix from its own tree.
UPSTREAM_WORKFLOWS_REPO = "robotsix-github-workflows"

_FAILED_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})
# Failing steps that run the caller's OWN code (tests, lint, type-check)
# inside a shared workflow are fixable here — never treat them as
# upstream-only.
_CALLER_CODE_STEP_RE = re.compile(
    r"\b(test|tests|pytest|lint|ruff|mypy|pyright|type[- ]?check|format|"
    r"vulture|pre-commit|coverage)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class StaleCiVerdict:
    """Outcome of :func:`evaluate_stale_ci`: ``close`` (DONE) or ``block``."""

    action: Literal["close", "block"]
    note: str
    # Set for a repo-setting block (missing caller secret/variable): the
    # setting to add and the repo to add it on — drives one escalation.
    setting: str | None = None
    repo: str = ""
    # Set for an upstream-only block (every failing job lives inside a
    # robotsix-github-workflows reusable): the pinned ``uses:`` ref
    # (``owner/repo/path@sha``), the failing run-job names and the affected
    # consumer repo — the ingredients of the companion ticket filed on the
    # github-workflows board (see :func:`file_upstream_companion`).
    upstream_ref: str = ""
    failing_jobs: tuple[str, ...] = ()
    consumer: str = ""


def is_ci_failure_ticket(ticket: Any) -> bool:
    """True for a ``source=ci`` ticket or one titled ``CI failure: X on Y``."""
    return ticket.source == SourceKind.CI or bool(_TITLE_RE.match(ticket.title or ""))


def _failing_run(
    runs: list[dict[str, Any]], wf: str, f_sha: str, run_id: int | None
) -> dict[str, Any] | None:
    for r in runs:
        if run_id is not None and r.get("id") == run_id:
            return r
    for r in runs:
        if (r.get("name") or "") == wf and (r.get("head_sha") or "") == f_sha:
            return r
    return None


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _find_job(jobs: dict[str, Any], label: str) -> dict[str, Any] | None:
    """Return the workflow job whose key or ``name`` produced run-job *label*
    (matrix jobs render as ``<name> (<axis>)``).
    """
    for key, job in jobs.items():
        if not isinstance(job, dict):
            continue
        for cand in {str(key), str(job.get("name") or key)}:
            if label == cand or label.startswith(f"{cand} ("):
                return job
    return None


def _upstream_ref(run_job: dict[str, Any], wf_jobs: dict[str, Any]) -> str | None:
    """Return the upstream ``uses:`` ref a failing *run_job* lives in, or None.

    ``None`` whenever the failure may be fixable from this repo: the job is
    not from ``robotsix-github-workflows``, or no failing step is known, or a
    failing step runs the caller's own code (tests / lint / type-check).
    """
    name = str(run_job.get("name") or "")
    failed_steps = [
        str(st.get("name") or "")
        for st in run_job.get("steps") or []
        if (st.get("conclusion") or "") in _FAILED_CONCLUSIONS
    ]
    if not failed_steps or any(_CALLER_CODE_STEP_RE.search(s) for s in failed_steps):
        return None
    if " / " in name:
        # ``<caller> / <reusable job>``: a job of a called reusable workflow.
        job = _find_job(wf_jobs, name.split(" / ", 1)[0])
        uses = str((job or {}).get("uses") or "")
        return uses if UPSTREAM_WORKFLOWS_REPO in uses else None
    job = _find_job(wf_jobs, name)
    if job is None:
        return None
    refs: list[str] = []
    for step_name in failed_steps:
        ref = None
        for st in job.get("steps") or []:
            uses = str((st or {}).get("uses") or "")
            if UPSTREAM_WORKFLOWS_REPO in uses and step_name in (
                str(st.get("name") or ""),
                f"Run {uses}",
            ):
                ref = uses
                break
        if ref is None:
            return None
        refs.append(ref)
    return refs[0] if refs else None


def _upstream_only(
    forge: Any, run_id: int | None, path: str | None
) -> tuple[str, list[dict[str, Any]], list[str]] | None:
    """Return ``(upstream ref, caller job/step dicts, failing job names)``
    when EVERY failing job of *run_id* is internal to a
    ``robotsix-github-workflows`` reusable workflow / composite action.  The
    caller dicts are the workflow-file entries that call the upstream code
    (used to name caller-side secrets); the failing job names name the run
    jobs for the companion ticket filed on the github-workflows board.
    """
    if run_id is None or not path:
        return None
    try:
        jobs = forge.list_workflow_run_jobs(run_id=run_id)
    except Exception:
        return None
    if not isinstance(jobs, list):
        return None
    failing = [j for j in jobs if (j.get("conclusion") or "") in _FAILED_CONCLUSIONS]
    if not failing:
        return None
    text = forge.get_workflow_file(path=path)
    if not isinstance(text, str) or not text:
        return None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    wf_jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(wf_jobs, dict):
        return None
    refs = [_upstream_ref(j, wf_jobs) for j in failing]
    if not refs or any(r is None for r in refs):
        return None
    return (
        str(refs[0]),
        _caller_entries(failing, wf_jobs),
        [str(j.get("name") or "") for j in failing],
    )


def _caller_entries(
    failing: list[dict[str, Any]], wf_jobs: dict[str, Any]
) -> list[dict[str, Any]]:
    """Workflow-file job / step dicts that call the upstream code."""
    callers: list[dict[str, Any]] = []
    for j in failing:
        name = str(j.get("name") or "")
        if " / " in name:
            job = _find_job(wf_jobs, name.split(" / ", 1)[0])
            if job:
                callers.append(job)
            continue
        job = _find_job(wf_jobs, name) or {}
        callers.extend(
            st
            for st in job.get("steps") or []
            if isinstance(st, dict)
            and UPSTREAM_WORKFLOWS_REPO in str(st.get("uses") or "")
        )
    return callers


# --- caller-side evidence (missing secret / variable) -------------------------
#
# A failure inside a reusable is NOT upstream-only when the caller starved it
# of a required secret/input (live 2026-10-28: ``app-id:`` empty at
# ``actions/create-github-app-token`` because the caller repo had neither the
# ``RELEASE_APP_PRIVATE_KEY`` secret nor the ``RELEASE_APP_ID`` variable).
# That is a repository setting the operator must add.

_LOG_TS_RE = re.compile(r"^\d{4}-\d\d-\d\dT\S+Z ?")
_WITH_ENTRY_RE = re.compile(r"^\s+([A-Za-z0-9_.-]+):(.*)$")
_INPUT_NOT_SUPPLIED_RE = re.compile(
    r"Input required and not supplied:?\s*([A-Za-z0-9_-]+)?", re.IGNORECASE
)
_INPUT_REQUIRED_RE = re.compile(r"Error: Input required", re.IGNORECASE)
_SECRET_NOT_FOUND_RE = re.compile(
    r"secret\s+[`'\"]?([A-Za-z0-9_-]+)[`'\"]?\s+(?:was\s+)?not\s+found",
    re.IGNORECASE,
)
_CALLER_REF_RE = re.compile(r"\$\{\{\s*(secrets|vars)\.([A-Za-z0-9_]+)\s*\}\}")


def _clean_lines(log_text: str) -> list[str]:
    return [_LOG_TS_RE.sub("", ln).rstrip() for ln in log_text.splitlines()]


def _empty_failing_step_inputs(log_text: str) -> list[str]:
    """Input names echoed EMPTY in the ``with:`` block of the first failing
    step (the last ``##[group]Run`` before the first ``##[error]``).
    """
    lines = _clean_lines(log_text)
    err = next((i for i, ln in enumerate(lines) if "##[error]" in ln), len(lines))
    start = next(
        (i for i in range(err - 1, -1, -1) if lines[i].startswith("##[group]Run")),
        None,
    )
    if start is None:
        return []
    empty: list[str] = []
    in_with = False
    for ln in lines[start + 1 : err]:
        if ln.startswith("##[endgroup]"):
            break
        if ln.strip() == "with:":
            in_with = True
            continue
        if not in_with:
            continue
        m = _WITH_ENTRY_RE.match(ln)
        if not m:
            in_with = False
            continue
        if not m.group(2).strip() and m.group(1) not in empty:
            empty.append(m.group(1))
    return empty


def _log_evidence(log_text: str) -> list[str]:
    """Caller-side evidence in a failing job log; the list names the starved
    inputs/secrets (``["input app-id"]``) — empty when there is no evidence.
    """
    if not log_text:
        return []
    found = [f"input {n}" for n in _empty_failing_step_inputs(log_text)]
    for m in _INPUT_NOT_SUPPLIED_RE.finditer(log_text):
        found.append(f"input {m.group(1)}" if m.group(1) else "a required input")
    for m in _SECRET_NOT_FOUND_RE.finditer(log_text):
        found.append(f"secret {m.group(1)}")
    if not found and _INPUT_REQUIRED_RE.search(log_text):
        found.append("a required input")
    return list(dict.fromkeys(found))


def _workflow_call_contract(text: str) -> tuple[set[str], set[str]]:
    """``(required secrets, required inputs)`` of a reusable workflow."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return set(), set()
    if not isinstance(data, dict):
        return set(), set()
    on = data.get("on", data.get(True))  # PyYAML parses ``on:`` as True
    call = on.get("workflow_call") if isinstance(on, dict) else None
    if not isinstance(call, dict):
        return set(), set()

    def _required(section: Any) -> set[str]:
        if not isinstance(section, dict):
            return set()
        return {
            str(k)
            for k, v in section.items()
            if isinstance(v, dict) and v.get("required") is True
        }

    return _required(call.get("secrets")), _required(call.get("inputs"))


def _read_upstream(forge: Any, ref: str) -> str | None:
    """Best-effort read of the reusable *ref* (``owner/repo/path@sha``) from
    the upstream repo's default branch; ``None`` when unavailable.
    """
    target = ref.split("@", 1)[0]
    parts = target.split("/", 2)
    reader = getattr(forge, "_get_workflow_file", None)
    if len(parts) != 3 or reader is None:
        return None
    try:
        text = reader(owner=parts[0], repo=parts[1], path=parts[2])
    except Exception:
        return None
    return text if isinstance(text, str) else None


def _contract_evidence(
    forge: Any, ref: str, callers: list[dict[str, Any]]
) -> list[str]:
    """Required ``secrets:`` / ``inputs:`` of the reusable *ref* that the
    caller job does not pass.
    """
    if "/.github/workflows/" not in ref:
        return []
    text = _read_upstream(forge, ref)
    if not text:
        return []
    req_secrets, req_inputs = _workflow_call_contract(text)
    missing: list[str] = []
    for job in callers:
        passed_secrets = job.get("secrets")
        if passed_secrets != "inherit":
            given = set(passed_secrets) if isinstance(passed_secrets, dict) else set()
            missing += [f"secret {n}" for n in sorted(req_secrets - given)]
        with_ = job.get("with")
        given_inputs = set(with_) if isinstance(with_, dict) else set()
        missing += [f"input {n}" for n in sorted(req_inputs - given_inputs)]
    return list(dict.fromkeys(missing))


def _caller_settings(callers: list[dict[str, Any]]) -> list[str]:
    """``secret X`` / ``variable Y`` names the caller passes to the upstream
    code (``${{ secrets.X }}`` / ``${{ vars.Y }}``), ``GITHUB_TOKEN`` excluded.
    """
    names: list[str] = []
    for c in callers:
        for kind, name in _CALLER_REF_RE.findall(yaml.safe_dump(c)):
            if name == "GITHUB_TOKEN":
                continue
            names.append(f"{'secret' if kind == 'secrets' else 'variable'} {name}")
    return sorted(dict.fromkeys(names), key=lambda n: (n.startswith("variable"), n))


def _green_sibling(
    siblings: Iterable[tuple[str, Any]],
    ref: str,
    branch: str,
    around: datetime | None,
) -> str | None:
    """Label of a sibling repo whose caller of the same reusable at the same
    SHA ran green on *branch* within an hour of *around*, else ``None``.
    """
    if around is None:
        return None
    for label, sib in siblings:
        try:
            runs = sib.list_workflow_runs(branch=branch)
        except Exception:
            log.debug("ci_stale_gate: sibling %s runs unavailable", label)
            continue
        for r in runs if isinstance(runs, list) else []:
            if (r.get("conclusion") or "") != "success" or not r.get("path"):
                continue
            ts = _parse_ts(r.get("created_at"))
            if ts is None or abs(ts - around) > timedelta(hours=1):
                continue
            try:
                text = sib.get_workflow_file(path=r["path"])
            except Exception:
                log.debug("ci_stale_gate: sibling %s workflow unreadable", label)
                continue
            if isinstance(text, str) and ref in text:
                return label
    return None


def _forge_repo(forge: Any) -> str:
    try:
        owner, name = forge._owner_repo
        return f"{owner}/{name}"
    except Exception:
        return "this repo"


def repo_setting_note(repo: str, setting: str, sibling: str | None) -> str:
    """``BLOCKED`` note for a reusable starved of a caller secret/variable."""
    see = f" (see {sibling}, whose caller runs green)" if sibling else ""
    return (
        f"{REPO_SETTING_BLOCK_MARKER} add {setting} on {repo}{see}. The failure "
        "is inside a shared reusable workflow, but the caller does not supply "
        "a required secret/variable — not an upstream bug. Not auto-resumed: "
        "add it in Settings > Secrets and variables > Actions, then resume "
        "the ticket (/transition ready)."
    )


def evaluate_stale_ci(
    forge: Any,
    ticket: Any,
    body: str,
    *,
    stale_days: int,
    now: datetime | None = None,
    siblings: Callable[[], Iterable[tuple[str, Any]]] | None = None,
) -> StaleCiVerdict | None:
    """Run the three stale-CI checks for *ticket*; ``None`` = proceed normally.

    *siblings* lazily yields ``(label, forge)`` for the other registered
    repos; it is only consulted when a failure looks upstream-only.
    """
    if not is_ci_failure_ticket(ticket):
        return None
    wf, branch, f_sha, created, _ = _resolve_anchors(forge, ticket, body)
    if not wf or not branch or not f_sha:
        return None
    try:
        runs = forge.list_workflow_runs(branch=branch)
    except Exception:
        log.warning("ci_stale_gate: list_workflow_runs failed for %s", ticket.id)
        runs = []
    if not isinstance(runs, list):
        runs = []

    # 1. Green-since check.
    green, _ = _green_since_failure(runs, wf, f_sha, failing_created_at=created)
    if green:
        url = green[0].get("html_url") or green[0].get("id")
        return StaleCiVerdict(
            "close",
            f"{STALE_CI_FAILURE_PREFIX} workflow '{wf}' is green on {branch} at "
            f"or after the failing commit {f_sha} (green run: {url}). "
            "No change needed.",
        )

    run_id = _run_url_id(body)
    failing = _failing_run(runs, wf, f_sha, run_id)
    if run_id is None and failing is not None:
        run_id = failing.get("id")
    m = _BODY_PATH_RE.search(body or "")
    path = m.group(1) if m else (failing or {}).get("path")
    created = created or (failing or {}).get("created_at")

    # 2. Deleted-workflow check: the file is gone at branch HEAD, so the
    # failure can never reproduce (or go green) again.
    if path and workflow_path_deleted(forge, path, branch):
        return StaleCiVerdict(
            "close",
            f"{STALE_CI_FAILURE_PREFIX} {deleted_workflow_note(path, branch)}",
        )

    # 3. Stale-run check.
    created_ts = _parse_ts(created)
    now = now or datetime.now(UTC)
    if created_ts and path and now - created_ts > timedelta(days=stale_days):
        commits = forge.list_path_commits_since(
            path=path, branch=branch, since=created_ts.isoformat()
        )
        if isinstance(commits, list) and commits:
            return StaleCiVerdict(
                "close",
                f"{STALE_CI_FAILURE_PREFIX} workflow '{wf}' failed at {f_sha} "
                f"more than {stale_days} days ago and {path} changed on "
                f"{branch} since (commit {commits[0]}). No change needed.",
            )

    # 4. Upstream-only check.
    return _upstream_verdict(forge, run_id, path, wf, branch, created_ts, siblings)


def _upstream_verdict(
    forge: Any,
    run_id: int | None,
    path: str | None,
    wf: str,
    branch: str,
    around: datetime | None,
    siblings: Callable[[], Iterable[tuple[str, Any]]] | None,
) -> StaleCiVerdict | None:
    """Upstream-only check, refused on caller-side evidence (→ repo-setting
    block) or on a sibling caller that ran green within the hour (→ proceed).
    """
    upstream = _upstream_only(forge, run_id, path)
    if not upstream:
        return None
    ref, callers, failing_jobs = upstream
    sibs: Iterable[tuple[str, Any]] = ()
    if siblings is not None:
        try:
            sibs = siblings()
        except Exception:
            log.warning("ci_stale_gate: sibling lookup failed", exc_info=True)
    sibling = _green_sibling(sibs, ref, branch, around)
    try:
        log_text = forge.fetch_workflow_job_logs(run_id=run_id)
    except Exception:
        log_text = ""
    evidence = _log_evidence(log_text if isinstance(log_text, str) else "")
    evidence += _contract_evidence(forge, ref, callers)
    if evidence:
        named = _caller_settings(callers) or list(dict.fromkeys(evidence))
        setting = " / ".join(named)
        repo = _forge_repo(forge)
        return StaleCiVerdict(
            "block",
            repo_setting_note(repo, setting, sibling),
            setting=setting,
            repo=repo,
        )
    if sibling:
        # Same reusable at the same SHA ran green elsewhere this hour: the
        # reusable is not the cause — let the ticket proceed.
        return None
    return StaleCiVerdict(
        "block",
        f"Not fixable from this repo: failure is internal to {ref}. "
        f"ci-auto-close will close this once '{wf}' goes green on {branch}.",
        upstream_ref=ref,
        failing_jobs=tuple(failing_jobs),
        consumer=_forge_repo(forge),
    )


def sibling_forges(
    settings: Any, repo_config: Any
) -> Callable[[], list[tuple[str, Any]]]:
    """Lazy ``(repo_id, forge)`` list of the registered repos other than
    *repo_config* — the sibling callers of a shared reusable workflow.
    """

    def _resolve() -> list[tuple[str, Any]]:
        from ..config.repos import get_repos_config
        from ..scm.forge import get_forge

        own = getattr(repo_config, "repo_id", None)
        out: list[tuple[str, Any]] = []
        for rid, rc in get_repos_config().repos.items():
            if rid == own:
                continue
            try:
                out.append((rid, get_forge(settings, repo_config=rc)))
            except Exception:
                log.debug("ci_stale_gate: no forge for sibling %s", rid)
                continue
        return out

    return _resolve


def escalate_verdict(settings: Any, verdict: StaleCiVerdict, ticket_id: str) -> None:
    """Raise the one operator escalation a repo-setting verdict needs."""
    if not verdict.setting:
        return
    from ..agents.runners.repo_setting_block_runner import (
        escalate_repo_setting_block,
    )

    try:
        escalate_repo_setting_block(
            settings,
            verdict.repo,
            verdict.setting,
            ticket_id,
            message=(
                f"OPERATOR ESCALATION — repository setting required on "
                f"{verdict.repo}: add {verdict.setting} "
                f"(https://github.com/{verdict.repo}/settings/secrets/actions). "
                f"CI cannot pass until it is set; ticket {ticket_id} is BLOCKED "
                "and will not auto-resume. After adding it, resume the ticket "
                "(/transition ready) to re-run CI."
            ),
        )
    except Exception:
        log.warning("ci_stale_gate: escalation failed for %s", ticket_id, exc_info=True)


# --- companion ticket on the github-workflows board -------------------------
#
# An upstream-only failure (every failing job inside a robotsix-github-
# workflows reusable) can only go green once the WORKFLOWS repo fixes the
# reusable and the consumer advances its pin — the consumer repo cannot fix
# it.  ``block until green, auto-close when green`` alone discards the
# evidence the moment the owning side fixes it (the consumer ticket closes
# with no trace reaching the workflows repo).  So the ci source guarantees a
# companion failure ticket on the github-workflows board naming the reusable,
# the pinned SHA, the failing job and the affected consumer.

_UPSTREAM_COMPANION_MARKER_RE = re.compile(r"<!-- ci-upstream: (.*?) -->")


def _companion_marker(consumer: str, ref: str) -> str:
    """Hidden HTML-comment dedup marker embedding the companion's identity."""
    return f"<!-- ci-upstream: consumer={consumer};ref={ref} -->"


def _find_open_upstream_companion(service: Any, consumer: str, ref: str) -> Any | None:
    """Return an open companion ticket on *service*'s board that already
    names the same (consumer, pinned ref), else ``None`` — the dedup key that
    makes filing idempotent across the gate's three call sites (filing,
    refine entry, human approval).
    """
    for t in service.list():
        if t.state in DONE_OR_CLOSED:
            continue
        try:
            desc = service.workspace(t).read_description() or ""
        except Exception:
            log.debug(
                "ci_stale_gate: companion dedup — unreadable description for %s",
                t.id,
            )
            continue
        m = _UPSTREAM_COMPANION_MARKER_RE.search(desc)
        if not m:
            continue
        fields = dict(
            part.split("=", 1) for part in m.group(1).split(";") if "=" in part
        )
        if fields.get("consumer") == consumer and fields.get("ref") == ref:
            return t
    return None


def _companion_text(verdict: StaleCiVerdict) -> tuple[str, str]:
    """``(title, body)`` of the companion ticket for an upstream-only verdict.

    Names the reusable workflow file, the pinned ``uses:`` ref (``@sha``),
    the failing run-job(s) and the affected consumer — the four facts the
    workflows-repo maintainer needs to reproduce and fix the regression.
    """
    ref = verdict.upstream_ref
    path, _, sha = ref.partition("@")
    file = path.rsplit("/", 1)[-1] if path else ref
    sha8 = sha[:8]
    title = (
        f"Reusable workflow failure: {file}@{sha8} for {verdict.consumer}"
        if sha8
        else f"Reusable workflow failure: {file} for {verdict.consumer}"
    )
    jobs = "; ".join(verdict.failing_jobs) or "n/a"
    body = (
        f"**Automated companion ticket for an upstream CI failure.**\n\n"
        f"A main-CI failure in `{verdict.consumer}` was classified as "
        f"internal to a reusable workflow owned by this repo; the consumer "
        f"cannot fix it. Green on the consumer only returns when this repo "
        f"fixes the reusable AND the consumer advances its pin.\n\n"
        f"- **Reusable:** `{path}`\n"
        f"- **Pinned ref:** `{ref}`\n"
        f"- **Failing job(s):** `{jobs}`\n"
        f"- **Affected consumer:** `{verdict.consumer}`\n\n"
        f"The consumer-board CI ticket stays blocked until its workflow "
        f"goes green on main, then closes automatically. Track and fix the "
        f"regression here.\n\n"
        f"{_companion_marker(verdict.consumer, verdict.upstream_ref)}\n"
    )
    return title, body


def file_upstream_companion(settings: Any, verdict: StaleCiVerdict) -> bool:
    """Guarantee a companion failure ticket exists for an upstream-only
    *verdict* on the github-workflows board.

    Returns ``True`` when the companion exists (filed now, or already open
    from a previous gate pass — the dedup key is the consumer + pinned ref),
    ``False`` when it could not be resolved or filed.  Best-effort and never
    raises: a companion failure must not break the gate's own decision.
    """
    if not (verdict.upstream_ref and verdict.consumer):
        return False
    try:
        from .ci_fix_report import _github_workflows_board_id

        board_id = _github_workflows_board_id()
    except Exception:
        log.warning(
            "ci_stale_gate: could not resolve github-workflows board",
            exc_info=True,
        )
        return False
    from ..core.service import TicketService

    service = TicketService(settings, board_id=board_id)
    try:
        if _find_open_upstream_companion(
            service, verdict.consumer, verdict.upstream_ref
        ):
            return True
    except Exception:
        # A lookup failure should not suppress the trace — file anyway (a
        # duplicate is preferable to no companion).
        log.warning("ci_stale_gate: companion dedup lookup failed", exc_info=True)
    title, body = _companion_text(verdict)
    try:
        service.create(title, body, source=SourceKind.AGENT)
    except Exception:
        log.warning(
            "ci_stale_gate: could not file companion for %s",
            verdict.upstream_ref,
            exc_info=True,
        )
        return False
    return True


def check_ticket(
    settings: Any, repo_config: Any, ticket: Any, body: str
) -> StaleCiVerdict | None:
    """Resolve the forge for *repo_config* and run :func:`evaluate_stale_ci`.

    Never raises — any error returns ``None`` (the ticket proceeds).
    """
    if not is_ci_failure_ticket(ticket):
        return None
    try:
        from ..scm.forge import get_forge

        forge = get_forge(settings, repo_config=repo_config)
        verdict = evaluate_stale_ci(
            forge,
            ticket,
            body,
            stale_days=settings.ci_stale_failure_days,
            siblings=sibling_forges(settings, repo_config),
        )
    except Exception:
        log.warning("ci_stale_gate: check failed for %s", ticket.id, exc_info=True)
        return None
    if verdict is not None:
        escalate_verdict(settings, verdict, ticket.id)
        if verdict.upstream_ref:
            file_upstream_companion(settings, verdict)
    return verdict
