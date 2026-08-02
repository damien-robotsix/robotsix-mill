Delegate the Claude SDK `task_budget` floor and API-400 classification to `robotsix_llmio` instead of reimplementing them locally.

`robotsix-llmio` is bumped to `c65df6b`, which adds `build_task_budget()` (clamps a below-floor `max_tokens` up to the API's 20,000-token `task_budget` minimum) and `ClaudeSDKPermanentAPIError` (an API 400, excluded from the transient set). With those in place:

- `refine.yaml` returns to its intended `max_tokens: 8192`. The previous bump to `20000` was mill working around the floor itself, which also silently loosened the cap on the OpenRouter path, where `max_tokens` is a real per-response limit rather than an advisory budget. llmio now clamps only the Claude SDK value, warning once.
- `agents/retry.py` drops its local `_is_permanent_api_error` message matcher and its `_chain_contains` helper in favour of llmio's `is_claude_sdk_permanent_api_error`. `is_transient` no longer needs an explicit guard either — `is_claude_sdk_transient` already excludes a 400 ahead of the degenerate-`success` signature. `_is_claude_sdk_degenerate_result` still defers to the library predicate, since the refine runner consults it directly when deciding whether to swallow a failure as an empty result.

Adds a regression test asserting that **every** agent definition's `max_tokens` yields an API-valid `task_budget`, so a future below-floor value can't take a stage offline again. This also covers `retrospect` (16384) and `periodic/completeness_check` (8192), which were previously safe only because they don't route to the Claude SDK.
