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

import logging
import re
from pathlib import Path
from typing import Any, Literal, cast

import yaml as _yaml

from pydantic import BaseModel, Field, model_validator

from ..data_paths import data_dir

from ..config import RepoConfig, Settings
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


def _strip_explore_call_directives(
    prompt: str,
    *,
    include_explore: bool,
    include_parallel_explore: bool,
) -> str:
    """Remove markdown bullet items that issue a ``\\`tool(...)\\`` call
    directive for an explore sub-agent tool that is gated OFF.

    When triage rules a ticket ``simple`` the ``explore`` /
    ``parallel_explore`` sub-agent tools are dropped from refine's
    resolved tool set (cost gating, PR #1637), but the static refine
    prompt still tells the agent to *call* them — which trips the
    build-time prompt/tool-consistency guard in
    :func:`build_agent_from_definition`
    (``Prompt contains call directives to unavailable tools``). Stripping
    the offending bullets keeps the guard satisfied and stops the agent
    being told to call a tool it no longer has. A bare backtick mention
    (no trailing ``(``) is left intact — only call directives matter.
    """
    disabled: set[str] = set()
    if not include_explore:
        disabled.add("explore")
    if not include_parallel_explore:
        disabled.add("parallel_explore")
    if not disabled:
        return prompt
    directive = re.compile(
        r"`+(?:" + "|".join(re.escape(t) for t in sorted(disabled)) + r")\s*\("
    )
    lines = prompt.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()
        if stripped.startswith(("* ", "- ")):
            indent = len(line) - len(stripped)
            block = [line]
            j = i + 1
            # Gather continuation lines: deeper-indented, non-blank, and
            # not themselves a sibling/parent bullet.
            while j < len(lines):
                nxt = lines[j]
                nstr = nxt.lstrip()
                nindent = len(nxt) - len(nstr)
                if nstr == "" or nindent <= indent or nstr.startswith(("* ", "- ")):
                    break
                block.append(nxt)
                j += 1
            if directive.search("\n".join(block)):
                i = j
                continue
            out.extend(block)
            i = j
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


# Triggers for "pytest warnings / filterwarnings hardening" tickets — the spec
# needs the suite's CURRENT warnings enumerated to write the documented-ignore
# list. Without deterministic injection, the refine agent re-runs the whole
# suite many times to discover them and blows the stage timeout.
_WARNINGS_TICKET_RE = re.compile(
    r"filterwarnings"
    r"|-W\s*error"
    r"|[A-Za-z]*Warning\b"
    r"|warnings?\b[^.\n]{0,40}\b(strict|error|hardening|ignore|enumerate)\b"
    r"|\b(strict|error|hardening)\b[^.\n]{0,40}\bwarnings?\b",
    re.IGNORECASE,
)


def _collect_test_warnings_block(
    draft: str, repo_dir: Path | None, settings: Settings
) -> str:
    """Deterministic ``<test-warnings>`` prompt block for warnings-hardening
    refines.

    When *draft* is about pytest warnings/filterwarnings, run the suite's
    warning collection ONCE in the sandbox and return the summary as a prompt
    section, so refine writes the documented-ignore list from this ground
    truth instead of re-running the whole suite many times (which times out
    the stage — see ticket …filterwarnings…-f946). Best-effort: returns ``""``
    when the ticket isn't warnings-related, there's no clone, or the run
    fails — the agent then falls back to its own tools.
    """
    if repo_dir is None or not _WARNINGS_TICKET_RE.search(draft or ""):
        return ""
    from ..sandbox import run as sandbox_run

    # -W default surfaces warnings; -rw lists them in the run's "warnings
    # summary" section with file:line + category — exactly what the ignore
    # list needs. Extract just that section to keep the prompt bounded.
    cmd = (
        "python -m pytest -p no:cacheprovider -q -W default -rw 2>&1 "
        "| sed -n '/warnings summary/,/^====*/p'"
    )
    try:
        _code, out = sandbox_run(
            cmd, repo_dir=Path(repo_dir), settings=settings, install_project=True
        )
    except Exception:  # noqa: BLE001 — best-effort; never block refine (incl. SandboxError)
        log.warning(
            "refine: deterministic test-warnings collection failed", exc_info=True
        )
        return ""
    out = (out or "").strip()
    if not out:
        return ""
    max_chars = 12000
    if len(out) > max_chars:
        out = out[:max_chars] + "\n… (truncated — more warnings exist)"
    return "\n\n" + section(
        "test-warnings",
        "Deterministic ONE-TIME `pytest -W default -rw` warnings summary for "
        "this repo, collected for you. Write the `filterwarnings` ignore list "
        "from THIS — do NOT run the test suite yourself to rediscover "
        "warnings (that is what timed this ticket out before):\n\n" + out,
    )


