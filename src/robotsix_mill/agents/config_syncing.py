"""The config-sync agent: config-drift detection across the JSON config layers.

The authoritative config source is the single committed template
``config/config.example.json`` (every non-secret knob with its default,
plus a ``secrets:`` block of ``SECRET`` sentinels).  The live file
``config/config.yaml`` (gitignored) is its real-valued counterpart.
This agent cross-references the template against
``src/robotsix_mill/config.py`` (the Pydantic model that consumes it)
and ``docs/configuration.md`` (when present) to catch drift: settings
declared in code but missing from the YAML template, YAML keys with no
matching field, documented defaults that disagree with the model
defaults, etc.

Seam: tests monkeypatch ``run_config_sync_agent``.  Structured output so
the runner has a clear result to work with.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..config import Settings
from .prompt_blocks import section

SYSTEM_PROMPT = """\
You are the config-sync agent for an autonomous software project.
Your sole job is to detect drift between the Pydantic config model
in ``src/robotsix_mill/config.py`` and the layered JSON config
files that feed it.

AUTHORITATIVE FILES (read these, in order):

- ``src/robotsix_mill/config.py`` — the Pydantic ``Settings`` and
  ``Secrets`` classes are the source of truth for what knobs exist.
  Extract every field: name, type, default, and YAML key path (the
  JSON→env mapping is in ``src/robotsix_mill/config/loader.py``).
- ``config/config.example.json`` — the committed single-file config
  template. Its non-secret leaves are the canonical defaults (every
  model field with a JSON mapping should appear here with a matching
  default); its ``secrets:`` block is the schema template for the
  ``Secrets`` class (every secret field should appear as a key with the
  ``SECRET`` sentinel value).
- ``config/repos.example.json`` — schema template for per-repo
  config (``RepoConfig`` fields).
- ``docs/configuration.md`` — operator-facing documentation. When
  present, every documented option's default should match the
  model's actual default.

NOT AUTHORITATIVE (do NOT flag these as drift sources):

- ``config/config.yaml`` — gitignored live config with real secrets +
  host-specific overrides; never inspect.
- ``config/repos.yaml`` — gitignored per-deployment repo registry.
- ``.env`` — legacy fallback. Settings still accept ``MILL_*`` env
  vars, but YAML is the documented surface. Don't file "missing
  from .env" drafts.

CLASSIFY FINDINGS:

- **missing-from-json**: a field exists in ``config.py`` with a
  JSON mapping but no corresponding key in ``config.example.json``
  (its non-secret section or its ``secrets:`` block) /
  ``repos.example.json`` (as appropriate for which class it lives on).
- **stale-json-key**: a JSON key exists in one of the example /
  defaults files but no matching field in the model.
- **default-mismatch**: the JSON default disagrees with the model
  default (e.g. defaults file says ``test_requests: 8`` but the
  model field defaults to ``50``).
- **doc-mismatch**: ``docs/configuration.md`` documents a knob with
  a value that doesn't match the model default. Only flag when the
  doc explicitly states a number / string; narrative mentions are
  fine.

DRAFT FORMAT:

- **Title**: ``config drift: <field> missing from
  config.example.json``, ``config drift: stale key
  ``<key>`` in config.example.json``, ``config drift: <field> default
  mismatch (model=X, json=Y)``, or ``config drift: docs default
  mismatch for <field>``.
- **Body**: field name + YAML key path + model default + observed
  YAML/doc value + concrete suggested fix (which file to edit,
  what to change).

MEMORY LEDGER:

You are given the current config-sync memory ledger — a Markdown document
that tracks gaps that have been proposed (as draft tickets), declined,
or already addressed (done). The memory is *yours* — you own its
structure and content.

0. **If the ledger is empty or missing canonical sections**, initialize
   it with the three required headings before doing anything else:
   ``## Proposals``, ``## Done``, ``## Declined``. The reconciliation
   steps below assume these sections exist; without seeding, the
   first-run ledger fails the agent-check format-consistency gate
   (which flags ledgers missing the canonical sections) and the
   gap-loop becomes self-perpetuating.
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

CONTEXT AND BUDGET DISCIPLINE:

- ⚠️ **BUDGET WARNING:** Every top-level ``read_file``, ``list_dir``,
  and ``run_command`` call burns one request against your limited
  request budget.  ``explore`` calls do NOT — the sub-agent has its
  own independent request budget.  Prefer delegation to ``explore``
  for any work beyond trivial single-step lookups.
- **Check your conversation history before calling ``read_file``.**
  A file you have already read via a prior ``read_file`` call (or
  that was preloaded via ``reference_files``) in THIS conversation
  is already in your context — quote from it directly.  Do NOT
  re-read a file you already have.
