# robotsix_llmio claude_sdk

Claude Agent SDK transport layer (subscription / ``claude login`` auth).

Requires the ``claude_sdk`` extra, a logged-in ``claude`` CLI, and Node.js
at runtime.  The model and provider are loaded lazily so importing the
lightweight transient helpers stays free of the SDK.

## Exports

- `ClaudeSDKProvider` — pydantic-ai provider backed by the Claude Agent SDK
- `ClaudeSDKModel` — pydantic-ai model implementation
- `ClaudeSDKTurnLimitError` — raised when a subscription turn limit is reached
- `is_claude_sdk_transient` — detect transient errors from the Claude SDK
- `is_claude_sdk_turn_limit` — detect turn-limit errors