# Re-export SYSTEM_PROMPT for tests (loaded from YAML without env-var resolution)

_SYSPROMPT_PATH = data_dir("agent_definitions") / "refine.yaml"
SYSTEM_PROMPT: str = _yaml.safe_load(_SYSPROMPT_PATH.read_text())["system_prompt"]


class TriageResult(BaseModel):
    """Triage agent output — a single cheap classification call."""

    decision: Literal["REFINE", "SKIP", "MAINTENANCE", "NO_CHANGE", "MIGRATE"]
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
            "When decision is SKIP or MAINTENANCE this field is ignored."
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


class ChildSpec(BaseModel):
    """A split child."""

    title: str
    spec_markdown: str
    depends_on: list[int] = []


class FileMapEntry(BaseModel):
    """A file relevant to the ticket, with a one-line note on its role."""

    file: str
    note: str


class ReviewerAgreementResult(BaseModel):
    """Pre-Opus classifier output: does the reviewer agree with the draft?

    When AGREE, the pipeline short-circuits to DONE (skipping Opus).
    When DISAGREE, the full refine agent runs as normal.
    """

    decision: Literal["AGREE", "DISAGREE"]
    reason: str


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

    definition = load_agent_definition(data_dir("agent_definitions") / "triage.yaml")

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


def _classify_maintenance_draft(title: str, draft: str) -> str | None:
    """Return an action-type string if *title* / *draft* signals a
    maintenance request, or ``None`` otherwise.

    Deterministic keyword heuristic — no LLM call.  Case-insensitive.
    Called in phase 0 of the unified triage (before workspace clone).
    """
    title_lower = title.lower()
    draft_lower = draft.lower()

    if "create repo" in title_lower or "create repo" in draft_lower:
        return "create_repo"
    if "fork repo" in title_lower or "fork repo" in draft_lower:
        return "fork_repo"
    return None


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
- **Do NOT re-explore the codebase.** The spec was already grounded in
  the actual files during the original refine run. Only re-read a file
  if the reviewer's comment makes the existing file_map entry
  insufficient. Every unnecessary read_file or explore call on a
  re-refine pass wastes quota and inflates cost without improving
  spec quality.

## Reviewer threads

Each comment in ``<reviewer_feedback>`` includes a thread id
(e.g. ``[id=42 @ …]``).  You have two tools:

