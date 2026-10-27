"""Dedup + routing for CI-failure reports the ci_fix agent files.

When the ci_fix agent decides a failure is pre-existing / out of scope it
files a report through ``report_issue``.  Left alone, every ci_fix session
that sees the same broken run files its own differently-worded ticket
(robotsix-invest, 2026-09-23: six "CI + Lint Workflows startup_failure,
pre-existing" tickets in 20 minutes, all later closed by the scope-triage
repo-awareness gate because they spoke mill vocabulary).

:func:`handle_ci_fix_report` intercepts those reports:

* **Dedup** — a report carries a hidden marker with its dedup key
  (repo + workflow(s) + run id set).  A new report that shares a run id
  with an open or recently (24 h) closed report on the same repo — or is a
  ``startup_failure`` of the same workflow within 24 h — becomes a comment
  on that ticket instead of a new one.
* **Routing** — a failure whose failing ``uses:`` is a
  ``damien-robotsix/robotsix-github-workflows`` reusable workflow is filed
  on the github-workflows board; a GitHub-side failure (billing / permission
  text, no check-run registered — see :mod:`.ci_infra_block`) is not filed
  at all (the infra/account-block path owns it); anything else lands on the
  repo's own board.
* **Wording** — mill pipeline vocabulary (``ci_fix``, "implement agent",
  …) is scrubbed so the target repo's scope-triage gate does not reject it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..config import Settings
from ..core.models import SourceKind, Ticket
from ..core.service import TicketService
from ..core.states import DONE_OR_CLOSED
from ..runtime import tracing as _tracing
from .ci_infra_block import _ACCOUNT_BLOCK_PATTERNS
from .ci_stale_gate import UPSTREAM_WORKFLOWS_REPO

log = logging.getLogger(__name__)

# repo_id (and conventional board id) of the shared reusable-workflow repo.
GITHUB_WORKFLOWS_REPO_ID = UPSTREAM_WORKFLOWS_REPO

REPORT_DEDUP_WINDOW = timedelta(hours=24)

ROUTE_REPO = "repo"
ROUTE_GITHUB_WORKFLOWS = "github_workflows"
ROUTE_INFRA = "infra"

_MARKER_RE = re.compile(r"<!-- ci-report: (.*?) -->")
_RUN_ID_RE = re.compile(
    r"/actions/runs/(\d+)|\bruns?(?:[ _-]?ids?)?\s*[:#=]?\s*(\d{6,})\b",
    re.IGNORECASE,
)
_WORKFLOW_FILE_RE = re.compile(r"\.github/workflows/([\w.-]+\.ya?ml)")
_STARTUP_FAILURE_RE = re.compile(r"startup[_ -]failure", re.IGNORECASE)
_REUSABLE_USES_RE = re.compile(
    r"damien-robotsix/robotsix-github-workflows/\.github/workflows/([\w.-]+\.ya?ml)"
)
# GitHub-side (account / infra) signatures beyond the billing annotations.
_INFRA_TEXT_RE = re.compile(
    r"\bno check[- ]runs?\b|\bbilling\b|resource not accessible by integration|"
    r"\b(?:missing|insufficient) permissions?\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CiReport:
    """Identity of a CI-failure report: the dedup key's ingredients."""

    repo: str
    workflows: frozenset[str]
    run_ids: frozenset[str]
    startup_failure: bool
    reusable_workflow: str | None = None

    def marker(self) -> str:
        """Hidden HTML-comment marker embedding the dedup key."""
        return (
            f"<!-- ci-report: repo={self.repo};"
            f"workflows={','.join(sorted(self.workflows))};"
            f"runs={','.join(sorted(self.run_ids))};"
            f"startup={int(self.startup_failure)} -->"
        )

    def matches(self, other: CiReport) -> bool:
        """Same repo and same run id(s), or same workflow startup_failure."""
        if self.repo != other.repo:
            return False
        if self.run_ids & other.run_ids:
            return True
        return (
            self.startup_failure
            and other.startup_failure
            and self.workflows == other.workflows
        )


def parse_ci_report(repo: str, *texts: str) -> CiReport | None:
    """Extract a :class:`CiReport` from free text, or ``None`` when the text
    names neither a workflow run id nor a ``startup_failure``.
    """
    text = "\n".join(t for t in texts if t)
    run_ids = frozenset(a or b for a, b in _RUN_ID_RE.findall(text))
    startup = bool(_STARTUP_FAILURE_RE.search(text))
    if not run_ids and not startup:
        return None
    reusable = _REUSABLE_USES_RE.search(text)
    workflows = {
        m.group(1)
        for m in _WORKFLOW_FILE_RE.finditer(text)
        if not text[: m.start()].endswith("robotsix-github-workflows/")
    }
    return CiReport(
        repo=repo,
        workflows=frozenset(workflows),
        run_ids=run_ids,
        startup_failure=startup,
        reusable_workflow=reusable.group(1) if reusable else None,
    )


def _parse_marker(text: str) -> CiReport | None:
    m = _MARKER_RE.search(text or "")
    if not m:
        return None
    fields = dict(part.split("=", 1) for part in m.group(1).split(";") if "=" in part)

    def _set(key: str) -> frozenset[str]:
        return frozenset(v for v in fields.get(key, "").split(",") if v)

    return CiReport(
        repo=fields.get("repo", ""),
        workflows=_set("workflows"),
        run_ids=_set("runs"),
        startup_failure=fields.get("startup") == "1",
    )


