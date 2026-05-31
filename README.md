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
   upstream provider to DeepSeek (warm prompt cache) and a tier→reasoning policy
   (`default`→`effort: xhigh`; `cheap`→`reasoning disabled`). pydantic-ai
   round-trips reasoning natively, so this layer neither remaps reasoning nor
   adds a DeepSeek-specific transient signature (it inherits OpenRouter's). The
   models are **baked**: `default = deepseek/deepseek-v4-pro`,
   `cheap = deepseek/deepseek-v4-flash`.

### Alternative transport — Claude Agent SDK (subscription auth)

`robotsix_llmio.claude_sdk` is a **sibling of the OpenRouter layer** (both derive
from `core.LLMProvider`) that needs **no API key**: it drives the local `claude`
CLI through the [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk),
so it authenticates with your `claude login` (Claude Code subscription / OAuth)
credentials. `ClaudeSDKProvider` maps `default→opus`, `cheap→haiku`.

Because the SDK runs its own agent loop and executes tools internally — returning
only final text, never raw `tool_use` blocks — this transport supports
`output_type=str` and pydantic-ai's `PromptedOutput` (JSON-in-text), but **not**
function/tool calling or the default tool-based structured output (those raise a
clear `UserError`). Each request also spawns a fresh CLI subprocess and pays
Claude Code's injected system-prompt overhead, so it's a convenience transport,
not a hot path. Runtime needs Node.js and a logged-in `claude` CLI.

```python
from pydantic import BaseModel
from pydantic_ai import PromptedOutput
from robotsix_llmio.claude_sdk import ClaudeSDKProvider
from robotsix_llmio.core import Tier

provider = ClaudeSDKProvider()  # no key — uses your `claude login` session

class City(BaseModel):
    name: str
    country: str

agent = provider.build_agent(
    tier=Tier.CHEAP, system_prompt="Extract the city.",
    output_type=PromptedOutput(City), name="extract",
)
result = provider.call_with_retry(lambda: agent.run_sync("Tell me about Kyoto."))
print(result.output)  # name='Kyoto' country='Japan'
agent.close()
```

> Auth note: Anthropic restricts offering claude.ai login to third-party *end
> users*; driving your *own* subscription from your own automation is the
> intended personal use. Keep this transport for your own tooling.

## Install

```bash
pip install "robotsix-llmio[openrouter_deepseek]"
# or, for the subscription-auth transport (also needs Node + `claude login`):
pip install "robotsix-llmio[claude_sdk]"
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

## Tracing & cost (Langfuse)

Every provider model already stamps per-call cost onto the active OpenTelemetry
span. To ship those spans — traces **and** cost, for any provider — to a
[Langfuse](https://langfuse.com) project, call `setup_langfuse_tracing()` once at
startup. It wires an OTLP exporter to Langfuse and `Agent.instrument_all()`, so
every subsequent agent run is traced. It's a **no-op** unless
`LANGFUSE_PUBLIC_KEY` + `LANGFUSE_SECRET_KEY` are set (`LANGFUSE_BASE_URL`
defaults to Langfuse Cloud), so it's always safe to call.

```bash
pip install "robotsix-llmio[tracing]"   # adds the OTLP exporter (no langfuse SDK)
```

```python
from robotsix_llmio.core import setup_langfuse_tracing, langfuse_session, flush_tracing

setup_langfuse_tracing()  # reads LANGFUSE_* env; no-op without credentials

with langfuse_session("my-run-id"):       # groups the run's spans under one session
    result = provider.call_with_retry(lambda: agent.run_sync("..."))

flush_tracing()  # force-export before exit (or after a run you want shipped)
```

Single-tenant: one Langfuse project per process.
