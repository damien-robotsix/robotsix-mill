# Image handling with `build_agent(images=...)`

Mill feeds screenshots and other attached images to agents through
**llmio's `images=` parameter** on `build_agent`, rather than a
Claude-only vision gate. The mechanism is **provider-agnostic**: the
same `images=[(media_type, bytes), ...]` call works whether the agent
runs on the Claude SDK transport or the OpenRouter (DeepSeek) fallback
transport. The transport decides *how* the image is consumed; the
caller's prompt always stays text-only.

## How it works

`build_agent` (in `src/robotsix_mill/agents/base.py`) accepts two
extra keyword arguments and forwards them to the provider's
`build_agent`:

| Argument | Type | Purpose |
|----------|------|---------|
| `images` | `Sequence[tuple[str, bytes]] \| None` | Attached images as `(media_type, bytes)` pairs |
| `vision_api_key` | `str \| None` | API key for the vision binding's provider (OpenRouter) |

```python
from robotsix_mill.agents.base import build_agent

png_bytes = (repo_dir / "screenshot.png").read_bytes()

agent = build_agent(
    settings,
    system_prompt="Describe the rendered board.",
    images=[("image/png", png_bytes)],
    vision_api_key=get_secrets().openrouter_api_key,
)
```

The same call works for every provider:

- **Claude SDK (default slot)** — llmio delivers the images to the
  model **natively** (Claude reads image blocks directly). No
  `ask_image` tool is attached, and `vision_api_key` is ignored for
  signature compatibility — the native transport never consults the
  vision binding.
- **OpenRouter / DeepSeek (fallback slot)** — the text-only backend
  cannot ingest image bytes (the request fails with *"No endpoints
  found that support image input"*), so llmio instead hands the agent
  an **`ask_image` tool** answered by the `TierConfig.vision` binding,
  plus a system-prompt note telling the agent it can query the attached
  images by index.

### The prompt stays text-only

In **both** cases the caller's prompt must remain text-only — **never
embed `BinaryContent` when passing `images=`**. On the Claude SDK path
llmio injects the image blocks for you; on the OpenRouter path it
injects the `ask_image` tool and the image note. If you also embed the
bytes as text you double-send them (and on the Claude SDK bridge the
raw `BinaryContent` repr is useless).

```python
# ❌ wrong — bytes embedded in the prompt
agent = build_agent(settings, system_prompt=f"...{png_bytes!r}...")

# ✅ right — pass bytes via images=, keep the prompt text-only
agent = build_agent(settings, system_prompt="...", images=[("image/png", png_bytes)])
```

## `vision_api_key` sourcing

`vision_api_key` authenticates the vision model that answers
`ask_image` calls on text-only transports. It is resolved with this
precedence (llmio):

1. the explicit `vision_api_key` argument,
2. else the provider's own OpenRouter key, when the provider is an
   OpenRouter-family provider,
3. else the vision provider's environment fallback —
   `OPENROUTER_API_KEY`.

In mill, screenshot paths set it explicitly to the OpenRouter key:

```python
overrides["images"] = [("image/png", png_bytes)]
overrides["vision_api_key"] = get_secrets().openrouter_api_key
```

So when attaching images, make sure an OpenRouter key is available
(either via `OPENROUTER_API_KEY` or the provider carrying it) — the
vision binding always talks to OpenRouter regardless of the agent's
main transport slot. On the Claude SDK native path no vision key is
needed; the argument is accepted and ignored.

## Configuring the `TierConfig.vision` binding

llmio's `TierConfig` carries a `vision` field: the model that answers
`ask_image` tool calls on behalf of text-only transports. It is
**resolved directly — never through the provider slots or failover** —
so a vision model is picked independently of the agent's level/slot
binding. The baked default is:

```
openrouter-deepseek/deepseek-v4-flash-vision-exp   (max_tokens 8192)
```

You can override the vision binding per provider by constructing a
`TierConfig` with a different `vision` `TierLevelConfig` and passing it
through llmio's factory (`default_tier_config()` → `for_level(...)` →
`get_provider_for_level(...)`). In Python:

```python
from robotsix_llmio.config.tier import TierConfig, TierLevelConfig
from robotsix_llmio import get_provider_for_level

tier_config = TierConfig(
    vision=TierLevelConfig(
        # Any vision-capable OpenRouter model identifier; the baked default
        # is openrouter-deepseek/deepseek-v4-flash-vision-exp.
        model="openrouter-deepseek/deepseek-v4-flash-vision-exp",
        max_tokens=8192,
    ),
)
provider = get_provider_for_level(
    2, slot=None, api_key=get_secrets().openrouter_api_key, tier_config=tier_config
)
```

The equivalent YAML/JSON shape (as accepted by llmio's
`load_tier_config`) for the vision block:

```yaml
vision:
  model: openrouter-deepseek/deepseek-v4-flash-vision-exp
  max_tokens: 8192
```

## Migration guide: from the Claude-only vision gate

Earlier versions of mill gated screenshot/vision input behind a
**Claude-only vision gate**:

- a `claude_sdk_vision_enabled` config flag (env
  `MILL_CLAUDE_SDK_VISION_ENABLED`), and
- a `claude_sdk_supports_inline_image(settings)` helper that returned
  that flag, so the refine/review screenshot paths only emitted inline
  `BinaryContent` when it was `true` and otherwise degraded to a text
  note.

That gate is **removed**. Image feeding is now provider-agnostic and
driven entirely by passing bytes through `images=` on `build_agent`,
which llmio routes correctly per transport. To migrate:

1. **Drop the flag check.** Stop branching on
   `claude_sdk_vision_enabled` / `claude_sdk_supports_inline_image`.
   Attach images unconditionally via
   `build_agent(..., images=[(media_type, bytes)], ...)`.
2. **Pass bytes, not `BinaryContent`.** Replace any code that embedded
   image bytes or `BinaryContent` in the prompt with the `images=`
   parameter and keep the prompt text-only.
3. **Set `vision_api_key` for text-only transports.** When your agent
   may run on the OpenRouter fallback slot, pass the OpenRouter key as
   `vision_api_key` so the `ask_image` vision binding can authenticate.
4. **Remove the `claude_sdk_vision_enabled` config.** Delete the flag
   from your Settings model, `config.example.json`, the schema, and the
   configuration reference (see the note on the
   `core.claude_sdk_vision_enabled` row in
   [Configuration & Deployment](../config/configuration.md)).

If your config still sets `claude_sdk_vision_enabled`, it is now inert
on the image path — screenshots reach the model via `images=` no matter
what it says. The legacy flag is accepted for backward compatibility
until the setting is fully removed; new configs should omit it.

## Degradation behaviour

Screenshot feeding degrades gracefully:

- **Missing / unreadable screenshot** — the bytes are `None`, so no
  `images=` is passed and the agent runs text-only; it never crashes or
  changes routing on a bad screenshot.
- **Vision model unreachable** (OpenRouter path) — the `ask_image`
  tool returns an explanatory string the agent can read and act on
  rather than raising into the agent loop, and a warning is logged.

## See also

- [Attaching screenshots](../core/screenshots.md) — how a screenshot
  gets onto a ticket from the board.
- [Agent API reference](../agents/reference.md) — `robotsix_mill.agents.base.build_agent` and the agent modules.
