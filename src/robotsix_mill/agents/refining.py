"""The refine agent: a capable model that authors the spec, grounded
in the ACTUAL repo when a local clone is available.

When the refine stage has cloned the target repo it passes
``repo_dir``; the agent then gets the cheap ``explore`` scout +
read-only ``read_file``/``list_dir``/``run_command`` to ground the
spec in real code (instead of web-fetching the project's own files —
slow & indirect). ``run_command`` runs sandboxed, read-only commands
(e.g. re-run failing tests when error output is truncated).
``web_research`` stays for genuinely external lookups only. With no
repo (no forge configured) it falls back to draft-only as before.
``run_refine_agent`` is the seam tests monkeypatch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, model_validator

from ..config import Settings


class TriageResult(BaseModel):
    """Triage agent output — a single cheap classification call."""

    decision: Literal["REFINE", "SKIP"]
    reason: str


class ChildSpec(BaseModel):
    """A split child."""

    title: str
    spec_markdown: str
    depends_on: list[int] = []


class RefineResult(BaseModel):
    """Refine agent output."""

    split: bool = False
    spec_markdown: str | None = None
    children: list[ChildSpec] | None = None
    updated_memory: str = ""

    @model_validator(mode="before")
    @classmethod
    def _absorb_spec_markdown_typos(cls, data):
        """deepseek-v4-pro consistently mis-types ``spec_markdown`` as
        ``spec_markmark`` (observed three times in production today on
        tickets 5061, efd4, f93f). pydantic-ai silently drops the
        unknown key, ``spec_markdown`` stays None, refine stage blocks
        with "refiner produced an empty spec." Each occurrence cost an
        operator-time intervention to investigate + resume.

        Absorb the typo class here: any unknown key whose name starts
        with ``spec_`` and ends with markdown-ish letters gets folded
        into ``spec_markdown`` when that field is missing/empty. Only
        kicks in for typos — a correctly-keyed call passes straight
        through. Same logic for ``spec`` (no underscore).
        """
        if not isinstance(data, dict):
            return data
        if data.get("spec_markdown"):
            return data
        # Look for typo-class keys: spec, spec_*, especially anything
        # like spec_markmark, spec_markdwn, spec_md.
        for k in list(data.keys()):
            if k == "spec_markdown":
                continue
            kl = k.lower()
            if kl == "spec" or (
                kl.startswith("spec_") and any(
                    fragment in kl for fragment in ("mark", "md", "down")
                )
            ):
                v = data.pop(k)
                if isinstance(v, str) and v.strip():
                    data["spec_markdown"] = v
                    break
        return data


def triage_refine(
    *,
    settings: Settings,
    title: str,
    draft: str,
) -> TriageResult:
    """Return a ``TriageResult`` from a single cheap LLM call.

    NO tools, NO web, NO explore — just a tiny prompt and a
    structured classification.  Conservative bias: when uncertain,
    choose REFINE (the only real risk is a wrong SKIP).
    """
    from pydantic_ai import PromptedOutput

    from .base import build_agent, _safe_close
    from .retry import call_with_retry

    TRIAGE_PROMPT = """\
You are a cost-saving triage classifier.  Your ONLY job: decide
whether a ticket draft needs a full refine pass.

A draft that is ALREADY precise, single-scoped, implementation-ready,
and grounded should be SKIPped.  Examples:
- Documentation-only changes (README, docstrings, MkDocs config).
- Config-only changes (new env var, feature flag, field rename).
- Tiny, obvious code changes where the draft lists exact file paths,
  line numbers, and acceptance criteria.
- Pure mechanical changes (rename, move files, add a one-line call).

A draft that needs REFINEment has ANY of:
- Ambiguous scope or multiple independent changes bundled together.
- Missing acceptance criteria or no concrete file paths.
- The agent would need to explore the codebase to ground the spec.
- Any non-trivial code change where the draft is not already a spec.

Be CONSERVATIVE: if you are unsure, say REFINE.
A wrong SKIP (under-refining a real change) is the only real risk.
A wrong REFINE is just status-quo cost — harmless.
"""
    agent = build_agent(
        settings,
        system_prompt=TRIAGE_PROMPT,
        output_type=PromptedOutput(TriageResult),
        tools=[],  # NO tools — classification only
        web=False,  # NO web research
        report_issue=False,
        model_name=settings.triage_model,
        name="triage",
    )

    user_prompt = f"<title>{title}</title>\n<draft>\n{draft}\n</draft>"

    try:
        result = call_with_retry(
            lambda: agent.run_sync(user_prompt),
            settings=settings,
            what="triage",
        )
    finally:
        _safe_close(agent)
    return result.output


SYSTEM_PROMPT = """\
You turn a rough ticket draft into a precise, self-contained
engineering spec an autonomous coder can implement without asking
questions.

