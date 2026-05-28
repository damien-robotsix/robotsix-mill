"""Cost-reconciliation agent — analyses OpenRouter ↔ Langfuse cost divergence.

A single LLM call (no tools) that receives daily cost data from both
sources and produces a structured analysis.  Used by the periodic
cost-reconciliation runner.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel

from ..config import Settings

log = logging.getLogger("robotsix_mill.agents.cost_reconciling")


class CostReconciliationResult(BaseModel):
    """Structured output from the cost-reconciliation agent."""
    analysis: str = ""
    conclusion: str = ""


def _build_prompt(
    *,
    openrouter_total: float,
    langfuse_total: float,
    delta: float,
    openrouter_breakdown: str,
    langfuse_breakdown: str,
) -> str:
    """Build the prompt for the cost-reconciliation agent."""
    direction = "more" if openrouter_total > langfuse_total else "less"
    return f"""## Cost reconciliation: OpenRouter vs Langfuse

**OpenRouter total:** ${openrouter_total:.4f}  
**Langfuse total:** ${langfuse_total:.4f}  
**Delta:** ${delta:.4f} (OpenRouter reports ${abs(openrouter_total - langfuse_total):.4f} {direction} than Langfuse)

### OpenRouter daily breakdown (by model)

```
{openrouter_breakdown}
```

### Langfuse daily breakdown (by trace name)

```
{langfuse_breakdown}
```

## Task

Analyse the discrepancy between OpenRouter's usage accounting and
Langfuse's span-derived cost totals.  Consider these possible causes:

- Non-OpenRouter models routed through OpenRouter that don't support
  usage accounting (cost missing from spans → Langfuse under-reports).
- Streaming-only providers where OpenRouter can't compute cost until
  the stream completes (timing skew).
- OTel export gaps — if the OTLP exporter was down or a span was
  dropped, Langfuse never saw the cost.
- BYOK (bring-your-own-key) models where OpenRouter charges a routing
  fee but the inference cost hits the user's own provider key.
- Lag — Langfuse ingestion is near-real-time but not instant;
  OpenRouter's activity endpoint reflects completed UTC days.

Return your analysis with a clear conclusion.  Be specific about which
cause(s) are most likely given the data, and whether the gap warrants
investigation or is within normal tolerance."""


def run_cost_reconciliation_agent(
    *,
    settings: Settings,
    openrouter_total: float,
    langfuse_total: float,
    delta: float,
    openrouter_breakdown: str,
    langfuse_breakdown: str,
) -> CostReconciliationResult:
    """Invoke the cost-reconciliation agent.

    Returns a ``CostReconciliationResult`` on success.  On any error,
    returns a fallback result with the error as the conclusion.
    """
    from .base import build_agent_from_definition, _safe_close
    from pydantic_ai.usage import UsageLimits
    from .yaml_loader import load_agent_definition
    from .retry import call_with_retry

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent
        / "agent_definitions" / "periodic" / "cost_reconciliation.yaml"
    )

    agent = build_agent_from_definition(
        settings, definition, tools=[],
        model_name=definition.model or settings.cost_reconciliation_model,
    )
    limits = UsageLimits(request_limit=4)

    try:
        result = call_with_retry(
            lambda: agent.run_sync(
                _build_prompt(
                    openrouter_total=openrouter_total,
                    langfuse_total=langfuse_total,
                    delta=delta,
                    openrouter_breakdown=openrouter_breakdown,
                    langfuse_breakdown=langfuse_breakdown,
                ),
                usage_limits=limits,
            ),
            settings=settings,
            what="cost reconciliation",
        )
        output = result.output
        if isinstance(output, CostReconciliationResult):
            return output
        log.warning(
            "cost reconciliation agent returned unexpected type: %s", type(output)
        )
        return CostReconciliationResult(
            analysis="",
            conclusion=f"Agent returned unexpected type: {type(output).__name__}",
        )
    except Exception:
        log.warning("cost reconciliation agent failed", exc_info=True)
        return CostReconciliationResult(
            analysis="",
            conclusion="Agent invocation failed — see worker logs for details.",
        )
    finally:
        _safe_close(agent)
