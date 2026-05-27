"""The retrospect agent: analyse a finished ticket's workflow + its
Langfuse session and propose a concrete improvement as a draft.

Seam: tests monkeypatch ``run_retrospect_agent``. Structured output so
the stage has a clear spawn decision.
"""

from __future__ import annotations

from pathlib import Path

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
    recent_proposals: str = "",
    epic_context: str = "",
    sibling_context: str = "",
) -> RetrospectResult:
    from .yaml_loader import load_agent_definition
    from .base import build_agent_from_definition, _safe_close

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent / "agent_definitions" / "retrospect.yaml"
    )

    # PromptedOutput (not the default ToolOutput): the cheap driver
    # model has no OpenRouter endpoint for the forced `tool_choice`
    # ToolOutput needs (404), and it doesn't support NativeOutput
    # either — but it produces schema-valid JSON from a prompt fine.
    # This keeps retrospect on the cheap model (no deepseek cost).
    agent = build_agent_from_definition(
        settings, definition, tools=[],
        model_name=definition.model or settings.retrospect_model,
    )
    lf = langfuse_summary or "(no Langfuse trace data — workflow-only review)"
    prompt = (
        f"{recent_proposals}"
        + section("ticket", ticket_summary) + "\n\n"
        + section("workflow", history_text) + "\n\n"
        + section("langfuse", lf) + "\n\n"
        + section("comments", comments_text or '(no comments)') + "\n\n"
        + section("memory", memory or '(empty — start a new ledger)')
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
