"""The audit agent: meta-audit to identify gaps in quality/security
tooling coverage.

Seam: tests monkeypatch ``run_audit_agent``. Structured output so
the runner has a clear result to work with.
"""

from __future__ import annotations

from ..config import Settings
from .periodic_base import PeriodicAgentResult, load_periodic_system_prompt

# Re-export SYSTEM_PROMPT for tests (loaded from YAML without env-var resolution)
SYSTEM_PROMPT: str = load_periodic_system_prompt("audit")


MAX_GAPS = 5


AuditResult = PeriodicAgentResult


def run_audit_agent(
    *,
    settings: Settings,
    memory: str = "",
    recent_proposals: str = "",
    verified_proposals: str = "",
    repo_dir=None,
    definition_override=None,
) -> AuditResult:
    """Run the meta-audit pass.

    Audits the repository through two complementary lenses —
    codebase health / maintainability (lens A, by reading the actual
    code) and tooling / security coverage (lens B, using
    ``web_research`` for external best practices) — and returns a
    structured ``AuditResult`` with draft tickets.  For recurring
    quality dimensions the agent proposes dedicated standing
    checkers rather than emitting per-instance remediation tickets.

    When ``repo_dir`` is provided, the agent gets filesystem tools
    (``read_file``, ``list_dir``) and the ``explore`` scout tool so
    it can inspect the actual codebase.  Without ``repo_dir`` the
    agent runs in a read-only reasoning mode (no repo access).

    The agent is constructed via
    :func:`~.periodic_base.run_periodic_agent` with the role-specific
    ``SYSTEM_PROMPT``, structured output type
    ``PromptedOutput(AuditResult)`` (for provider compatibility),
    ``web=True`` (for the ``web_research`` sub-agent tool),
    ``report_issue=False`` (the agent emits drafts through its
    structured output instead), and
    ``model_name=settings.audit_model``.

    Execution is wrapped in :func:`~.retry.call_with_retry`, which
    handles transient network/model failures (exponential backoff:
    2s base, 30s cap) and ``UsageLimitExceeded`` rate-limit errors
    (30s base, 120s cap with provider fallback after
    ``settings.rate_limit_fallback_retries`` consecutive failures).

    Args:
        settings: Application configuration — model names
            (``audit_model``), retry parameters, forge URL, and
            tool paths.
        memory: The agent's memory ledger as a Markdown string.
            Defaults to ``""`` (the agent starts a fresh ledger).
        repo_dir: Optional path to the local repository clone.
            When not ``None``, enables the ``explore``,
            ``read_file``, and ``list_dir`` tools.

    Returns:
        An ``AuditResult`` with draft titles, bodies, and gap IDs
        clipped to ``MAX_GAPS`` (5) entries, plus the updated memory
        ledger.
    """
    from pydantic_ai.usage import UsageLimits

    from .periodic_base import run_periodic_agent

    limits = UsageLimits(request_limit=settings.audit_request_limit)
    return run_periodic_agent(
        settings=settings,
        definition_name="audit",
        definition_override=definition_override,
        model_setting=settings.audit_model,
        max_gaps=MAX_GAPS,
        repo_dir=repo_dir,
        memory=memory,
        recent_proposals=recent_proposals,
        verified_proposals=verified_proposals,
        prompt_tail="Perform the audit and return your result.",
        include_forge_url=True,
        include_jscpd=True,
        include_workflow_caller_audit=True,
        include_run_command=True,
        usage_limits=limits,
    )
