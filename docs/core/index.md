# robotsix_llmio core

Provider-agnostic retry and transient error classification.

## Exports

- `_status` — extract HTTP status code from an exception
- `call_with_retry` — call a function with automatic retries on transient errors
- `is_rate_limited` — detect rate-limit (usage-limit) exceptions
- `is_transient` — detect transient (retryable) exceptions
