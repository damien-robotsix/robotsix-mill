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

from pydantic import BaseModel, Field, model_validator

from ..config import Settings
from .prompt_blocks import section

# Re-export SYSTEM_PROMPT for tests (loaded from YAML without env-var resolution)
import yaml as _yaml

_SYSPROMPT_PATH = (
    Path(__file__).parent.parent.parent.parent / "agent_definitions" / "refine.yaml"
)
SYSTEM_PROMPT: str = _yaml.safe_load(_SYSPROMPT_PATH.read_text())["system_prompt"]


class TriageResult(BaseModel):
    """Triage agent output — a single cheap classification call."""

    decision: Literal["REFINE", "SKIP"]
    reason: str


class AutoApproveResult(BaseModel):
    """Auto-approve triage output — gates on genuine design decisions.

    Returns APPROVE when the spec is precise, unambiguous, and free of
    design/architecture decisions that a human would want to review.
    Returns NEEDS_APPROVAL when the spec contains a genuine design
    decision, is ambiguous, or is security-sensitive.  The bias is
    conservative: when unsure whether a real decision exists, return
    NEEDS_APPROVAL.
    """

    decision: Literal["APPROVE", "NEEDS_APPROVAL"]
    reason: str


class SpecReviewResult(BaseModel):
    """Post-refinement spec review output — strips verbose narrative.

    Produces a clean, concise spec without exploratory narration.
    """

    concise_spec: str
    stripped_summary: str


class ChildSpec(BaseModel):
    """A split child."""

    title: str
    spec_markdown: str
    depends_on: list[int] = []


class FileMapEntry(BaseModel):
    """A file relevant to the ticket, with a one-line note on its role."""

    file: str
    note: str