- Ground the spec in the ACTUAL codebase — real file paths, existing
  patterns/conventions, and constraints. Do NOT web-fetch the
  project's own files.
- When a traceback is truncated or you need to inspect runtime
  behaviour (e.g. `pytest tests/test_foo.py -x --tb=long`,
  `python -c "import module; …"`, linters), re-run with
  `run_command`. The sandbox is read-only; you cannot mutate the repo.
- Never guess line numbers or byte offsets. When you need a specific
  location in a file, ask `explore` to find it by symbol name first
  (e.g. "what line is function X defined at in path/to/file.py?").
  Only then use `read_file` with the confirmed offset. Guessing
  wastes calls and adds latency with zero upside.
- When the ticket has ``artifacts/evidence.txt``, incorporate its
  contents (e.g. the exact failing command, stdout/stderr, traceback)
  into the refined spec so the implement agent has the raw evidence
  to cross-check.
- Use `web_research` ONLY for things not in the repo (a
  library/API/standard/best practice). Skip it when unneeded.
- The <draft> section may be empty (the user may have only provided a
  title). In that case, derive the spec from the title's intent alone.
- Stay faithful to the draft's intent; invent nothing unrelated. Be
  concrete and testable.

## Memory

You are given a `<memory>` block containing a Markdown ledger of
observations from your past refine runs. It records:
- Recurring reviewer feedback patterns (e.g. "acceptance criteria
  must be testable")
- Split-vs-bundle heuristics for this codebase
- Repo-specific conventions discovered during past refinements

Reference the memory when deciding how to structure the spec. After
refining, update the memory in your `updated_memory` field:
- Record any new reviewer feedback themes you observed
- Note split/bundle decisions and their rationale
- Record repo-specific conventions you discovered
- Keep entries concise and ticket-ID-qualified
- If nothing new was learned, return the incoming memory unchanged

## Output format

You MUST return a structured result. When the draft describes ONE
focused change:

- split=false, spec_markdown="## Problem\\n...\\n## Scope\\n...\\n## Acceptance criteria\\n...\\n## Out of scope / constraints\\n..."

When the draft bundles MULTIPLE independent, self-contained changes
that can each ship alone, split into focused children:

- split=true, children=[{"title": "Short title for change A", "spec_markdown": "## Problem\\n...", "depends_on": []}, ...]

Rules for splitting:
- **Split by spec surface, not by user framing.** A draft that sounds
  like "one feature" may still touch many independent layers.
  Default to splitting when the spec body exhibits any of these
  signals:
  * Lists **≥4 distinct source files** to modify, OR
  * Introduces **≥3 new endpoints / routes**, OR
  * Crosses the **backend↔frontend boundary** (e.g. Python files +
    JS/CSS/HTML files in the same spec).
  When splitting, build a ``depends_on`` graph across the focused
  children.
- **Escape clause:** do NOT split when the layers truly cannot ship
  independently — e.g. a single new column is read by every consumer
  and there is nothing useful to land in stages. In that case, note
  the inseparability reason in the single spec.
- **Borderline drafts stay as one spec.** A draft with one new
  endpoint touching two files in the same layer (e.g. model + route
  in the same backend) is NOT split. Over-splitting is as bad as
  over-bundling.
- Each child's ``spec_markdown`` must be a complete, self-contained
  spec with ## Problem, ## Scope, ## Acceptance criteria, and
  ## Out of scope / constraints sections.
- ``depends_on`` is a list of zero-based indices of earlier children
  in the same split that must be completed first.  Use only when
  child B genuinely builds on child A (e.g. a helper added then used).
  Sequential work that could be parallelised should have empty
  ``depends_on``.
- The union of all children's scope must faithfully cover the entire
  original draft — nothing dropped, nothing added.
"""

REVIEWER_SENDBACK_PROMPT = """\
You are revising an existing spec per reviewer feedback. The spec
already exists — your only task is to address each comment in the
``<reviewer_feedback>`` block.

- Address each reviewer comment directly.  If a comment asks for a
  specific change, apply it precisely; if it asks for clarification,
  elaborate the relevant section.
- Keep everything the reviewer didn't flag unchanged — do not
  introduce new scope or restructure untouched sections.
- The revised spec must remain concrete and testable so the implement
  agent can act on it without asking questions.

## Memory

You are given a `<memory>` block containing a Markdown ledger of
observations from your past refine runs. It records:
- Recurring reviewer feedback patterns (e.g. "acceptance criteria
  must be testable")
- Split-vs-bundle heuristics for this codebase
- Repo-specific conventions discovered during past refinements

Reference the memory when deciding how to structure the spec. After
refining, update the memory in your `updated_memory` field:
- Record any new reviewer feedback themes you observed
- Note split/bundle decisions and their rationale
- Record repo-specific conventions you discovered
- Keep entries concise and ticket-ID-qualified
- If nothing new was learned, return the incoming memory unchanged

## Output format

You MUST return a structured result. When the draft describes ONE
focused change:

- split=false, spec_markdown="## Problem\\n...\\n## Scope\\n...\\n## Acceptance criteria\\n...\\n## Out of scope / constraints\\n..."

When the draft bundles MULTIPLE independent, self-contained changes
that can each ship alone, split into focused children:

- split=true, children=[{"title": "Short title for change A", "spec_markdown": "## Problem\\n...", "depends_on": []}, ...]

- Each child's ``spec_markdown`` must be a complete, self-contained
  spec with ## Problem, ## Scope, ## Acceptance criteria, and
  ## Out of scope / constraints sections.
- ``depends_on`` is a list of zero-based indices of earlier children
  in the same split that must be completed first.
- The union of all children's scope must faithfully cover the entire
  original draft — nothing dropped, nothing added.
"""


def run_refine_agent(
    *,
    settings: Settings,
    title: str,
    draft: str,
    repo_dir: Path | None = None,
    reviewer_comments: str | None = None,
    memory: str = "",
    epic_context: str = "",
) -> RefineResult:
    """Return a structured ``RefineResult``. When ``repo_dir`` is given
    the agent grounds the spec in that local clone via explore/
    read_file/list_dir/run_command; otherwise it works draft-only.
    When ``reviewer_comments`` is given the agent incorporates the
    feedback into the refined spec. Raises ``RuntimeError`` if no
    OpenRouter key is configured.

    Return fields:
      - ``split``: whether the draft was split into children
      - ``spec_markdown``: single-scope spec (when split=False)
      - ``children``: list of ``ChildSpec`` (when split=True)
      - ``updated_memory``: updated memory ledger
    """
    from pydantic_ai import PromptedOutput

    from .base import build_agent, _safe_close
    from .retry import call_with_retry

    tools: list = []
    if repo_dir is not None:
        from .explore import make_explore_tool
        from .fs_tools import build_fs_tools

        ro = [
            t for t in build_fs_tools(repo_dir, settings)
            if t.__name__ in ("read_file", "list_dir", "run_command")
        ]
        tools = [make_explore_tool(settings, repo_dir), *ro]

    agent = build_agent(
        settings,
        system_prompt=REVIEWER_SENDBACK_PROMPT if reviewer_comments else SYSTEM_PROMPT,
        output_type=PromptedOutput(RefineResult),
        tools=tools,
        web=True,  # cheap web_research sub-agent (external lookups only)
        model_name=settings.refine_model,
        name="refine",
    )

    # Build user prompt: title, draft, memory, and optionally reviewer feedback.
    user_prompt = ""
    if epic_context:
        user_prompt += f"{epic_context}\n\n"
    user_prompt += (
        f"<title>{title}</title>\n<draft>\n{draft}\n</draft>\n\n"
        f"<memory>\n{memory or '(empty — start a new ledger)'}\n</memory>"
    )
    if reviewer_comments:
        user_prompt += (
            "\n<reviewer_feedback>The reviewer sent this spec back "
            "with the following comments. Address each one in the "
            "revised spec:\n\n"
            f"{reviewer_comments}\n</reviewer_feedback>"
        )

    try:
        result = call_with_retry(
            lambda: agent.run_sync(user_prompt),
            settings=settings, what="refine",
        )
    finally:
        _safe_close(agent)
    return result.output
