"""End-to-end validation that event-mode OTel logging delivers message
events to Langfuse.

This is a one-shot operator script — NOT a pytest test.  It requires a
live Langfuse project and a real LLM call (OpenRouter).  Run from the
mill repo root with valid credentials in the environment:

    OPENROUTER_API_KEY=sk-... \
    LANGFUSE_PUBLIC_KEY=pk-... \
    LANGFUSE_SECRET_KEY=sk-... \
    LANGFUSE_HOST=https://cloud.langfuse.com \
    ./.venv/bin/python scripts/validate_event_mode.py

The script:
1. Enables event-mode tracing (``_ensure_tracing()``).
2. Builds a pydantic-ai agent with a simple tool.
3. Sends a user prompt that triggers ≥2 tool calls and ≥3 turns.
4. Flushes all pending spans + LogRecords.
5. Prints trace/session IDs for manual Langfuse UI lookup.
6. Prints any export failures and relevant log output.

Returns exit code 0 on success (agent conversation completed).
"""

from __future__ import annotations

import os
import sys
import tempfile
import time


def main() -> int:
    # --- credential check -----------------------------------------------
    from robotsix_mill.config import Settings, get_secrets

    secrets = get_secrets()
    if not secrets.openrouter_api_key:
        print("SKIP: no OPENROUTER_API_KEY")
        return 0
    if not secrets.langfuse_public_key:
        print("SKIP: no LANGFUSE_PUBLIC_KEY")
        return 0

    # --- cheap/fast model override --------------------------------------
    # Use a flash-tier model to keep cost minimal — configurable via env.
    model_name = os.environ.get("VALIDATE_MODEL", "deepseek/deepseek-v4-flash")

    # --- enable event-mode tracing --------------------------------------
    from robotsix_mill.runtime.tracing import (
        _ensure_tracing,
        flush_tracing,
        get_export_failures,
        make_session_id,
        start_ticket_root_span,
    )

    _ensure_tracing()  # idempotent; sets up global provider + Agent.instrument_all
    session_id = make_session_id("validate-event-mode")

    # --- build a simple agent with a tool -------------------------------
    s = Settings(
        data_dir=tempfile.mkdtemp(),
        llm_backend="deepseek",
    )

    # Trivial tool: the model must call this before answering about
    # server status.  Two distinct servers forces ≥2 tool calls, which
    # with user→tool_call→result→tool_call→result→assistant gives ≥5
    # back-and-forth turns (> the 3-turn minimum).
    def get_server_status(server_name: str) -> str:
        """Check the status of a server by name.

        Args:
            server_name: The name of the server to check.
        """
        statuses = {
            "alpha": "online, CPU 23%, memory 45%, uptime 7d 3h",
            "beta": "online, CPU 67%, memory 82%, uptime 1d 12h",
        }
        return f"Server '{server_name}' is {statuses.get(server_name.lower(), 'not found')}."

    from robotsix_mill.agents.base import build_agent
    from robotsix_mill.agents.retry import run_agent

    system_prompt = (
        "You are a concise server-monitoring assistant. "
        "When asked about server status, ALWAYS call get_server_status "
        "for every server mentioned before answering. "
        "Answer in one or two sentences."
    )

    agent = build_agent(
        s,
        system_prompt=system_prompt,
        tools=[get_server_status],
        model_name=model_name,
        name="validate-event-mode",
        report_issue=False,
        reply_to_thread=False,
        close_thread=False,
        ask_user=False,
    )

    # --- run the agent inside a traced session --------------------------
    trace_id: str | None = None
    try:
        with start_ticket_root_span(session_id, "validate-event-mode") as root:
            trace_id = root.trace_id
            print(f"session_id: {session_id}")
            print(f"trace_id:   {trace_id}")

            user_prompt = (
                "Check the status of servers alpha and beta, "
                "then tell me which one has higher memory usage."
            )
            print(f"user: {user_prompt}")

            t0 = time.monotonic()
            out = run_agent(
                agent,
                lambda h: h.run_sync(user_prompt),
                settings=s,
                what="validate-event-mode",
            )
            elapsed = time.monotonic() - t0
            text = str(out.output)
            print(f"assistant ({elapsed:.1f}s): {text!r}")
            root.set_output(text)
    except Exception as exc:
        print(f"Agent run failed: {exc}")
        flush_tracing()
        return 2
    finally:
        agent.close()  # type: ignore[attr-defined]

    # --- flush everything to Langfuse -----------------------------------
    print("Flushing traces + logs to Langfuse ...")
    flush_tracing(timeout=15_000)

    # --- report ---------------------------------------------------------
    failures = get_export_failures()
    if failures:
        print(f"\n⚠ Export failures ({len(failures)}):")
        for f in failures:
            print(f"  [{f['at']}] {f['project']}: {f['error']}")
    else:
        print("\n✓ No export failures recorded.")

    if trace_id:
        langfuse_host = (
            os.environ.get("LANGFUSE_HOST") or "https://cloud.langfuse.com"
        ).rstrip("/")
        langfuse_project = (
            secrets.langfuse_project_name or secrets.langfuse_project_id or ""
        )
        if langfuse_host and langfuse_project:
            print(
                f"\nLangfuse trace URL: {langfuse_host}/project/{langfuse_project}/traces/{trace_id}"
            )
        print(f"\nInspect with: python scripts/inspect_trace.py {trace_id}")

    print("\nPASS: agent conversation completed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