- ``reply_to_thread(thread_id, body)`` — reply to a comment thread.
- ``close_thread(comment_id)`` — close a top-level thread (marks it
  resolved; only call when you have fully addressed the issue in the
  revised spec). Do NOT call ``close_thread`` again for the same
  ``comment_id`` once it has returned success (``Thread closed ...``).
  If it returns ``Thread already closed ... is already resolved``,
  treat that as success — the thread is already resolved, do not retry.

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
general repo-level observations from your past refine runs — for
cross-cutting knowledge that helps you do your job better over time.
It records:
- Recurring reviewer feedback patterns (e.g. "acceptance criteria
  must be testable")
- Split-vs-bundle heuristics for this codebase
- Repo-specific conventions discovered during past refinements

It is NOT a per-ticket diary. DO NOT record in memory:
- Ticket IDs or per-ticket section headings (e.g. "## Refine run
  for <id>")
- Per-ticket decisions (what you chose for one specific draft)
- Anything that is a log of "what I refined this run"

Ticket history lives in the DB; memory is for cross-cutting
knowledge only.

General-vs-per-ticket discriminator example:
- Good (general rule): "Shared libs live in standalone external
  repos consumed via git+https in pyproject.toml; layout
  src/<pkg>/, Hatchling."
- Bad (per-ticket decision): "On ticket X I chose single-spec
  because the lib was a prerequisite."

Reference the memory when deciding how to structure the spec. After
refining, update the memory in your `updated_memory` field:
- Record any new reviewer feedback themes you observed
- Record repo-specific conventions you discovered
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

# Markers that signal an internal CI/type/lint/test failure — NOT an
# external knowledge gap.  The ``ask_web_knowledge`` tool is blocked
# during refine when the draft carries any of these, because
# web-searching internal toolchain errors wastes model turns on
# irrelevant results.  The markers are a module constant so the unit
# tests can inspect them (no hidden strings).  The predicate is
# conservative: it requires a recognisable tool/failure token, not
# just the bare word "test".
_INTERNAL_FAILURE_MARKERS: tuple[str, ...] = (
    # Python traceback
    "Traceback (most recent call last)",
    # pytest failure summary lines
    "= FAILURES =",
    "short test summary",
    # mypy error lines: ``error: … [code]`` (e.g. ``[arg-type]``)
    "[arg-type]",
    "[attr-defined]",
    "[call-overload]",
    "[misc]",
    "[name-defined]",
    "[no-untyped-def]",
    "[operator]",
    "[override]",
    "[return-value]",
    "[union-attr]",
    "[valid-type]",
    "[var-annotated]",
    # ruff / lint codes — specific enough to indicate actual lint OUTPUT,
    # not a prose mention. Bare tool NAMES are deliberately NOT markers:
    # a normal feature spec routinely says "ship pytest tests", "fix the
    # mypy hook", "add a ruff config" — matching the tool name there
    # mis-fired the refine short-circuit and replaced the real spec with a
    # generic "fix the failing check" template (observed across many
    # tickets). Markers must be FAILURE-OUTPUT signatures, never tool names.
    "E501",
    "F401",
    "F811",
    "F841",
    # Generic tool exit codes (require a named tool, not just "exit")
    "exit code",
    "exit=1",
)

# Case-SENSITIVE markers. pytest emits ``FAILED `` in uppercase; matching it
# case-insensitively (against ``draft.lower()``) flagged ordinary prose —
# "on failed connection", "the success and failure paths", "retry on failed
# requests" — as a toolchain failure, re-triggering the short-circuit on
# feature specs. Require the literal uppercase token instead.
_INTERNAL_FAILURE_MARKERS_CASE_SENSITIVE: tuple[str, ...] = ("FAILED ",)


def is_internal_toolchain_failure(draft: str) -> bool:
    """Return ``True`` when *draft* text carries concrete internal
    CI/type/lint/test failure signals.

    Designed as a refine-stage gate: when ``True`` the draft describes
    a local code fix scoped from the repo and logs, NOT an external
    knowledge gap.  The caller uses this to disable
    ``ask_web_knowledge`` (web-search) and/or to short-circuit to a
    minimal spec.

    The predicate is conservative — it requires at least one
    FAILURE-OUTPUT signature (traceback, pytest ``FAILED``/summary, mypy
    error codes, lint codes, exit codes; see ``_INTERNAL_FAILURE_MARKERS``).
    It deliberately does NOT match bare tool names (pytest/mypy/ruff/…):
    a normal feature spec routinely mentions those (e.g. "ship pytest
    tests"), and matching them mis-fired the refine short-circuit, replacing
    the real spec with a generic "fix the failing check" template.
    """
    lowered = draft.lower()
    if any(marker.lower() in lowered for marker in _INTERNAL_FAILURE_MARKERS):
        return True
    return any(m in draft for m in _INTERNAL_FAILURE_MARKERS_CASE_SENSITIVE)


def _coerce_refine_output(output: object) -> "RefineResult":
    """Return *output* as a ``RefineResult``.

    When the model's final message doesn't parse as ``RefineResult`` JSON,
    llmio's structured-output path returns the raw string (more likely on the
    claude_sdk backend, which parses output itself). Wrap that text into a
    result so the caller's ``output.conversation_state = …`` setattr — and its
    ``except AttributeError`` branch — don't blow up with "'str' object has no
    attribute 'conversation_state'" (the bug that blocked tickets like 0da9)."""
    if isinstance(output, RefineResult):
        return output
    log.warning(
        "refine: output did not parse as RefineResult (got %s); "
        "coercing raw text into spec_markdown",
        type(output).__name__,
    )
    return RefineResult(spec_markdown=str(output).strip() or None)


def _build_refine_overrides(
    definition,
    settings: Settings,
    reviewer_comments: str | None,
) -> dict:
    """Assemble the ``build_agent_from_definition`` overrides for refine:
    the reviewer-sendback prompt + thread flags when handling feedback.
    The model comes from the definition's ``level`` (refine is level 3
    → Claude Opus)."""
    overrides: dict = {}
    if reviewer_comments:
        overrides["system_prompt"] = REVIEWER_SENDBACK_PROMPT
        overrides["reply_to_thread"] = True
        overrides["close_thread"] = True
    return overrides


def run_refine_agent(  # noqa: C901 — continuation guard + pre-output/quota checks add branches; tightly-coupled control flow
    *,
    settings: Settings,
    title: str,
    draft: str,
    repo_dir: Path | None = None,
    repo_config: RepoConfig | None = None,
    reviewer_comments: str | None = None,
    memory: str = "",
    epic_context: str = "",
    extra_roots: list[Path] | None = None,
    message_history: list | None = None,
    board_id: str = "",
    current_ticket_id: str = "",
    language_instructions: str = "",
    deployed_log_summary: str = "",
    deployed_log_dir: Path | None = None,
    screenshot_paths: list[Path] | None = None,
    include_explore: bool = True,
    include_parallel_explore: bool = True,
    refine_level: int | None = None,
    refine_model: str | None = None,
    request_limit_override: int | None = None,
    triage_findings: str | None = None,
) -> RefineResult:
    """Return a structured ``RefineResult``. When ``repo_dir`` is given
    the agent grounds the spec in that local clone via explore/
    read_file/list_dir/run_command; otherwise it works draft-only.
    When ``reviewer_comments`` is given the agent incorporates the
    feedback into the refined spec.

    ``message_history`` — when non-``None``, passed directly to
    ``agent.run_sync(…)`` so the agent continues from a prior paused
    conversation (the resume path after ``ask_user``).

    ``screenshot_paths`` — user-supplied image files attached to the
    ticket. When present AND the claude_sdk backend is active for
    refine, each image is read and passed to the model as a
    ``pydantic_ai.BinaryContent`` block so the agent can *see* it. On
    the non-vision default (DeepSeek) path the images are not attached;
    a short text note tells the agent they exist instead.

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

    from pydantic_ai.usage import UsageLimits

    from .yaml_loader import load_agent_definition
    from .base import (
        build_agent_from_definition,
        _safe_close,
        level_uses_claude,
        claude_sdk_supports_inline_image,
    )
    from .retry import run_agent

    definition = load_agent_definition(data_dir("agent_definitions") / "refine.yaml")

    from ._repo_tools import _build_repo_tools

    tools = _build_repo_tools(
        repo_dir,
        settings,
        extra_roots=extra_roots,
        include_parallel_explore=include_parallel_explore,
        include_explore=include_explore,
        read_file_max_calls=settings.max_refine_read_file_calls,
    )

    # Wrap explore / parallel_explore tools with a cap-enforcing counter
    # so sub-agent calls beyond settings.max_refine_explore_calls are
    # rejected.  Track the count in a mutable cell closed over by each
    # wrapper.
    _explore_call_count: list[int] = [0]
    _explore_cap: int = settings.max_refine_explore_calls

    def _wrap_explore_with_cap(tool: Any) -> Any:
        """Return *tool* wrapped to count + cap explore/parallel_explore calls."""
        import functools

        @functools.wraps(tool)
        async def _capped(*args: Any, **kwargs: Any) -> Any:
            if _explore_cap > 0 and _explore_call_count[0] >= _explore_cap:
                return (
                    f"ERROR: exploration cap reached "
                    f"({_explore_call_count[0]}/{_explore_cap} explore/parallel_explore "
                    f"calls already made).  Synthesise your spec from the information "
                    f"you already have — do not request further exploration."
                )
            _explore_call_count[0] += 1
            return await tool(*args, **kwargs)

        return _capped

    _EXPLORE_TOOL_NAMES = {"explore", "parallel_explore"}
    for i, t in enumerate(tools):
        if getattr(t, "__name__", "") in _EXPLORE_TOOL_NAMES:
            tools[i] = _wrap_explore_with_cap(t)

    # Emit a structured log line recording exploration skip/invoke + cap.
    _explore_enabled = include_explore or include_parallel_explore
    if not _explore_enabled:
        log.info(
            "refine exploration: skipped (include_explore=%s, include_parallel_explore=%s)",
            include_explore,
            include_parallel_explore,
        )
    else:
        log.info(
            "refine exploration: invoked (cap=%d, include_explore=%s, include_parallel_explore=%s)",
            _explore_cap,
            include_explore,
            include_parallel_explore,
        )

    # Langfuse read tools — always available (four simple closures).
    from .langfuse_tools import _build_langfuse_tools

    tools.extend(_build_langfuse_tools(settings, repo_config=repo_config))

    # Langfuse trace-inspect sub-agent — only when repo_dir is provided,
    # since its value is grounding findings in the actual source code.
    if repo_dir is not None:
        from .langfuse_tools import make_langfuse_inspect_tool, make_cost_inspect_tool

        tools.append(
            make_langfuse_inspect_tool(settings, repo_dir, repo_config=repo_config)
        )
        tools.append(
            make_cost_inspect_tool(settings, repo_dir, repo_config=repo_config)
        )

    # Deployed-log query tool — only when a deployed log folder is
    # configured and resolved to an existing directory (the resolution
    # happens in the refine orchestration). The static summary orients
    # the agent; this tool lets it drill into specific log lines.
    if deployed_log_dir is not None:
        from .log_tools import make_log_query_tool

        tools.append(make_log_query_tool(deployed_log_dir))

    # Per-trace (cross-consult) web budget + error-loop guard. The
    # per-consult web_fetch caps don't bound fetch/search fan-out across
    # a whole refine run, so a refine loop could re-bill millions of
    # input tokens on runaway web I/O (83 fetches / 22 searches in one
    # observed specimen). Reset the process-global trace budgets once per
    # run (mirroring survey_runner) and wrap the tools with the shared
    # error counter (mirroring trace_inspector / periodic_base).
    from ..agents.web_tools import reset_trace_web_fetch_budget
    from ..agents.web_knowledge import reset_trace_web_search_budget

    reset_trace_web_fetch_budget(
        settings.refine_web_fetch_max_calls,
        settings.refine_web_fetch_max_total_bytes,
    )
    reset_trace_web_search_budget(settings.refine_web_search_max_calls)

    from .trace_inspector import _wrap_tools_with_error_limit

    tools = _wrap_tools_with_error_limit(tools, max_errors=settings.refine_max_errors)

    # --- sendback tool-strip: when handling reviewer feedback the agent's
    # only job is text-level spec revision — filesystem/exploration tools
    # are a cost risk with no legitimate use.  Strip them from the resolved
    # tool set while preserving thread-management tools (reply_to_thread,
    # close_thread, report_issue) which the sendback prompt instructs the
    # agent to use.
    if reviewer_comments:
        _SENDBACK_STRIP_TOOLS = frozenset(
            {"explore", "parallel_explore", "read_file", "list_dir", "run_command"}
        )
        tools = [
            t
            for t in tools
            if getattr(t, "__name__", None) not in _SENDBACK_STRIP_TOOLS
        ]

    overrides = _build_refine_overrides(
        definition,
        settings,
        reviewer_comments,
    )

    # When a cheap triage classifier rules the ticket trivial-scope,
    # override the YAML's default model level (3 / Opus) with the
    # configured trivial level (default 3 = subscription; set to 1/2
    # to roll back to DeepSeek).
    if refine_level is not None:
        overrides["level"] = refine_level

    # Right-size the level-3 Claude model (subscription transport unchanged).
    # Skip when downgraded to a DeepSeek level (1/2), which ignores `model`.
    resolved_level = refine_level if refine_level is not None else 3
    if resolved_level == 3 and refine_model is not None:
        overrides["model"] = refine_model

    # When exploration sub-agents are gated off (triage ruled the ticket
    # "simple"), strip the now-dangling `explore`/`parallel_explore` call
    # directives from the system prompt so the build-time prompt/tool
    # consistency guard doesn't raise ValueError for the absent tools.
    if not include_explore or not include_parallel_explore:
        base_prompt = overrides.get("system_prompt", definition.system_prompt)
        overrides["system_prompt"] = _strip_explore_call_directives(
            base_prompt,
            include_explore=include_explore,
            include_parallel_explore=include_parallel_explore,
        )

    # Block ask_web_knowledge when the draft is an internal
    # CI/type/lint/test failure — web-searching internal toolchain
    # errors wastes model turns on irrelevant results.  The tool
    # stays registered (no prompt-tool-consistency violation); the
    # block_reason is returned immediately if the agent calls it.
    if is_internal_toolchain_failure(draft):
        overrides["web_knowledge_block_reason"] = (
            "ask_web_knowledge is disabled for this ticket: it is an internal "
            "CI/type/lint/test failure, which is a local code fix. Work from "
            "the failing logs and the repo — do not web-search."
        )

    agent = build_agent_from_definition(
        settings,
        definition,
        tools=tools,
        board_id=board_id,
        current_ticket_id=current_ticket_id,
        **overrides,
    )

    # -- delta-context trimming: re-refine passes ------------------------
    # When the stage re-invokes us with reviewer_comments (sendback),
    # this is a re-refine pass — the model already saw the full draft,
    # epic context, and memory ledger on the first pass.  Re-sending
    # them in full inflates every call.  Pass only the delta: a minimal
    # draft reminder plus the reviewer comments themselves.
    _re_refine_draft = draft
    _re_refine_epic = epic_context
    _re_refine_memory = memory
    if reviewer_comments and settings.delta_context_retry_enabled:
        from ..core.delta_context import trim_draft_for_re_refine

        _re_refine_draft = trim_draft_for_re_refine(draft)
        _re_refine_epic = ""
        _re_refine_memory = ""

    # Build user prompt: title, draft, memory, and optionally reviewer feedback.
    user_prompt = ""
    if language_instructions:
        user_prompt += "## Language conventions\n\n" + language_instructions + "\n\n"
    if deployed_log_summary:
        user_prompt += (
            "## Deployed system logs\n\n"
            + deployed_log_summary
            + "\n\nYou may also call `query_app_logs` with keywords drawn "
            "from the ticket and a recency window (`since_hours`) to pull "
            "relevant log excerpts instead of reading whole files.\n\n"
        )
    if _re_refine_epic:
        user_prompt += f"{_re_refine_epic}\n\n"
    user_prompt += (
        section("title", title)
        + "\n"
        + section("draft", _re_refine_draft)
        + "\n\n"
        + section("memory", _re_refine_memory or "(empty — start a new ledger)")
    )
    if triage_findings:
        user_prompt += "\n\n" + section("triage-findings", triage_findings)
    # Deterministic ground truth for warnings-hardening tickets: run the
    # suite's warning collection ONCE here and inject it, so the agent writes
    # the ignore list from facts instead of re-running pytest many times.
    user_prompt += _collect_test_warnings_block(draft, repo_dir, settings)
    if reviewer_comments:
        user_prompt += "\n\n" + section(
            "reviewer-feedback",
            "The reviewer sent this spec back with the following "
            "comments. Address each one in the revised spec:\n\n"
            f"{reviewer_comments}",
        )

    # Decide the prompt payload: attach screenshots as vision input only
    # on the claude_sdk path AND only when that backend can actually view
    # inline images (the capability gate — default OFF, because the
    # installed llmio bridge silently mishandles BinaryContent and stalls
    # the CLI for 1200s). The DeepSeek default has no vision either. When
    # images exist but the backend can't see them, leave a text note so
    # the agent knows they're there.
    _vision = (
        bool(screenshot_paths)
        and level_uses_claude(3)  # refine is level 3 → Claude SDK
        and claude_sdk_supports_inline_image(settings)
    )
    binary_contents: list[Any] = []
    if _vision:
        from pydantic_ai import BinaryContent

        _media_types = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
        }
        for sp in screenshot_paths or []:
            media_type = _media_types.get(sp.suffix.lower())
            if media_type is None:
                continue
            try:
                data = sp.read_bytes()
            except OSError as e:
                log.warning("refine: could not read screenshot %s: %s", sp, e)
                continue
            binary_contents.append(BinaryContent(data=data, media_type=media_type))
        if not binary_contents:
            _vision = False
    elif screenshot_paths:
        user_prompt += "\n\n" + section(
            "attached-screenshots",
            f"{len(screenshot_paths)} screenshot(s) are attached to this "
            "ticket but cannot be viewed by the current model backend "
            "(no vision). Refine from the text draft.",
        )

    prompt_payload: object = (
        [user_prompt, *binary_contents] if binary_contents else user_prompt
    )

    limits = UsageLimits(
        request_limit=request_limit_override
        if request_limit_override is not None
        else settings.refine_request_limit,
        tool_calls_limit=settings.refine_max_tool_calls,
    )

    try:
        result = run_agent(
            agent,
            lambda h: h.run_sync(
                prompt_payload,
                message_history=message_history,
                usage_limits=limits,
            ),
            what="refine",
        )

        # Guard: if the agent hit the iteration limit while the model
        # was still requesting tool calls, the last response has
        # finish_reason == "tool_call" and result.output may be empty.
        # Synthesise a final answer with a single continuation call
        # that includes the full message history for context.
        finish_reason = getattr(
            getattr(result, "response", None), "finish_reason", None
        )
        if finish_reason == "tool_call":
            # Pre-output guard: if the agent already produced a valid
            # RefineResult (e.g. a complete spec landed in an earlier
            # turn before a verification loop), skip the continuation
            # to avoid burning quota on redundant tool calls.
            if isinstance(result.output, RefineResult) and (
                result.output.spec_markdown
                or result.output.epic_body
                or result.output.children
            ):
                pass  # Already have a good result; skip continuation
            else:
                # Pre-turn quota check: refuse to start a continuation
                # when there are ≤ 5 requests remaining — the call
                # would likely fail mid-turn and burn quota without
                # producing a usable result.
                remaining = limits.request_limit - result.usage.requests
                if remaining <= 5:
                    pass  # Not enough quota; return what we have
                else:
                    continuation_result = run_agent(
                        agent,
                        lambda h: h.run_sync(
                            "Please synthesise a final answer based on the tool results above.",
                            message_history=result.all_messages(),
                            usage_limits=limits,
                        ),
                        what="refine (continuation after tool_calls stop)",
                    )
                    result = continuation_result

        # Log prompt-cache hit metrics when the provider supports it.
        # Claude CLI auto-caches system prompts >= 1024 chars; OpenRouter
        # reports cached_tokens when an upstream Anthropic cache is warm.
        try:
            _u = result.usage()
            _cache_read = getattr(_u, "cache_read_tokens", 0) or 0
            _cache_write = getattr(_u, "cache_write_tokens", 0) or 0
            if _cache_read or _cache_write:
                _total_in = _u.input_tokens
                _pct = round(100 * _cache_read / _total_in, 1) if _total_in else 0.0
                log.info(
                    "refine prompt cache: read=%d write=%d input=%d (%.1f%% cached)",
                    _cache_read,
                    _cache_write,
                    _total_in,
                    _pct,
                )
            # Warn when the refine pass consumed > threshold fraction of
            # its request budget, so near-exhaustion patterns are observable
            # even when the run doesn't hit UsageLimitExceeded.
            _effective_limit = (
                request_limit_override
                if request_limit_override is not None
                else settings.refine_request_limit
            )
            _requests_used = _u.requests
            if (
                _effective_limit > 0
                and _requests_used / _effective_limit
                > settings.refine_usage_warning_threshold
            ):
                log.warning(
                    "refine agent used %d/%d requests (%.0f%%) — "
                    "near request_limit exhaustion",
                    _requests_used,
                    _effective_limit,
                    100 * _requests_used / _effective_limit,
                )
        except Exception:
            log.debug("refine: cache-metric extraction failed", exc_info=True)

        output: RefineResult = _coerce_refine_output(result.output)

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
