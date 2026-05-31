"""The retrospect agent: analyse a finished ticket's workflow + its
Langfuse session and propose a concrete improvement as a draft.

Seam: tests monkeypatch ``run_retrospect_agent``. Structured output so
the stage has a clear spawn decision.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, model_validator

from ..config import Settings
from .prompt_blocks import section

# Per-trace deep inspection formerly gated by `_DEEP_ANALYSIS_ADDENDUM`
# was removed — trace / cost evaluation is now handled by the
# periodical cost-evaluation pipeline (cost_reconciliation_runner,
# trace_health_runner, expensive-item detector). The retrospect agent
# only reasons about the pre-computed session summary now.


class RetrospectResult(BaseModel):
    findings: str
    conclusion: str
    propose_draft: bool = False
    draft_title: str | None = None
    draft_body: str | None = None
    updated_memory: str = ""
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

    @model_validator(mode="before")
    @classmethod
    def _absorb_findings_list_shape(cls, data):
        """The retrospect agent sometimes emits the trace-inspector's
        list-of-TraceFinding shape for ``findings`` instead of the
        single string this schema requires. Symptom seen on trace
        ``1db69cc322c07f77`` (2026-05-21 22:28): the model returned
        ``{findings: [{category, symptom, root_cause, proposed_solution,
        confidence}, ...]}`` mirroring TraceInspectResult, pydantic-ai
        rejected it because ``findings: str`` required a string, output
        retries exhausted, the retrospect cost ($0.16) was wasted with
        no artifact written.

        Render the list back into a single string (one line per
        finding, prefixed by category + confidence) so the canonical
        ``findings: str`` field carries the same information. Canonical
        string input passes straight through.
        """
        if not isinstance(data, dict):
            return data
        f = data.get("findings")
        if isinstance(f, list):
            lines: list[str] = []
            for item in f:
                if isinstance(item, dict):
                    cat = item.get("category", "")
                    conf = item.get("confidence", "")
                    sym = item.get("symptom", "")
                    sol = item.get("proposed_solution", "")
                    prefix = f"[{cat}/{conf}] " if cat else ""
                    line = f"{prefix}{sym}"
                    if sol:
                        line += f"  (fix: {sol})"
                    lines.append(line)
                else:
                    lines.append(str(item))
            data["findings"] = "\n".join(lines) if lines else ""
        return data


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
    # Scan forward from *start* looking for the first unescaped
    # double-quote whose following structural context (comma then
    # next key, or closing brace) marks it as the field terminator.
    # We track a minimal escape flag so that backslash-escaped
    # quotes inside the value don't fool the scanner.
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if escaped:
            escaped = False
            continue
        if ch == '\\':
            escaped = True
            continue
        if ch != '"':
            continue
        # Candidate closing quote — check what follows.
        j = i + 1
        while j < len(text) and text[j] in ' \t\n\r':
            j += 1
        if j >= len(text):
            return text  # truncated — can't repair
        if text[j] == '}':
            pass  # end-of-object terminator
        elif text[j] == ',':
            # Comma — must be followed by the next key or closing brace
            k = j + 1
            while k < len(text) and text[k] in ' \t\n\r':
                k += 1
            if k >= len(text) or text[k] not in ('"', '}'):
                continue  # not a structural comma — keep scanning
        else:
            continue  # not a terminator — keep scanning

        # Try extracting and re-escaping the value between start and i
        raw = text[start:i]
        escaped_val = json.dumps(raw)[1:-1]  # strip surrounding quotes
        repaired = text[:start] + escaped_val + text[i:]
        try:
            json.loads(repaired)
            return repaired
        except json.JSONDecodeError:
            continue

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
    epic_context: str = "",
    sibling_context: str = "",
) -> RetrospectResult:
    from .yaml_loader import load_agent_definition
    from .base import build_agent_from_definition, _safe_close

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent
        / "agent_definitions"
        / "retrospect.yaml"
    )

    # PromptedOutput (not the default ToolOutput): the cheap driver
    # model has no OpenRouter endpoint for the forced `tool_choice`
    # ToolOutput needs (404), and it doesn't support NativeOutput
    # either — but it produces schema-valid JSON from a prompt fine.
    # This keeps retrospect on the cheap model (no deepseek cost).
    agent = build_agent_from_definition(
        settings,
        definition,
        tools=[],
        model_name=definition.model or settings.retrospect_model,
    )

    # Register a pre-parse repair hook so the agent's output can be
    # salvaged when the model emits unescaped newlines / quotes inside
    # the ``updated_memory`` JSON string value.  The hook fires after
    # the model returns raw text but before pydantic-core attempts
    # JSON parsing, avoiding the retry loop for this class of error.
    from pydantic_ai.capabilities import Hooks

    agent.root_capability.capabilities.append(
        Hooks(
            before_output_validate=lambda ctx, output_context, output: (
                _repair_memory_field_escaping(output)
                if isinstance(output, str)
                else output
            )
        )
    )

    lf = langfuse_summary or "(no Langfuse trace data — workflow-only review)"
    prompt = (
        f"{recent_proposals}"
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
    from .retry import call_with_retry

    try:
        result = call_with_retry(
            lambda: agent.run_sync(prompt), settings=settings, what="retrospect"
        )
    finally:
        _safe_close(agent)
    return result.output
