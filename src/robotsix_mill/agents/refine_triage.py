"""Triage and review classification functions for the refine pipeline.

Cost-saving classifiers that decide whether a ticket draft needs a full
refine pass, whether a refined spec needs human approval, whether a
reviewer agrees with a draft, and whether a spec can be de-narrated.

Extracted from ``refining.py`` to keep that module under 1 000 lines.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, Field

from robotsix_mill._resources import agent_definitions_dir
from ..config import Settings
from .prompt_blocks import section

# Strips the ``## Tool: `explore``` section (through to the next
# ``## `` heading) from the triage system prompt when no repo clone
# is available, so the prompt-tool-consistency guard doesn't raise
# ValueError for an advertised-but-absent tool.
_STRIP_EXPLORE_SECTION_RE = re.compile(r"## Tool: `explore`.*?\n(?=## )", re.DOTALL)

# Strips the ``## Tool: `read_file``` section (through to the next
# ``## `` heading) from the triage system prompt when no repo clone
# is available, so the prompt-tool-consistency guard doesn't raise
# ValueError for the advertised-but-absent ``read_file`` tool.
_STRIP_READFILE_SECTION_RE = re.compile(r"## Tool: `read_file`.*?\n(?=## )", re.DOTALL)

log = logging.getLogger(__name__)


class TriageResult(BaseModel):
    """Triage agent output — a single cheap classification call."""

    decision: Literal["REFINE", "SKIP", "NO_CHANGE", "MIGRATE"]
    reason: str
    target_board: str | None = Field(
        default=None,
        description=(
            "Required when decision == 'MIGRATE'. MUST be a board listed "
            "in the registered-boards catalog given in the prompt. "
            "Specifies the target board to migrate this ticket to."
        ),
    )
    complexity: Literal["simple", "needs-exploration"] | None = Field(
        default=None,
        description=(
            "When decision is REFINE: 'simple' means the ticket is a "
            "single-file / auto-approve-class change that needs no "
            "multi-step codebase exploration — the refine agent can "
            "work from read_file/list_dir/run_command alone. "
            "'needs-exploration' (or None for backward compat) means "
            "full explore/parallel_explore tools should be provided. "
            "When decision is SKIP this field is ignored."
        ),
    )
    trivial_scope: bool | None = Field(
        default=None,
        description=(
            "Only meaningful when decision == 'REFINE'. True ONLY when "
            "ALL THREE of the following hold: (1) the scope is a "
            "single-file edit (one file, not a module + tests pair); "
            "(2) the change is mechanical — no new abstractions, design "
            "decisions, or external research; (3) the draft already "
            "contains the exact diff or equivalent imperative line-level "
            "instructions. When True the refine agent may be routed to "
            "a cheaper model level. Default None/False ⇒ standard "
            "(Opus) routing. Bias conservative: when unsure, False."
        ),
    )
    exploration_findings: str | None = Field(
        default=None,
        description=(
            "A COMPACT summary of facts you VERIFIED via the explore/"
            "read_file tools during triage: confirmed file paths, the "
            "symbols/functions/classes found in them (with approximate "
            "locations), and existence confirmations. Include ONLY facts "
            "you actually gathered from a tool call this turn — never "
            "guess or restate the draft. Leave null when you ran no "
            "exploration tools. Keep it to a short bulleted list; this is "
            "forwarded to the refine agent so it can skip re-exploring "
            "these files."
        ),
    )


class AutoApproveResult(BaseModel):
    """Auto-approve triage output — gates on genuinely high-risk changes only.

    Returns APPROVE for all routine work: new internal modules/classes/
    schemas/endpoints, UI changes, refactors, tests, docs, config, and
    any single-repo feature that does not cross the five high-risk gates.
    Returns NEEDS_APPROVAL only for: security/auth/secrets, destructive
    or irreversible operations, cross-repo/infra/CI/deploy changes,
    PUBLIC-API breaking changes, or a new external runtime dependency
    with material license or security weight.
    The bias is permissive: when unsure whether a high-risk condition
    applies, return APPROVE.

    The ``reason`` field must follow a concise/structured contract:
    - For NEEDS_APPROVAL: one short sentence naming the primary
      factor, then a Markdown bullet list when multiple independent
      signals apply — each bullet naming the concrete artifact (file,
      module, API, DB table, dependency, behaviour).  No narration,
      no meta-commentary.  Target ≤ 5 bullets / ≤ 80 words.
    - For APPROVE: a single short sentence naming the change and why
      it requires no design review.
    """

    decision: Literal["APPROVE", "NEEDS_APPROVAL"]
    reason: str = Field(
        description=(
            "Concise classification rationale for a human manager. "
            "When decision is NEEDS_APPROVAL: lead with one short "
            "sentence naming the primary factor requiring review, "
            "then a short Markdown bullet list (≤ 5 bullets, ≤ 80 "
            "words) when multiple independent design-decision signals "
            "apply — each bullet naming the concrete artifact (file, "
            "module, API, DB table, dependency, behaviour).  Omit "
            "narration, restated spec text, and meta-commentary. "
            "When decision is APPROVE: a single short sentence."
        )
    )


class SpecReviewResult(BaseModel):
    """Post-refinement spec review output — strips verbose narrative.

    Produces a clean, concise spec without exploratory narration.
    """

    concise_spec: str
    stripped_summary: str


class ReviewerAgreementResult(BaseModel):
    """Pre-Opus classifier output: does the reviewer agree with the draft?

    When AGREE, the pipeline short-circuits to DONE (skipping Opus).
    When DISAGREE, the full refine agent runs as normal.
    """

    decision: Literal["AGREE", "DISAGREE"]
    reason: str


def triage_refine(
    *,
    settings: Settings,
    title: str,
    draft: str,
    repo_dir: Path | None = None,
    extra_roots: list[Path] | None = None,
) -> TriageResult:
    """Return a ``TriageResult`` from a single cheap LLM call.

    When ``repo_dir`` is given the agent receives the ``explore`` tool
    (only) so it can delegate quick verification questions to a scout
    sub-agent.  The scout has its own independent request budget; the
    triage classifier's own cap (``triage_request_limit``) only needs
    to cover classification + delegation calls.

    Without ``repo_dir`` the agent runs with no tools — the original
    draft-only classification path (e.g. for meta-board tickets with
    no repo clone).
    """

    from pydantic_ai.usage import UsageLimits

    from .yaml_loader import load_agent_definition
    from .base import build_agent_from_definition, _safe_close
    from .retry import run_agent

    definition = load_agent_definition(agent_definitions_dir() / "triage.yaml")

    tools: list = []
    if repo_dir is not None:
        from .explore import make_explore_tool
        from .fs_tools import build_fs_tools

        tools = [make_explore_tool(settings, repo_dir, extra_roots=extra_roots)]
        # Wire the read-only ``read_file`` closure so the classifier can
        # deterministically verify a cited path exists before concluding
        # it's absent — instead of over-defaulting to REFINE when the
        # explore scout errors or returns a truncated/empty result.
        all_fs = build_fs_tools(
            repo_dir,
            settings,
            extra_roots=extra_roots,
            read_file_max_calls=settings.max_refine_read_file_calls,
        )
        read_file_tool = next(t for t in all_fs if t.__name__ == "read_file")
        tools.append(read_file_tool)

    system_prompt = definition.system_prompt
    if repo_dir is None:
        # Strip the ``## Tool: `explore``` and ``## Tool: `read_file```
        # sections so the build-time prompt-tool-consistency guard
        # doesn't raise ValueError when those tools are absent from the
        # resolved tool set.
        system_prompt = _STRIP_EXPLORE_SECTION_RE.sub("", system_prompt)
        system_prompt = _STRIP_READFILE_SECTION_RE.sub("", system_prompt)

    agent = build_agent_from_definition(
        settings,
        definition,
        tools=tools,
        system_prompt=system_prompt,
    )

    user_prompt = section("title", title) + "\n" + section("draft", draft)

    # List the registered boards so the classifier can name a valid
    # target_board when emitting a MIGRATE decision.
    try:
        from ..config import get_repos_config

        board_ids = sorted({rc.board_id for rc in get_repos_config().repos.values()})
        if board_ids:
            user_prompt += (
                "\n# Registered boards (valid target_board values for a MIGRATE decision)\n"
                + "\n".join(f"- {b}" for b in board_ids)
                + "\n"
            )
    except Exception:
        log.debug("could not list registered boards for triage prompt", exc_info=True)

    limits = UsageLimits(request_limit=settings.triage_request_limit)

    try:
        result = run_agent(
            agent,
            lambda h: h.run_sync(user_prompt, usage_limits=limits),
            what="triage",
        )
    finally:
        _safe_close(agent)
    return result.output


def triage_auto_approve(
    *,
    settings: Settings,
    spec: str,
) -> AutoApproveResult:
    """Return an ``AutoApproveResult`` from a single cheap LLM call.

    Inspects the refined spec and decides whether it contains a
    genuinely high-risk change requiring human review.  Returns
    APPROVE for all routine work.  Returns NEEDS_APPROVAL only for
    the five high-risk gates: security/auth, destructive/irreversible
    operations, cross-repo/infra/CI, public-API breaking changes, or
    new external runtime dependencies with material license or
    security weight.  The bias is permissive: when unsure whether a
    high-risk condition applies, return APPROVE.

    NO tools, NO web, NO explore — just a tiny prompt and a
    structured classification.
    """

    from .yaml_loader import load_and_run_agent

    user_prompt = section("spec", spec)

    result = load_and_run_agent(
        settings=settings,
        definition_name="auto-approve",
        tools=[],
        prompt=user_prompt,
        what="auto-approve triage",
    )
    return result.output


def triage_reviewer_agreement(
    *,
    settings: Settings,
    draft: str,
    reviewer_comments: str,
) -> ReviewerAgreementResult:
    """Return a ``ReviewerAgreementResult`` from a single cheap LLM call.

    Checks whether the reviewer's feedback on a sendback ticket already
    agrees with the draft's no-change-needed conclusion.  When AGREE,
    the pipeline short-circuits to DONE — skipping the expensive Opus
    refine agent (~$0.28 vs ~$0.0003 for this DeepSeek flash call).

    NO tools, NO web, NO explore — just a tiny prompt and a
    structured classification.
    """

    from .yaml_loader import load_and_run_agent

    user_prompt = (
        section("draft", draft)
        + "\n\n"
        + section("reviewer_feedback", reviewer_comments)
    )

    result = load_and_run_agent(
        settings=settings,
        definition_name="reviewer-agreement",
        tools=[],
        prompt=user_prompt,
        what="reviewer-agreement triage",
    )
    return cast(ReviewerAgreementResult, result.output)


def review_spec_for_conciseness(
    *,
    settings: Settings,
    spec_markdown: str,
) -> SpecReviewResult:
    """Return a ``SpecReviewResult`` from a single cheap LLM call.

    Strips verbose refinement-step narration (exploratory file reads,
    command runs, dead-end investigations, reasoning chains) from the
    refined spec while preserving the four standard sections
    (## Problem, ## Scope, ## Acceptance criteria, ## Out of scope).

    NO tools, NO web, NO explore — classification/transformation only.
    """

    from .yaml_loader import load_and_run_agent

    user_prompt = section("spec", spec_markdown)

    result = load_and_run_agent(
        settings=settings,
        definition_name="spec-review",
        tools=[],
        prompt=user_prompt,
        what="spec review",
    )
    return result.output
