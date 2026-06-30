"""The retrospect agent: analyse a finished ticket's workflow + its
Langfuse session and propose a concrete improvement as a draft.

Seam: tests monkeypatch ``run_retrospect_agent``. Structured output so
the stage has a clear spawn decision.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from ..config import Settings
from .prompt_blocks import section

log = logging.getLogger(__name__)

# Known no-data placeholders for the pre-computed Langfuse summary. The
# first doubles as the workflow-only fallback injected into the prompt
# when ``langfuse_summary`` is falsy.
_NO_LANGFUSE_PLACEHOLDER = "(no Langfuse trace data — workflow-only review)"
_NO_TRACES_PLACEHOLDER = "(no Langfuse traces found for this session)"


def _langfuse_absent(langfuse_summary: str | None) -> bool:
    """Return True when *langfuse_summary* carries no real trace data.

    Treats ``None``, empty/whitespace-only strings, and the known
    no-data placeholders as absent.
    """
    if langfuse_summary is None:
        return True
    stripped = langfuse_summary.strip()
    if not stripped:
        return True
    return stripped in (_NO_LANGFUSE_PLACEHOLDER, _NO_TRACES_PLACEHOLDER)


# Per-trace deep inspection formerly gated by `_DEEP_ANALYSIS_ADDENDUM`
# was removed — trace / cost evaluation is now handled by the
# periodical pipeline (trace_health_runner, expensive-item detector).
# The retrospect agent only reasons about the pre-computed session
# summary now.


class MemoryEdit(BaseModel):
    """A single targeted edit to the retrospect memory ledger.

    Expresses one change without re-emitting the whole ledger. ``op``
    selects the operation (``append``, ``replace``, or ``remove``);
    ``find`` is the exact existing ledger block to locate (required for
    ``replace``/``remove``, matched verbatim including the ``## ``
    heading); and ``text`` is the new content (the section to add for
    ``append`` or the replacement for ``replace``; ignored for
    ``remove``).
    """

    op: Literal["append", "replace", "remove"]
    # For "replace"/"remove": the EXACT existing block of ledger text to
    # locate (must match verbatim, including the "## " heading line).
    find: str = ""
    # New content. For "append": the section(s) to add. For "replace":
    # the replacement text. Ignored for "remove".
    text: str = ""


class RetrospectResult(BaseModel):
    """Structured outcome of a retrospect pass over a finished ticket.

    ``findings`` and ``conclusion`` are the agent's analysis of the
    ticket's workflow and Langfuse session. The ``propose_draft`` /
    ``draft_title`` / ``draft_body`` / ``draft_gap_id`` fields describe
    an optional improvement ticket to file, and ``follow_up_title`` /
    ``follow_up_body`` an optional continuation ticket; ``draft_target``
    and ``follow_up_target`` route each to the current repo's board or
    the mill maintenance board. Memory updates flow through exactly one
    of ``updated_memory`` (full re-emit), ``memory_edits`` (targeted
    :class:`MemoryEdit` operations, preferred for modifications), or
    ``memory_delta`` — in that precedence order. ``agented_md_proposals``
    carries any proposed ``AGENT.md`` changes.
    """

    findings: str
    conclusion: str
    propose_draft: bool = False
    draft_title: str | None = None
    draft_body: str | None = None
    updated_memory: str = ""
    memory_delta: str | None = None
    # Exactly one memory path is used per response. ``memory_edits`` is
    # the PREFERRED path for MODIFICATIONS (resolve / move / repair /
    # remove an existing entry): it expresses a targeted change without
    # re-emitting the whole ledger, replacing the old requirement to use
    # the full ``updated_memory`` re-emit (PATH 3) for edits. Stage
    # precedence: ``updated_memory`` > ``memory_edits`` > ``memory_delta``.
    memory_edits: list[MemoryEdit] | None = None
    draft_gap_id: str | None = None
    follow_up_title: str | None = None
    follow_up_body: str | None = None
    # Target board for the proposed draft. ``"current"`` (default)
    # files on the same repo as the ticket being retrospected — right
    # for issues specific to that codebase. ``"mill"`` files on the
    # mill maintenance board (resolved via
    # ``settings.trace_review_target_repo_id``) — right for issues
    # about mill's pipeline itself: agent prompts, stage handlers,
    # silent failure modes, retry logic. The retrospect prompt
    # describes when to pick each.
    draft_target: Literal["current", "mill"] = "current"
    # Same routing for the follow-up draft. Most follow-ups are
    # incomplete-work continuations on the same ticket's repo
    # (default "current"); reserve "mill" for follow-ups that
    # describe a mill-internal gap the retrospect surfaced.
    follow_up_target: Literal["current", "mill"] = "current"
    agented_md_proposals: list[dict] | None = None


def _is_structural_quote_end(text: str, quote_idx: int) -> bool:
    """Return True if the double-quote at *quote_idx* is followed by a
    JSON structural terminator (``}`` or ``,`` then ``"`` or ``}``)
    after optional whitespace."""
    j = quote_idx + 1
    while j < len(text) and text[j] in " \t\n\r":
        j += 1
    if j >= len(text):
        return False
    if text[j] == "}":
        return True
    if text[j] != ",":
        return False
    k = j + 1
    while k < len(text) and text[k] in " \t\n\r":
        k += 1
    return k < len(text) and text[k] in ('"', "}")


def _find_memory_value_end(text: str, start: int) -> int | None:
    """Scan forward from *start* for the closing unescaped double-quote
    of the ``updated_memory`` field value.  Returns the index of that
    quote, or ``None`` if no valid terminator is found.
    """
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch != '"':
            continue
        if _is_structural_quote_end(text, i):
            return i
    return None


def _repair_memory_field_escaping(text: str) -> str:
    """Attempt to repair unescaped characters in the ``updated_memory``
    field of a JSON string before pydantic-core parsing.

    Fast path: returns *text* unchanged when it is already valid JSON.
    On failure, extracts the ``updated_memory`` value from the raw text,
    re-escapes it via :func:`json.dumps`, and reconstructs the field.
    Returns the original text if repair fails.
    """
    import json
    import re

    # Fast path: already valid JSON
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        pass

    # Locate the "updated_memory" field opening quote
    m = re.search(r'"updated_memory"\s*:\s*"', text)
    if not m:
        return text

    start = m.end()  # first character of the field value
    end = _find_memory_value_end(text, start)
    if end is None:
        return text

    raw = text[start:end]
    escaped_val = json.dumps(raw)[1:-1]  # strip surrounding quotes
    repaired = text[:start] + escaped_val + text[end:]
    try:
        json.loads(repaired)
        return repaired
    except json.JSONDecodeError:
        return text


def run_retrospect_agent(
    *,
    settings: Settings,
    ticket_summary: str,
    history_text: str,
    langfuse_summary: str | None,
    memory: str = "",
    comments_text: str = "",
    recent_proposals: str = "",
    verified_proposals: str = "",
    epic_context: str = "",
    sibling_context: str = "",
    repo_dir: Path | None = None,
) -> RetrospectResult:
    """Analyse a finished ticket and propose a concrete improvement.

    Builds the retrospect agent from its YAML definition and runs it
    over the ticket's workflow history plus its Langfuse session
    summary, returning a structured :class:`RetrospectResult`. When a
    repository clone is available the agent gets read-only filesystem
    tools (``read_file``, ``list_dir``, ``run_command``) so it can
    verify gap claims before filing follow-ups. A pre-parse repair hook
    salvages output whose ``updated_memory`` JSON contains unescaped
    characters, and non-structured output is degraded safely via
    :func:`_coerce_result`.

    Args:
        settings: Application configuration — model name
            (``retrospect_model``) and retry parameters.
        ticket_summary: Summary of the finished ticket under review.
        history_text: The ticket's rendered workflow history.
        langfuse_summary: Pre-computed Langfuse session summary, or
            ``None`` for a workflow-only review.
        memory: The agent's memory ledger as a Markdown string.
        comments_text: The ticket's operator/agent comment history.
        recent_proposals: Recent prior proposals prepended to the
            prompt for dedup awareness.
        verified_proposals: Verified-state table of prior proposals for
            ledger reconciliation.
        epic_context: Optional epic context block appended to the
            prompt.
        sibling_context: Optional sibling-ticket context block appended
            to the prompt.
        repo_dir: Optional path to the local repository clone; when set
            and present, enables the read-only filesystem tools.

    Returns:
        A :class:`RetrospectResult` with findings, conclusion, optional
        draft/follow-up proposals, and memory updates. Degrades to an
        empty result when the agent returns unparseable output.
    """
    # Pre-LLM guard: when there is genuinely no auditable run evidence
    # (no Langfuse trace data AND empty workflow history AND no comments),
    # short-circuit instead of running a full generation over a near-empty
    # prompt — that path lets the model fabricate PR numbers / scores. The
    # ticket description alone is not run evidence. A Langfuse-less but
    # history-present ticket is the supported workflow-only review and is
    # NOT short-circuited here.
    if (
        _langfuse_absent(langfuse_summary)
        and history_text.strip() == ""
        and comments_text.strip() == ""
    ):
        return RetrospectResult(
            findings=(
                "No auditable trace or workflow data was available for this "
                "ticket: there were no Langfuse traces and the workflow "
                "history and comments were empty. No retrospect was performed "
                "and no claims could be verified — there was nothing to audit."
            ),
            conclusion=(
                "Insufficient audit data — no Langfuse traces and empty "
                "workflow history; retrospect skipped."
            ),
        )

    from .yaml_loader import load_agent_definition
    from .base import build_agent_from_definition, _safe_close

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent
        / "agent_definitions"
        / "retrospect.yaml"
    )

    # Build read-only filesystem tools when a clone is available so
    # the agent can verify concrete gap claims before filing follow-ups.
    # Include `explore` / `parallel_explore` sub-agents for complex
    # multi-step verification — their internal calls don't count against
    # the retrospect agent's pydantic-ai `request_limit`, preventing the
    # saturation that previously BLOCKED already-delivered tickets.
    tools: list = []
    if repo_dir is not None and repo_dir.exists():
        from .fs_tools import build_fs_tools
        from .explore import make_explore_tool, make_parallel_explore_tool

        tools = [
            make_explore_tool(settings, repo_dir),
            make_parallel_explore_tool(settings, repo_dir),
        ]
        ro_tool_names: set[str] = {"read_file", "list_dir", "run_command"}
        tools += [
            t for t in build_fs_tools(repo_dir, settings) if t.__name__ in ro_tool_names
        ]

        from ..core.tool_wrappers import wrap_read_tools_with_consecutive_error_guard

        tools = wrap_read_tools_with_consecutive_error_guard(tools)

    # PromptedOutput (not the default ToolOutput): the cheap driver
    # model has no OpenRouter endpoint for the forced `tool_choice`
    # ToolOutput needs (404), and it doesn't support NativeOutput
    # either — but it produces schema-valid JSON from a prompt fine.
    # This keeps retrospect on the cheap model (no deepseek cost).
    agent = build_agent_from_definition(
        settings,
        definition,
        tools=tools,
        repo_dir=repo_dir,
    )

    # Register a pre-parse repair hook so the agent's output can be
    # salvaged when the model emits unescaped newlines / quotes inside
    # the ``updated_memory`` JSON string value.  The hook fires after
    # the model returns raw text but before pydantic-core attempts
    # JSON parsing, avoiding the retry loop for this class of error.
    from pydantic_ai.capabilities import Hooks

    # The pre-parse repair hook is a pydantic-ai capability AND a DeepSeek-
    # output-quirk workaround. It only applies on the pydantic-ai/OpenRouter
    # path; the Claude SDK transport handle has no ``root_capability`` (and
    # the SDK owns its own loop + emits clean JSON), so skip it there.
    root_capability = getattr(agent, "root_capability", None)
    if root_capability is not None:
        root_capability.capabilities.append(
            Hooks(
                before_output_validate=lambda ctx, output_context, output: (
                    _repair_memory_field_escaping(output)
                    if isinstance(output, str)
                    else output
                )
            )
        )

    lf = langfuse_summary or _NO_LANGFUSE_PLACEHOLDER
    verified_block = ("\n\n" + verified_proposals) if verified_proposals else ""
    prompt = (
        f"{recent_proposals}"
        + verified_block
        + "\n\n"
        + section("ticket", ticket_summary)
        + "\n\n"
        + section("workflow", history_text)
        + "\n\n"
        + section("langfuse", lf)
        + "\n\n"
        + section("comments", comments_text or "(no comments)")
        + "\n\n"
        + section("memory", memory or "(empty — start a new ledger)")
    )
    if epic_context:
        prompt += f"\n\n{epic_context}"
    if sibling_context:
        prompt += f"\n\n{sibling_context}"
    if repo_dir is not None and repo_dir.exists():
        prompt += (
            f"\n\nRepository working directory: {repo_dir}\n"
            "The filesystem tools (read_file, list_dir, run_command) are restricted "
            "to this directory only — paths above it will fail. The memory ledger "
            "is already provided inline in this prompt; do not read_file it separately.\n"
        )
    from .retry import run_agent

    try:
        result = run_agent(agent, lambda h: h.run_sync(prompt), what="retrospect")
        from .structured_output_guard import reprompt_if_unstructured

        result = reprompt_if_unstructured(
            result=result,
            agent=agent,
            expected_type=RetrospectResult,
            reprompt_message=(
                "Your last response did not produce a structured RetrospectResult. "
                "Reply now with a JSON object containing the required fields: "
                "findings, conclusion, and the optional draft / memory fields "
                "per the schema."
            ),
            settings=settings,
            what="retrospect (re-prompt after prose-only)",
            require_no_tool_calls=False,
        )
    finally:
        _safe_close(agent)
    return _coerce_result(result.output)


def _coerce_result(output: object) -> RetrospectResult:
    """Return *output* as a :class:`RetrospectResult`, degrading safely.

    pydantic-ai can fall back to raw text when the structured-output parse
    fails even after the ``_repair_memory_field_escaping`` hook. Returning that
    bare str would crash the retrospect STAGE on ``res.updated_memory``
    ("'str' object has no attribute 'updated_memory'") and knock an already-DONE
    ticket back to BLOCKED — retrospect runs last and is advisory, so a parse
    blip must never undo a completed ticket. On a non-model output we degrade to
    an empty result: no memory update, no drift check, no spawned draft — the
    stage proceeds harmlessly.
    """
    if isinstance(output, RetrospectResult):
        return output
    log.warning(
        "retrospect: agent returned non-structured output (%s); "
        "skipping memory update for this run",
        type(output).__name__,
    )
    return RetrospectResult(
        findings="(retrospect output could not be parsed)",
        conclusion="retrospect skipped — agent returned unstructured output",
    )
