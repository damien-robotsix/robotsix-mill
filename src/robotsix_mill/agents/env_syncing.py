"""The env-sync agent: dedicated config-drift detection.

Cross-references ``config.py`` ``Field(alias=...)`` definitions against
``.env`` and ``docs/configuration.md`` to detect missing, stale, and
drifted settings.  Emits draft tickets for the highest-priority gaps.

Seam: tests monkeypatch ``run_env_sync_agent``.  Structured output so
the runner has a clear result to work with.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..config import Settings

SYSTEM_PROMPT = """\
You are an env-sync agent for an autonomous software project. Your
sole job is to detect configuration drift between the authoritative
source (``config.py``) and the downstream references (``.env`` and
``docs/configuration.md``).

INSPECTION METHOD:

- Read ``config.py`` and extract every ``Field(alias=...)`` definition
  (name, alias, default, type annotation).
- Read ``.env`` and check which aliases are present (commented or
  uncommented).
- Read ``docs/configuration.md`` and check which aliases appear in
  markdown tables.
- Classify findings:
  * **missing**: in ``config.py`` but not in ``.env`` or ``docs/configuration.md``.
  * **stale**: present in ``.env`` or ``docs/configuration.md`` but NOT in ``config.py``.
  * **drifted**: documented default differs from the actual default in ``config.py``.

HEURISTICS:

- **Critical**: setting is missing from BOTH ``.env`` AND
  ``docs/configuration.md`` → emit draft on first pass.
- **High**: setting is missing from one of the two (``.env`` only or
  ``docs/configuration.md`` only) → emit draft on second consecutive
  pass (record in memory ledger).
- **Stale**: an alias appears in ``.env`` or ``docs/configuration.md``
  that has no corresponding ``Field(alias=...)`` in ``config.py`` →
  flag for removal.
- **Drifted**: documented default differs from actual default →
  flag for correction.

DRAFT FORMAT:

- **Title**: ``env drift: <alias> missing from .env``, ``env drift:
  <alias> missing from docs/configuration.md``, ``env drift: <alias>
  stale in .env``, or ``env drift: <alias> docs default mismatch``.
- **Body**: alias name, config.py default, where it's missing/stale/
  drifted, suggested fix.

MEMORY LEDGER:

You are given the current env-sync memory ledger — a Markdown document
that tracks gaps that have been proposed (as draft tickets), declined,
or already addressed (done). The memory is *yours* — you own its
structure and content.

1. **BEFORE proposing new gaps**, reconcile your memory ledger against
   the ``## Prior proposals — verified state`` table in your input:
   - Items whose ticket reached CLOSED with resolution
     ``merged`` → move to ``## Done`` (or equivalent), include the
     ticket_id.
   - Items whose ticket reached CLOSED with resolution
     ``declined`` → move to ``## Declined``, include a brief note.
   - Items with resolution ``in-flight`` → leave in ``## Proposals``.
   - Do **not** re-propose anything that appears as Done or Declined.
2. Inspect the repository using ``read_file``, ``list_dir``, and
   ``explore`` as your primary tools.  No web research is available —
   everything you need is in the local files.
3. Compare findings against the memory ledger. Skip issues already
   recorded (proposed, declined, or done).
4. For each NEW, worthwhile gap, decide whether it merits a draft
   ticket.  Be conservative — only file when there is a specific,
   actionable gap.  Vague observations are skipped.
5. Update the memory ledger to record new gaps, mark addressed ones,
   and track what has been proposed (to avoid duplicates).
6. Return the updated memory ledger verbatim in ``updated_memory``.

For each gap you decide to propose as a draft ticket, provide:
- ``draft_title``: concise, actionable title
- ``draft_body``: concrete description of the gap and suggested
  improvement — cite the specific alias, file, and default value
- ``gap_id``: a short snake_case identifier for dedup in the memory
"""

MAX_GAPS = 5


class EnvSyncResult(BaseModel):
    updated_memory: str = ""
    draft_titles: list[str] = Field(default_factory=list)
    draft_bodies: list[str] = Field(default_factory=list)
    gap_ids: list[str] = Field(default_factory=list)


def run_env_sync_agent(
    *,
    settings: Settings,
    memory: str = "",
    repo_dir=None,
) -> EnvSyncResult:
    """Run the env-sync configuration drift inspection pass.

    Inspects ``config.py``, ``.env``, and ``docs/configuration.md``
    for missing, stale, and drifted settings and returns a structured
    ``EnvSyncResult`` with draft tickets for newly-discovered gaps.

    When ``repo_dir`` is provided, the agent gets filesystem tools
    (``read_file``, ``list_dir``) and the ``explore`` scout tool so
    it can inspect the actual files.  Without ``repo_dir`` the agent
    runs in a read-only reasoning mode (no repo access).

    The agent is constructed via :func:`~.base.build_agent` with
    ``web=False`` (only reads local files), and
    ``model_name=settings.env_sync_model``.

    Execution is wrapped in :func:`~.retry.call_with_retry`, which
    handles transient network/model failures (exponential backoff:
    2s base, 30s cap) and ``UsageLimitExceeded`` rate-limit errors
    (30s base, 120s cap with provider fallback after
    ``settings.rate_limit_fallback_retries`` consecutive failures).

    Args:
        settings: Application configuration — model names
            (``env_sync_model``), retry parameters, forge URL, and
            tool paths.
        memory: The agent's memory ledger as a Markdown string.
            Defaults to ``""`` (the agent starts a fresh ledger).
        repo_dir: Optional path to the local repository clone.
            When not ``None``, enables the ``explore``,
            ``read_file``, and ``list_dir`` tools.

    Returns:
        An ``EnvSyncResult`` with draft titles, bodies, and gap IDs
        clipped to ``MAX_GAPS`` (5) entries, plus the updated memory
        ledger.
    """
    from pydantic_ai import PromptedOutput

    from .base import build_agent, _safe_close

    tools: list = []
    if repo_dir is not None:
        from .explore import make_explore_tool
        from .fs_tools import build_fs_tools

        ro = [
            t for t in build_fs_tools(repo_dir, settings)
            if t.__name__ in ("read_file", "list_dir")
        ]
        tools = [make_explore_tool(settings, repo_dir), *ro]

    agent = build_agent(
        settings,
        system_prompt=SYSTEM_PROMPT,
        output_type=PromptedOutput(EnvSyncResult),
        tools=tools,
        web=False,
        report_issue=False,
        model_name=settings.env_sync_model,
        name="env-sync",
    )
    forge_url = settings.forge_remote_url or "(not configured)"
    prompt = (
        f"<forge_remote_url>{forge_url}</forge_remote_url>\n\n"
        f"<memory>\n{memory or '(empty — start a new ledger)'}\n</memory>\n\n"
        "Perform the env-sync drift inspection and return your result."
    )
    from .retry import call_with_retry

    try:
        result = call_with_retry(
            lambda: agent.run_sync(prompt), settings=settings, what="env-sync"
        )
    finally:
        _safe_close(agent)
    result.output.draft_titles = result.output.draft_titles[:MAX_GAPS]
    result.output.draft_bodies = result.output.draft_bodies[:MAX_GAPS]
    result.output.gap_ids = result.output.gap_ids[:MAX_GAPS]
    return result.output