class RefineResult(BaseModel):
    """Refine agent output."""

    split: bool = False
    spec_markdown: str | None = None
    children: list[ChildSpec] | None = None
    updated_memory: str = ""
    title: str | None = None
    epic_body: str | None = None
    # When True, the refine stage converts THIS ticket to kind="epic"
    # in state EPIC_OPEN and triggers the epic-breakdown agent. The
    # refined spec ships as the epic body via ``epic_body``. Mutually
    # exclusive with ``split=true``; refine MUST NOT populate
    # ``children`` when promoting — epic-breakdown owns child
    # generation. Use for ≥ 6-child enumerations, manifest-driven
    # specs where each item is a substantial change, or anything
    # where children need their own deep refine cycles.
    promote_to_epic: bool = False
    # When True, refine concluded the ticket requires no code change —
    # the spec body already contains the full investigation, the
    # acceptance criteria are information-only (e.g. "post a comment
    # explaining why no change is needed"), or a parallel ticket
    # already shipped the fix. The stage posts ``no_change_rationale``
    # as a top-level comment on the ticket and transitions
    # DRAFT → DONE directly — skipping implement/review/document/
    # deliver/merge. Mutually exclusive with ``split=true`` and
    # ``promote_to_epic=true``.
    no_change_needed: bool = False
    # Rationale posted as the closing comment when
    # ``no_change_needed=true``. Markdown is fine.
    no_change_rationale: str | None = None
    file_map: list[FileMapEntry] | None = None
    reference_files: list[str] = Field(
        default_factory=list,
        description=(
            "Relative paths from the repo root that the implement agent "
            "should start with read_file outputs already loaded for. "
            "These are files the refine agent read deeply and expects "
            "to remain load-bearing for implementation. Include ONLY "
            "files that carry architectural context or show patterns "
            "the implementer must follow. Exclude: files only skimmed, "
            "files whose role was just confirming a hypothesis with no "
            "further bearing, generated artifacts, changelogs, lockfiles."
        ),
    )
    conversation_state: bytes | None = Field(
        default=None,
        exclude=True,
        description=(
            "Raw JSON bytes from all_messages_json() — the FULL "
            "transcript, persisted by the stage runner to "
            "conversation_state.json so a subsequent resume can pass "
            "it back as message_history."
        ),
    )
    new_messages: bytes | None = Field(
        default=None,
        exclude=True,
        description=(
            "Raw JSON bytes from new_messages_json() — only messages "
            "added during THIS run. Used by ``check_for_pause`` so the "
            "ask_user sentinel from a PRIOR turn (still present in the "
            "saved conversation_state after resume) doesn't re-trigger "
            "the pause guard."
        ),
    )

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
                kl.startswith("spec_")
                and any(fragment in kl for fragment in ("mark", "md", "down"))
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

    from .yaml_loader import load_agent_definition
    from .base import build_agent_from_definition, _safe_close
    from .retry import call_with_retry

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent / "agent_definitions" / "triage.yaml"
    )

    agent = build_agent_from_definition(
        settings,
        definition,
        tools=[],
        model_name=definition.model or settings.triage_model,
    )

    user_prompt = section("title", title) + "\n" + section("draft", draft)

    try:
        result = call_with_retry(
            lambda: agent.run_sync(user_prompt),
            settings=settings,
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

    Inspects the refined spec and decides whether a genuine design
    decision exists that a human should review.  Returns APPROVE when
    the spec is precise, unambiguous, and free of design/architecture
    decisions.  Returns NEEDS_APPROVAL when a real decision is
    present.  The bias is conservative: when unsure whether a genuine
    decision exists, returns NEEDS_APPROVAL.

    NO tools, NO web, NO explore — just a tiny prompt and a
    structured classification.
    """

    from .yaml_loader import load_agent_definition
    from .base import build_agent_from_definition, _safe_close
    from .retry import call_with_retry

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent
        / "agent_definitions"
        / "auto-approve.yaml"
    )

    agent = build_agent_from_definition(
        settings,
        definition,
        tools=[],
        model_name=definition.model or settings.auto_approve_model,
    )

    user_prompt = section("spec", spec)

    try:
        result = call_with_retry(
            lambda: agent.run_sync(user_prompt),
            settings=settings,
            what="auto-approve triage",
        )
    finally:
        _safe_close(agent)
    return result.output


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

    from .yaml_loader import load_agent_definition
    from .base import build_agent_from_definition, _safe_close
    from .retry import call_with_retry

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent
        / "agent_definitions"
        / "spec-review.yaml"
    )

    agent = build_agent_from_definition(
        settings,
        definition,
        tools=[],
        model_name=definition.model or settings.triage_model,
    )

    user_prompt = section("spec", spec_markdown)

    try:
        result = call_with_retry(
            lambda: agent.run_sync(user_prompt),
            settings=settings,
            what="spec review",
        )
    finally:
        _safe_close(agent)
    return result.output


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

## Reviewer threads

Each comment in ``<reviewer_feedback>`` includes a thread id
(e.g. ``[id=42 @ …]``).  You have two tools:

- ``reply_to_thread(thread_id, body)`` — reply to a comment thread.
- ``close_thread(comment_id)`` — close a top-level thread (marks it
  resolved; only call when you have fully addressed the issue in the
  revised spec).

For each reviewer comment:
- If your spec revision fully addresses it: call
  ``close_thread(comment_id)``.
- If you addressed it but want to explain your approach: call
  ``reply_to_thread(thread_id, body)`` then
  ``close_thread(comment_id)``.
- If the comment asks for clarification you cannot fully resolve
  without more context: call ``reply_to_thread(thread_id, body)``
  explaining what you need — do NOT close.

Do NOT use ``report_issue`` for thread replies — that is for
blocking issues only.

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
- Always produce a ``file_map``: a short list of the source files
  most relevant to this ticket, each with a one-line note on its
  role.  Format:
  ``file_map=[{"file": "path/to/file.py", "note": "reason this file matters"}, ...]``.
  Keep it to ≤ 20 files.  Only include files you actually explored
  or read — do not guess.  If no files are relevant (e.g. a pure
  configuration change with no codebase exploration needed), return
  ``file_map=[]``.
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
    extra_roots: list[Path] | None = None,
    message_history: list | None = None,
    board_id: str = "",
) -> RefineResult:
    """Return a structured ``RefineResult``. When ``repo_dir`` is given
    the agent grounds the spec in that local clone via explore/
    read_file/list_dir/run_command; otherwise it works draft-only.
    When ``reviewer_comments`` is given the agent incorporates the
    feedback into the refined spec.

    ``message_history`` — when non-``None``, passed directly to
    ``agent.run_sync(…)`` so the agent continues from a prior paused
    conversation (the resume path after ``ask_user``).

    Raises ``RuntimeError`` if no OpenRouter key is configured.

    The returned ``RefineResult.conversation_state`` is the raw JSON
    bytes from ``all_messages_json()`` — ``None`` when the agent call
    didn't produce a message history. The stage runner uses it to
    detect ``ask_user`` pauses and persist the conversation for resume.

    Return fields:
      - ``split``: whether the draft was split into children
      - ``spec_markdown``: single-scope spec (when split=False)
      - ``children``: list of ``ChildSpec`` (when split=True)
      - ``updated_memory``: updated memory ledger
      - ``epic_body``: revised epic description when ``<epic_context>``
        was provided, otherwise ``None``
      - ``conversation_state``: raw conversation JSON for pause/resume
    """

    from .yaml_loader import load_agent_definition
    from .base import build_agent_from_definition, _safe_close
    from .retry import call_with_retry

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent / "agent_definitions" / "refine.yaml"
    )

    tools: list = []
    if repo_dir is not None:
        from .explore import make_explore_tool
        from .fs_tools import build_fs_tools

        ro = [
            t
            for t in build_fs_tools(repo_dir, settings, extra_roots=extra_roots)
            if t.__name__ in ("read_file", "list_dir", "run_command")
        ]
        tools = [make_explore_tool(settings, repo_dir, extra_roots=extra_roots), *ro]

    overrides = {}
    if reviewer_comments:
        overrides["system_prompt"] = REVIEWER_SENDBACK_PROMPT
    if not definition.model:
        overrides["model_name"] = settings.refine_model

    agent = build_agent_from_definition(
        settings,
        definition,
        tools=tools,
        board_id=board_id,
        **overrides,
    )

    # Build user prompt: title, draft, memory, and optionally reviewer feedback.
    user_prompt = ""
    if epic_context:
        user_prompt += f"{epic_context}\n\n"
    user_prompt += (
        section("title", title)
        + "\n"
        + section("draft", draft)
        + "\n\n"
        + section("memory", memory or "(empty — start a new ledger)")
    )
    if reviewer_comments:
        user_prompt += "\n\n" + section(
            "reviewer-feedback",
            "The reviewer sent this spec back with the following "
            "comments. Address each one in the revised spec:\n\n"
            f"{reviewer_comments}",
        )

    try:
        result = call_with_retry(
            lambda: agent.run_sync(
                user_prompt,
                message_history=message_history,
            ),
            settings=settings,
            what="refine",
        )

        # Guard: if the agent hit the iteration limit while the model
        # was still requesting tool calls, the last response has
        # finish_reason == "tool_call" and result.output is empty.
        # Synthesise a final answer with a single continuation call
        # that includes the full message history for context.
        finish_reason = getattr(
            getattr(result, "response", None), "finish_reason", None
        )
        if finish_reason == "tool_call":
            continuation_result = call_with_retry(
                lambda: agent.run_sync(
                    "Please synthesise a final answer based on the "
                    "tool results above.",
                    message_history=result.all_messages(),
                ),
                settings=settings,
                what="refine (continuation after tool_calls stop)",
            )
            result = continuation_result

        output: RefineResult = result.output
        try:
            output.conversation_state = result.all_messages_json()
        except AttributeError:
            output.conversation_state = None
        try:
            output.new_messages = result.new_messages_json()
        except AttributeError:
            output.new_messages = None
    finally:
        _safe_close(agent)
    return output
