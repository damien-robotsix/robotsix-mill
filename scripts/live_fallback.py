"""Out-of-band live check for the Claude→DeepSeek model fallback.

The hermetic pytest suite strips OPENROUTER_API_KEY and blocks all network, so
this end-to-end path (real Claude run forced to fail → real DeepSeek answers)
can't run there. This standalone script does, against the real `claude` CLI and
OpenRouter. Run from the mill repo root with a logged-in `claude` and an
OpenRouter key in the environment:

    OPENROUTER_API_KEY=sk-... ./.venv/bin/python scripts/live_fallback.py

It forces every Claude query() to trip a 1ms wall-clock cap (so the primary
fails after its local retries are exhausted), then asserts the DeepSeek fallback
returned an answer.
"""

from __future__ import annotations

import shutil
import sys
import tempfile


def main() -> int:
    if shutil.which("claude") is None:
        print("SKIP: claude CLI not on PATH")
        return 0

    from robotsix_llmio.core import constants as llmio_constants

    from robotsix_mill.agents import base
    from robotsix_mill.agents.fallback import FallbackAgentHandle
    from robotsix_mill.agents.retry import run_agent
    from robotsix_mill.config import Settings, get_secrets

    if not get_secrets().openrouter_api_key:
        print("SKIP: no OPENROUTER_API_KEY")
        return 0

    # Force every Claude query() to time out instantly → terminal primary failure.
    llmio_constants.SDK_QUERY_TIMEOUT = 0.001

    s = Settings(data_dir=tempfile.mkdtemp(), llm_backend="claude_sdk")
    handle = base.build_agent(
        s,
        system_prompt="You are terse. Reply with a single word.",
        name="fallback-live",
        model_name="deepseek/deepseek-v4-flash",
        report_issue=False,
        reply_to_thread=False,
        close_thread=False,
        ask_user=False,
    )
    assert isinstance(handle, FallbackAgentHandle), type(handle).__name__

    out = run_agent(
        handle,
        lambda h: h.run_sync("Reply with exactly the word: ok"),
        settings=s,
        what="fallback-live",
        sleep=lambda _d: None,  # no backoff; the primary fails fast
    )
    text = str(out.output)
    print(f"fallback output: {text!r}")
    assert "ok" in text.lower(), text
    assert handle._fallback is not None, "fallback handle was never built"
    handle.close()
    print("PASS: Claude failed locally, DeepSeek fallback answered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
