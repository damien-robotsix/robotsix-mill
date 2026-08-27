Bumped `robotsix-llmio` off 0.5.0, which baked two defects into every mill
agent call: level 1 pointed at `deepseek/deepseek-v4-flash-latest`, a slug
OpenRouter rejects with `400 not a valid model ID`, and level 2's 32768
output cap was smaller than what `xiaomi/mimo-v2.5-pro` spends on `xhigh`
reasoning before emitting a token. Together they blocked the implement stage
outright — the primary blew the cap, then the fallback 400ed. Level-1 model
assertions in the test suite now derive the slug from `default_tier_config()`
instead of hardcoding it; hardcoding is what let a green CI certify the
invalid id.