def route_ci_report(report: CiReport, text: str) -> str:
    """Classify *report* by cause: reusable workflow, GitHub-side, or repo."""
    if report.reusable_workflow:
        return ROUTE_GITHUB_WORKFLOWS
    if _INFRA_TEXT_RE.search(text) or any(
        p.search(text) for p in _ACCOUNT_BLOCK_PATTERNS
    ):
        return ROUTE_INFRA
    return ROUTE_REPO


def scrub_pipeline_vocabulary(text: str) -> str:
    """Replace mill pipeline vocabulary with repo-neutral wording."""
    from .refine.gates import (
        _SCOPE_TRIAGE_DOMAIN_TOKENS,
        _SCOPE_TRIAGE_STAGE_PHRASE_RE,
    )

    text = _SCOPE_TRIAGE_STAGE_PHRASE_RE.sub("CI automation", text)
    tokens = sorted(_SCOPE_TRIAGE_DOMAIN_TOKENS, key=len, reverse=True)
    token_re = re.compile(
        r"(?<![\w-])(?:" + "|".join(re.escape(t) for t in tokens) + r")(?![\w-])",
        re.IGNORECASE,
    )
    return token_re.sub("CI automation", text)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def find_existing_report(
    service: TicketService, report: CiReport, now: datetime | None = None
) -> Ticket | None:
    """Return an open, or closed-within-24h, report matching *report*.

    A ``startup_failure``-by-workflow match (no shared run id) only counts
    for reports created within the same 24 h window.
    """
    now = now or datetime.now(UTC)
    cutoff = now - REPORT_DEDUP_WINDOW
    for t in service.list():
        terminal = t.state in DONE_OR_CLOSED
        if terminal and _aware(t.updated_at) < cutoff:
            continue
        try:
            existing = _parse_marker(service.workspace(t).read_description())
        except Exception:
            log.debug("ci report dedup: unreadable description for %s", t.id)
            continue
        if existing is None or not report.matches(existing):
            continue
        if not (report.run_ids & existing.run_ids) and _aware(t.created_at) < cutoff:
            continue
        return t
    return None


def _github_workflows_board_id() -> str:
    """Board of the configured github-workflows repo (fallback: its repo id)."""
    try:
        from ..config.repos import load_repos_config

        rc = load_repos_config().repos.get(GITHUB_WORKFLOWS_REPO_ID)
        if rc is not None and rc.board_id:
            return rc.board_id
    except Exception:
        log.warning("ci report routing: could not load repos config", exc_info=True)
    return GITHUB_WORKFLOWS_REPO_ID


def handle_ci_fix_report(
    settings: Settings,
    *,
    board_id: str,
    title: str,
    body: str,
    category: str,
    evidence: str = "",
) -> str | None:
    """Dedup/route a ci_fix CI-failure report.

    Returns the tool result string, or ``None`` when the report is not about
    a CI run (no run id / startup_failure) so the caller files it normally.
    """
    repo = board_id or "default"
    report = parse_ci_report(repo, title, body, evidence)
    if report is None:
        return None
    route = route_ci_report(report, f"{title}\n{body}\n{evidence}")
    if route == ROUTE_INFRA:
        return (
            "report_issue: not filed — GitHub-side account/infra failure "
            "(billing / permission / no check-run); the infra-block path "
            "handles it, a ticket would not help"
        )
    target_board = (
        _github_workflows_board_id() if route == ROUTE_GITHUB_WORKFLOWS else board_id
    )
    service = TicketService(settings, board_id=target_board)

    existing = find_existing_report(service, report)
    if existing is not None:
        runs = ", ".join(sorted(report.run_ids)) or "n/a"
        service.add_comment(
            existing.id,
            f"Same CI failure seen again on `{repo}` (run id(s): {runs}).\n\n"
            f"{scrub_pipeline_vocabulary((body or title).strip())}",
            author="agent",
        )
        return (
            f"report_issue: appended to existing report {existing.id} "
            f"(same repo/workflow/run) — not duplicating"
        )

    header = f"**Automated CI failure report** for `{repo}` (category: {category})"
    if report.reusable_workflow:
        header += (
            f"\n\nFailing reusable workflow: "
            f"`damien-robotsix/robotsix-github-workflows/.github/workflows/"
            f"{report.reusable_workflow}`"
        )
    full_body = (
        f"{header}\n\n{scrub_pipeline_vocabulary((body or '').strip())}\n\n"
        f"{report.marker()}\n"
    )
    origin_session = _tracing.current_session()
    priority = False
    if origin_session:
        origin = TicketService(settings, board_id=board_id).get(origin_session)
        priority = bool(origin and origin.priority)
    ticket = service.create(
        scrub_pipeline_vocabulary(title.strip()),
        full_body,
        source=SourceKind.AGENT,
        origin_session=origin_session,
        priority=priority,
    )
    if evidence and evidence.strip():
        from ..agents.report_issue import _attach_evidence

        _attach_evidence(service, ticket, full_body, evidence)
    return f"report_issue: filed draft {ticket.id} on board {target_board}"