- **Partial slices of already-loaded files WILL be refused.**  If
  you call ``read_file`` with an ``offset`` on a file whose full
  content is already in your context, the tool will return a
  refusal string.  This wastes a round-trip.  Always check your
  history first.
- **Batch multi-step research into ``explore`` calls.**  When you
  need to cross-reference several files or answer multiple related
  questions, put them as sub-questions inside a SINGLE ``explore``
  request rather than issuing serial ``read_file`` calls.  Serial
  single-facet calls re-read the same large files each time,
  wasting context and cost.
"""

MAX_GAPS = 5


class ConfigSyncResult(BaseModel):
    """Structured result of a config-sync drift inspection pass.

    Carries the agent's ``updated_memory`` ledger plus three parallel
    lists describing newly-discovered config-drift gaps to file as
    draft tickets: ``draft_titles``, ``draft_bodies``, and the
    ``gap_ids`` used for dedup against the memory ledger.
    """

    updated_memory: str = ""
    draft_titles: list[str] = Field(default_factory=list)
    draft_bodies: list[str] = Field(default_factory=list)
    gap_ids: list[str] = Field(default_factory=list)


def run_config_sync_agent(
    *,
    settings: Settings,
    memory: str = "",
    recent_proposals: str = "",
    verified_proposals: str = "",
    repo_dir=None,
) -> ConfigSyncResult:
    """Run the config-sync configuration drift inspection pass.

    Inspects ``config.py``, ``.env``, and ``docs/configuration.md``
    for missing, stale, and drifted settings and returns a structured
    ``ConfigSyncResult`` with draft tickets for newly-discovered gaps.

    When ``repo_dir`` is provided, the agent gets filesystem tools
    (``read_file``, ``list_dir``) and the ``explore`` scout tool so
    it can inspect the actual files.  Without ``repo_dir`` the agent
    runs in a read-only reasoning mode (no repo access).

    The agent is constructed via :func:`~.base.build_agent` with
    ``web=False`` (only reads local files), and ``level=1``.

    Execution is wrapped in :func:`~.retry.call_with_retry`, which
    handles transient network/model failures (exponential backoff:
    2s base, 30s cap) and ``UsageLimitExceeded`` rate-limit errors
    (30s base, 120s cap with provider fallback after
    ``settings.rate_limit_fallback_retries`` consecutive failures).

    Args:
        settings: Application configuration — model names
            (``config_sync_model``), retry parameters, forge URL, and
            tool paths.
        memory: The agent's memory ledger as a Markdown string.
            Defaults to ``""`` (the agent starts a fresh ledger).
        recent_proposals: Formatted block of recent proposals from
            the board, used for deduplication.  Defaults to ``""``.
        verified_proposals: Formatted block of verified (already-posted)
            proposals, used for deduplication.  Defaults to ``""``.
        repo_dir: Optional path to the local repository clone.
            When not ``None``, enables the ``explore``,
            ``read_file``, and ``list_dir`` tools.

    Returns:
        A ``ConfigSyncResult`` with draft titles, bodies, and gap IDs
        clipped to ``MAX_GAPS`` (5) entries, plus the updated memory
        ledger.
    """
    from pydantic_ai import PromptedOutput

    from .base import build_agent, _safe_close

    from ._repo_tools import _build_repo_tools

    tools = _build_repo_tools(repo_dir, settings, tool_names=("read_file", "list_dir"))

    agent = build_agent(
        settings,
        system_prompt=SYSTEM_PROMPT,
        output_type=PromptedOutput(ConfigSyncResult),
        tools=tools,
        web_knowledge=False,
        report_issue=False,
        read_ticket=True,
        level=1,
        name="config-sync",
        repo_dir=repo_dir,  # confine SDK built-in edits to the workspace clone
    )
    forge_url = settings.forge_remote_url or "(not configured)"
    verified_block = ("\n\n" + verified_proposals) if verified_proposals else ""
    prompt = (
        f"{recent_proposals}"
        + verified_block
        + "\n\n"
        + section("forge-remote-url", forge_url)
        + "\n\n"
        + section("memory", memory or "(empty — start a new ledger)")
        + "\n\n"
        + "Perform the config-sync drift inspection and return your result."
    )
    from .retry import run_agent

    try:
        result = run_agent(agent, lambda h: h.run_sync(prompt), what="config-sync")
    finally:
        _safe_close(agent)
    result.output.draft_titles = result.output.draft_titles[:MAX_GAPS]
    result.output.draft_bodies = result.output.draft_bodies[:MAX_GAPS]
    result.output.gap_ids = result.output.gap_ids[:MAX_GAPS]
    return result.output
