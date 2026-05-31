# robotsix-llmio

Provider-agnostic LLM I/O for [pydantic-ai](https://ai.pydantic.dev) agents,
with derived per-provider layers that bake in the known-working settings so a
consumer only ever picks a **tier** (`default` or `cheap`).

## Layers

1. **`robotsix_llmio.core`** — provider-agnostic base: the `LLMProvider` ABC,
   the `Tier` enum, bounded retry/backoff (`call_with_retry`, `is_transient`,
   `is_rate_limited`), cost-on-span recording, a timeout HTTP client, and the
   generic pydantic-ai `Agent` assembler. All numeric parameters (timeouts,
   retry counts, backoff) are **baked constants** — not tunable.
2. **`robotsix_llmio.openrouter`** — OpenRouter transport: auth/base-url,
   `usage.include` opt-in, cost extraction from `usage.cost`, and the
   OpenRouter upstream-error transient signature. Model-family agnostic.
3. **`robotsix_llmio.openrouter_deepseek`** — the derived layer most consumers
   plug in. Extends the OpenRouter layer with DeepSeek specifics: pin the
   upstream provider to DeepSeek (warm prompt cache), tier→reasoning policy
   (`default`→`effort: xhigh` + `reasoning_details` round-trip;
   `cheap`→`reasoning disabled`), and the thinking-mode `400` transient
   detector. The models are **baked**: `default = deepseek/deepseek-v4-pro`,
   `cheap = deepseek/deepseek-v4-flash`.

## Install

```bash
pip install "robotsix-llmio[openrouter_deepseek]"
```

## Configuration

The API key can be passed directly to the provider constructor or set via the
`OPENROUTER_API_KEY` environment variable. Copy `.env.example` to `.env` and
replace the placeholder with a real key — `.env` is git-ignored so the secret
never leaves your machine.

## Use

```python
from robotsix_llmio.openrouter_deepseek import OpenRouterDeepseekProvider
from robotsix_llmio.core import Tier

provider = OpenRouterDeepseekProvider(api_key="sk-or-...")  # or OPENROUTER_API_KEY env

agent = provider.build_agent(
    tier=Tier.CHEAP,
    system_prompt="You are a reviewer. Return a verdict.",
    tools=[],
    output_type=str,
    name="review",
)
result = provider.call_with_retry(lambda: agent.run_sync("Review this diff: ..."))
agent.close()
```

The only knobs are the provider you import and the tier you pass. Everything
else — reasoning policy, retry/backoff, timeouts, cost instrumentation — is
fixed at values proven in production.
