"""The env-sync agent: config-drift detection across the YAML layers.

The authoritative config source is no longer .env / docs — it's
``config/mill.defaults.yaml`` (canonical defaults), layered with the
gitignored ``mill.local.yaml`` / ``mill.production.yaml`` overrides
and the separate ``secrets.yaml``. This agent cross-references those
files against ``src/robotsix_mill/config.py`` (the Pydantic model
that consumes them) and ``docs/configuration.md`` (when present) to
catch drift: settings declared in code but missing from the YAML
template, YAML keys with no matching field, documented defaults that
disagree with the model defaults, etc.

Despite the historical name, this is a "config-sync" pass — `.env`
is a legacy fallback and only secrets that bypass YAML live there.

Seam: tests monkeypatch ``run_env_sync_agent``.  Structured output so
the runner has a clear result to work with.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..config import Settings

SYSTEM_PROMPT = """\
You are the config-sync agent for an autonomous software project.
Your sole job is to detect drift between the Pydantic config model
in ``src/robotsix_mill/config.py`` and the layered YAML config
files that feed it.

AUTHORITATIVE FILES (read these, in order):

- ``src/robotsix_mill/config.py`` — the Pydantic ``Settings`` and
  ``Secrets`` classes are the source of truth for what knobs exist.
  Extract every field: name, type, default, and YAML key path (the
  YAML→env mapping is in ``src/robotsix_mill/config_loader.py``).
- ``config/mill.defaults.yaml`` — canonical YAML defaults committed
  to the repo. Every model field that has a YAML mapping should
  appear here with a default that matches the model.
- ``config/secrets.example.yaml`` — schema template for the
  ``Secrets`` class. Every secret field should appear here as a
  documented key (with a placeholder value).
- ``config/repos.example.yaml`` — schema template for per-repo
  config (``RepoConfig`` fields).
- ``docs/configuration.md`` — operator-facing documentation. When
  present, every documented option's default should match the
  model's actual default.

NOT AUTHORITATIVE (do NOT flag these as drift sources):

- ``config/mill.local.yaml`` and ``config/mill.production.yaml`` —
  gitignored host-specific overrides; their absence is normal.
- ``config/secrets.yaml`` — gitignored real secrets; never inspect.
- ``config/repos.yaml`` — gitignored per-deployment repo registry.
- ``.env`` — legacy fallback. Settings still accept ``MILL_*`` env
  vars, but YAML is the documented surface. Don't file "missing
  from .env" drafts.

CLASSIFY FINDINGS:

- **missing-from-yaml**: a field exists in ``config.py`` with a
  YAML mapping but no corresponding key in ``mill.defaults.yaml`` /
  ``secrets.example.yaml`` / ``repos.example.yaml`` (as appropriate
  for which class it lives on).
- **stale-yaml-key**: a YAML key exists in one of the example /
  defaults files but no matching field in the model.
- **default-mismatch**: the YAML default disagrees with the model
  default (e.g. defaults file says ``max_concurrency: 4`` but the
  model defaults to ``1``).
- **doc-mismatch**: ``docs/configuration.md`` documents a knob with
  a value that doesn't match the model default. Only flag when the
  doc explicitly states a number / string; narrative mentions are
  fine.

DRAFT FORMAT:

- **Title**: ``config drift: <field> missing from
  mill.defaults.yaml``, ``config drift: stale key
  ``<key>`` in mill.defaults.yaml``, ``config drift: <field> default
  mismatch (model=X, yaml=Y)``, or ``config drift: docs default
  mismatch for <field>``.
- **Body**: field name + YAML key path + model default + observed
  YAML/doc value + concrete suggested fix (which file to edit,
  what to change).

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
    recent_proposals: str = "",
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
        read_ticket=True,
        model_name=settings.env_sync_model,
        name="env-sync",
    )
    forge_url = settings.forge_remote_url or "(not configured)"
    prompt = (
        f"{recent_proposals}"
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
