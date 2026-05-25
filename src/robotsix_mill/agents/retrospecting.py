"""The retrospect agent: analyse a finished ticket's workflow + its
Langfuse session and propose a concrete improvement as a draft.

Seam: tests monkeypatch ``run_retrospect_agent``. Structured output so
the stage has a clear spawn decision.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, model_validator

from ..config import Settings

_DEEP_ANALYSIS_ADDENDUM = """\

## DEEP ANALYSIS MODE (two-phase)

### Phase 1 — Per-trace inspection

You MUST call `trace_inspect` for EVERY trace in this
session to inspect its full observation tree. The tool returns a text
summary of tool errors, agent limitations, and optimisation
opportunities found in that trace.

The trace IDs to inspect are listed in the prompt below.  Call
`trace_inspect` for each one.

### Phase 2 — Cross-trace analysis

After ALL per-trace inspections complete, call `cross_trace_analyze`
ONCE with all the per-trace summaries concatenated together (prefix
each with its stage name so the analyser can tell which trace is from
which stage).  The tool returns cross-cutting findings — redundant
exploration across stages, information loss, retry cascades, context
waste, and stage inefficiencies — that are invisible in single-trace
analysis.

Treat cross-trace findings with EQUAL weight to per-trace findings:
- Cross-trace patterns are often the highest-impact improvements.
- When a cross-trace finding aligns with a per-trace finding, it
  confirms the issue from multiple angles — record it with high
  confidence.
- Incorporate both per-trace and cross-trace findings into your
  `findings`, `conclusion`, `updated_memory`, and draft decision.
"""


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
                    sym = item.get("symptom", "") or item.get("text", "")
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


def run_retrospect_agent(
    *,
    settings: Settings,
    ticket_summary: str,
    history_text: str,
    langfuse_summary: str | None,
    memory: str = "",
    comments_text: str = "",
    deep_analysis: bool = False,
    trace_ids: list[tuple[str, str]] | None = None,
    recent_proposals: str = "",
    epic_context: str = "",
    sibling_context: str = "",
) -> RetrospectResult:
    from .yaml_loader import load_agent_definition
    from .base import build_agent_from_definition, _safe_close

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent / "agent_definitions" / "retrospect.yaml"
    )

    extra_tools = []
    if deep_analysis:
        from .trace_inspector import make_trace_inspect_tool
        from .cross_trace_analyzer import make_cross_trace_analyze_tool

        extra_tools.append(make_trace_inspect_tool(settings))
        extra_tools.append(make_cross_trace_analyze_tool(settings))

    system_prompt = definition.system_prompt
    if deep_analysis:
        system_prompt += _DEEP_ANALYSIS_ADDENDUM

    # PromptedOutput (not the default ToolOutput): the cheap driver
    # model has no OpenRouter endpoint for the forced `tool_choice`
    # ToolOutput needs (404), and it doesn't support NativeOutput
    # either — but it produces schema-valid JSON from a prompt fine.
    # This keeps retrospect on the cheap model (no deepseek cost).
    agent = build_agent_from_definition(
        settings, definition, tools=extra_tools,
        system_prompt=system_prompt,
        model_name=definition.model or settings.retrospect_model,
    )
    lf = langfuse_summary or "(no Langfuse trace data — workflow-only review)"
    prompt = (
        f"{recent_proposals}"
        f"<ticket>\n{ticket_summary}\n</ticket>\n\n"
        f"<workflow>\n{history_text}\n</workflow>\n\n"
        f"<langfuse>\n{lf}\n</langfuse>\n\n"
        f"<comments>\n{comments_text or '(no comments)'}\n</comments>\n\n"
        f"<memory>\n{memory or '(empty — start a new ledger)'}\n</memory>"
    )
    if epic_context:
        prompt += f"\n\n{epic_context}"
    if sibling_context:
        prompt += f"\n\n{sibling_context}"
    if deep_analysis and trace_ids:
        ids_text = "\n".join(f"- {tid} ({tname})" for tid, tname in trace_ids)
        prompt += (
            f"\n\n<trace_ids>\n{ids_text}\n</trace_ids>\n\n"
            "Phase 1: call trace_inspect() on each trace above.\n"
            "Phase 2: call cross_trace_analyze() with all per-trace summaries together."
        )
    from .retry import call_with_retry

    try:
        result = call_with_retry(
            lambda: agent.run_sync(prompt), settings=settings, what="retrospect"
        )
    finally:
        _safe_close(agent)
    return result.output
