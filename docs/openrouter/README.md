# `robotsix_llmio.openrouter`

OpenRouter transport layer (model-family agnostic).

## Module structure

| File | Purpose |
|------|---------|
| `model.py` | `OpenRouterModel`, cost extraction, usage-include injection, model settings resolution |
| `provider.py` | `OpenRouterProvider` |
| `transient.py` | `is_openrouter_transient`, `is_openrouter_upstream_error` — lightweight helpers that avoid pulling in pydantic-ai/OpenTelemetry |
