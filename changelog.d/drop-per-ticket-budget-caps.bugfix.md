Turn off the per-ticket runaway budgets (`max_spend_usd_per_ticket`,
`max_traces_per_ticket`, `max_openrouter_marginal_usd_per_ticket`) and remove
`max_tokens` from every agent definition.

A per-ticket budget is the wrong unit for guarding against a model that
consumes erratically: that is a property of the model, not of whichever ticket
happened to be running, so the cap punishes the unlucky ticket while the real
problem continues on the next one. Measured against real fleet behaviour these
fired on ordinary long work rather than on runaways — on 2026-08-06 the trace
cap alone had 20 tickets BLOCKED at $0.00 of recorded OpenRouter spend.

Agent `max_tokens` could not be honoured at all on the Claude SDK transport:
it was forwarded as an advisory `task_budget` that capped nothing and instead
told the model it had a small allowance for the whole task. Also bumps the
llmio pin to pick up the transport-side fix.

The mechanism is retained, not deleted — set any cap non-zero to re-arm it.
`max_turns` and the per-stage wall-clock timeout remain the real backstops.
