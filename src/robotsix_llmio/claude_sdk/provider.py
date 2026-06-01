"""Claude Agent SDK provider — subscription-auth transport, one model per tier.

Sibling of the OpenRouter layer (both derive from :class:`core.LLMProvider`),
but it speaks to no HTTP endpoint: it drives the local ``claude`` CLI via the
Claude Agent SDK, so it needs **no API key** — only a logged-in ``claude``
(``claude login``) and Node.js on PATH.

The only consumer knob is the :class:`~robotsix_llmio.core.Tier`; the tier→model
map is baked (overridable at construction for experimentation).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core._otel import get_tracer, start_span
from ..core.provider import LLMProvider, Tier
from .transient import is_claude_sdk_transient

log = logging.getLogger("robotsix_llmio.claude_sdk")


def _short(value: Any, limit: int = 200) -> str:
    """One-line, length-capped repr of a tool input / text for logging."""
    s = value if isinstance(value, str) else json.dumps(value, default=str)
    s = " ".join(s.split())
    return s if len(s) <= limit else s[:limit] + "…"


def _log_stream_message(message: Any, turn: list[int], label: str) -> None:
    """Emit a concise INFO line for one streamed Claude SDK message.

    Gives live feedback on what the agent is doing — turns, tool calls, tool
    results, the final result — even when Langfuse spans haven't flushed yet
    (a stuck agent never completes its span, so this is the only signal).
    *turn* is a 1-element list used as a mutable counter across the loop.
    """
    cls = type(message).__name__
    try:
        if cls == "AssistantMessage":
            turn[0] += 1
            for block in getattr(message, "content", []) or []:
                bcls = type(block).__name__
                if bcls == "TextBlock":
                    txt = getattr(block, "text", "") or ""
                    if txt.strip():
                        log.info("%s turn %d: text — %s", label, turn[0], _short(txt))
                elif bcls == "ToolUseBlock":
                    log.info(
                        "%s turn %d: tool_use %s(%s)",
                        label,
                        turn[0],
                        getattr(block, "name", "?"),
                        _short(getattr(block, "input", {})),
                    )
                elif bcls == "ThinkingBlock":
                    log.info(
                        "%s turn %d: thinking (%d chars)",
                        label,
                        turn[0],
                        len(getattr(block, "thinking", "") or ""),
                    )
        elif cls in ("UserMessage", "ToolResultMessage"):
            for block in getattr(message, "content", []) or []:
                if type(block).__name__ == "ToolResultBlock":
                    is_err = bool(getattr(block, "is_error", False))
                    log.info(
                        "%s tool_result%s — %s",
                        label,
                        " [ERROR]" if is_err else "",
                        _short(getattr(block, "content", "")),
                    )
        elif cls == "ResultMessage":
            log.info(
                "%s result: subtype=%s is_error=%s turns=%d duration_ms=%s",
                label,
                getattr(message, "subtype", "?"),
                getattr(message, "is_error", "?"),
                turn[0],
                getattr(message, "duration_ms", "?"),
            )
    except Exception:  # noqa: BLE001 — logging must never break the agent loop
        pass


_TRACER_NAME = "robotsix_llmio.claude_sdk"

# Baked tier→model map. Values are Claude Code model aliases passed straight to
# the SDK's ``model`` option (it resolves them to the latest concrete model).
_DEFAULT_MODEL = "opus"
_CHEAP_MODEL = "haiku"

# Structured-output instruction template, mirrored from pydantic-ai's
# ``prompted_output_template`` profile default.
_JSON_OUTPUT_INSTRUCTION = (
    "Always respond with a JSON object that's compatible with this schema:\n"
    "{schema}\n"
    "Don't include any text or Markdown fencing before or after."
)


def _get_inner_type(output_type: Any) -> Any:
    """Return the pydantic model class from *output_type*.

    Handles plain ``BaseModel`` subclasses and ``PromptedOutput`` wrappers.
    """
    from pydantic_ai import PromptedOutput

    if isinstance(output_type, PromptedOutput):
        outputs = output_type.outputs
        if isinstance(outputs, (list, tuple)):
            return outputs[0]
        return outputs
    return output_type


def _parse_output(text: str, output_type: Any) -> Any:
    """Parse final assistant text against *output_type*.

    ``str`` → text as-is.  Otherwise JSON-parse and validate with the inner
    pydantic model.
    """
    if output_type is str:
        return text

    validator = _get_inner_type(output_type)

    data: Any = None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
            except (json.JSONDecodeError, ValueError):
                pass

    if isinstance(data, dict):
        return validator.model_validate(data)
    # Fallback: return raw text if JSON extraction failed.
    return text


def _chat_messages_input(system_prompt: str, user_text: str) -> str:
    """JSON ``{role, content}`` message list (system + user) for a generation
    span's Langfuse input.

    The system prompt IS sent to the SDK (``ClaudeAgentOptions.system_prompt``),
    but the span previously recorded only the user prompt — so traces showed the
    input without the system. Rendering both as chat messages surfaces the
    system prompt in Langfuse (which parses the JSON and shows the roles), the
    same shape the OpenRouter/pydantic-ai path produces."""
    return json.dumps(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        default=str,
    )


def _convert_tools(tools: list[Any]) -> tuple[list[str], Any]:
    """Convert pydantic-ai tools into SDK MCP tools.

    Returns:
        ``(allowed_tools, mcp_server)`` — the *allowed_tools* entries
        (``"mcp__milltools__<name>"``) and the MCP server object to pass
        to ``ClaudeAgentOptions.mcp_servers``.
    """
    from claude_agent_sdk import (  # type: ignore[import-not-found]
        create_sdk_mcp_server,
    )
    from claude_agent_sdk import tool as sdk_tool  # type: ignore[import-not-found]

    import pydantic_ai

    wrapped: list[Any] = []
    allowed: list[str] = []

    for t in tools:
        # Normalize: plain callables become pydantic_ai.Tool (idempotent).
        if not isinstance(t, pydantic_ai.Tool):
            t = pydantic_ai.Tool(t)

        name: str = t.name
        # The SDK's @tool wants a str description; pydantic-ai's may be None.
        description: str = t.description or ""
        schema: dict[str, Any] = t.tool_def.parameters_json_schema
        fn = t.function_schema.function
        is_async: bool = t.function_schema.is_async

        @sdk_tool(name, description, schema)
        async def _wrapper(
            args: dict[str, Any],
            _fn: Any = fn,
            _is_async: bool = is_async,
            _name: str = name,
        ) -> dict[str, Any]:
            # Emit a TOOL span around the actual call, so the tool (and any
            # subagent it runs) nests under the agent-run span in traces.
            with start_span(
                get_tracer(_TRACER_NAME),
                _name,
                {
                    "gen_ai.operation.name": "execute_tool",
                    "gen_ai.tool.name": _name,
                    "langfuse.observation.input": json.dumps(args, default=str),
                },
            ) as sp:
                if _is_async:
                    result = await _fn(**args)
                else:
                    result = _fn(**args)
                if sp is not None:
                    sp.set_attribute("langfuse.observation.output", str(result))
                return {"content": [{"type": "text", "text": str(result)}]}

        wrapped.append(_wrapper)
        allowed.append(f"mcp__milltools__{name}")

    server = create_sdk_mcp_server(name="milltools", tools=wrapped)
    return allowed, server


# Built-in tools whose input names a file the agent is about to write. The
# hook confines these to the workspace; reads/exploration are left free.
_EDIT_TOOLS = "Write|Edit|MultiEdit|NotebookEdit"
_EDIT_PATH_KEYS = ("file_path", "notebook_path", "path")


def _is_within(root: str, target: str) -> bool:
    """True if *target* (resolved, relative paths joined to *root*) is inside
    *root*. ``realpath`` collapses ``..`` and symlinks so escapes are caught."""
    p = target if os.path.isabs(target) else os.path.join(root, target)
    rp = os.path.realpath(p)
    return rp == root or rp.startswith(root + os.sep)


def _make_confine_hook(workspace_root: str):
    """Build a ``PreToolUse`` hook that denies built-in edits outside
    *workspace_root*.

    ``permission_mode="bypassPermissions"`` lets the SDK's built-in
    Write/Edit/etc. write anywhere the process can reach, so a tool-bearing
    agent working on a self-referential ticket can edit the host app's own
    source instead of its checkout. A PreToolUse hook is the one gate the SDK
    consults on *every* call regardless of permission mode (``can_use_tool``
    is skipped under bypass), so it is where confinement must live."""
    root = os.path.realpath(workspace_root)

    async def _hook(
        input: dict[str, Any], tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        tool_input = input.get("tool_input") or {}
        target = next(
            (tool_input[k] for k in _EDIT_PATH_KEYS if tool_input.get(k)), None
        )
        if not target or _is_within(root, str(target)):
            return {}  # no path, or inside the workspace → allow
        log.warning(
            "%s: denied out-of-workspace edit to %s (confined to %s)",
            input.get("tool_name", "edit"),
            target,
            root,
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"Refused: edits are confined to the ticket workspace "
                    f"{root}. {target!r} resolves outside it — edit the "
                    f"corresponding file inside the workspace checkout instead."
                ),
            }
        }

    return _hook


@dataclass
class _SdkToolResult:
    """Minimal result mirroring pydantic-ai's ``AgentRunResult`` interface.

    .. note::

        ``all_messages()`` returns minimal history (a single final
        ``ModelResponse``) — intermediate ``ToolCallPart`` objects are not
        surfaced because the SDK owns the agent loop and tool execution.
    """

    output: Any
    _messages: list[Any]
    _usage: dict[str, Any] | None

    def all_messages(self) -> list[Any]:
        """Best-effort message history (may contain only the final text)."""
        return self._messages

    @property
    def usage(self) -> Any:
        """Aggregate token usage from the final ``ResultMessage``."""
        from pydantic_ai.usage import RequestUsage

        u = self._usage
        if not isinstance(u, dict):
            return RequestUsage()
        return RequestUsage(
            input_tokens=int(u.get("input_tokens") or 0),
            output_tokens=int(u.get("output_tokens") or 0),
            cache_read_tokens=int(u.get("cache_read_input_tokens") or 0),
            cache_write_tokens=int(u.get("cache_creation_input_tokens") or 0),
        )


class _SdkToolAgentHandle:
    """Handle that drives the SDK tool loop directly, bypassing pydantic-ai.

    Satisfies the ``AgentHandle``-compatible contract (``run_sync``,
    ``close``).  Callers interact with the returned :class:`_SdkToolResult`.
    """

    def __init__(
        self,
        sdk_model: str,
        system_prompt: str,
        server: Any,
        allowed_tools: list[str],
        output_type: Any = str,
        name: str | None = None,
        max_turns: int | None = None,
        workspace_root: str | Path | None = None,
    ) -> None:
        self._sdk_model = sdk_model
        self._system_prompt = system_prompt
        self._server = server
        self._allowed_tools = allowed_tools
        self._output_type = output_type
        self._name = name or "claude_sdk agent"
        self._workspace_root = str(workspace_root) if workspace_root else None
        if max_turns is None:
            # Single source of truth for the runaway cap (see model._MAX_TURNS).
            # Generous, because an injected-MCP-tool loop legitimately needs many
            # turns; reaching it is a hard ClaudeSDKTurnLimitError, never retried.
            from .model import _MAX_TURNS

            max_turns = _MAX_TURNS
        self._max_turns = max_turns

    def _warn_dropped_run_kwargs(
        self, model_settings: Any, kwargs: dict[str, Any]
    ) -> None:
        """Loudly flag run arguments this transport can't honor, instead of
        silently dropping them. The SDK owns the agent loop, so pydantic-ai run
        knobs (``model_settings``, ``usage_limits``, ``deps``, …) have no effect
        here — and a silently-ignored ``usage_limits`` or the like is a real
        footgun. ``message_history`` IS honored (folded into the prompt), so it
        is consumed as a named parameter and never reaches here."""
        dropped = list(kwargs)
        if model_settings is not None:
            dropped.append("model_settings")
        if dropped:
            log.warning(
                "%s: the Claude SDK tool path ignores these run arguments "
                "(the SDK runs its own agent loop): %s. Only user_prompt and "
                "message_history are honored.",
                self._name,
                ", ".join(sorted(dropped)),
            )

    def run_sync(
        self,
        user_prompt: str,
        *,
        model_settings: dict[str, Any] | None = None,
        message_history: list[Any] | None = None,
        **kwargs: Any,
    ) -> _SdkToolResult:
        """Run the SDK tool loop synchronously, return ``_SdkToolResult``.

        *message_history* (pydantic-ai ``ModelMessage`` list) is honored: it is
        rendered into the prompt so a multi-turn caller (e.g. a review loop)
        keeps its context. Other pydantic-ai run kwargs can't be honored by the
        SDK loop and are warned about rather than silently dropped."""
        self._warn_dropped_run_kwargs(model_settings, kwargs)
        return asyncio.run(self._run(user_prompt, message_history=message_history))

    async def run(
        self,
        user_prompt: str,
        *,
        model_settings: dict[str, Any] | None = None,
        message_history: list[Any] | None = None,
        **kwargs: Any,
    ) -> _SdkToolResult:
        """Async entry point — awaits the SDK tool loop on the current event
        loop (unlike :meth:`run_sync`, which calls ``asyncio.run``). This lets a
        tool-bearing agent be used as a subagent from inside another agent's
        tool: ``await subagent.run(...)``. Its spans then nest under the calling
        tool's span. *message_history* is honored as in :meth:`run_sync`."""
        self._warn_dropped_run_kwargs(model_settings, kwargs)
        return await self._run(user_prompt, message_history=message_history)

    async def _run(
        self, user_prompt: str, message_history: list[Any] | None = None
    ) -> _SdkToolResult:
        from claude_agent_sdk import (  # type: ignore[import-not-found]
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            TextBlock,
            query,
        )

        system_prompt = self._system_prompt
        output_type = self._output_type

        # Honor message_history: the SDK query is stateless per call, so fold any
        # prior pydantic-ai conversation into the prompt as a labelled transcript
        # (same rendering the no-tools ClaudeSDKModel path uses) and append the
        # new turn. Without this the tool path silently lost a caller's context.
        prompt = user_prompt
        if message_history:
            from .model import render_prompt

            history_text = render_prompt(message_history)
            if history_text:
                prompt = f"{history_text}\n\nUser: {user_prompt}"

        # Augment system prompt with JSON schema for structured output.
        if output_type is not str:
            inner = _get_inner_type(output_type)
            schema_json = json.dumps(inner.model_json_schema())
            system_prompt = f"{system_prompt}\n\n" + _JSON_OUTPUT_INSTRUCTION.format(
                schema=schema_json
            )

        # Confine built-in edits to the workspace when a root is set: run the
        # SDK there (so relative paths + Bash default into it) and gate every
        # Write/Edit/MultiEdit/NotebookEdit through a PreToolUse hook.
        extra: dict[str, Any] = {}
        if self._workspace_root:
            from claude_agent_sdk import HookMatcher  # type: ignore[import-not-found]

            extra["cwd"] = self._workspace_root
            extra["hooks"] = {
                "PreToolUse": [
                    HookMatcher(
                        matcher=_EDIT_TOOLS,
                        hooks=[_make_confine_hook(self._workspace_root)],
                    )
                ]
            }

        options = ClaudeAgentOptions(
            system_prompt=system_prompt,
            model=self._sdk_model,
            max_turns=self._max_turns,
            mcp_servers={"milltools": self._server},
            allowed_tools=self._allowed_tools,
            permission_mode="bypassPermissions",
            setting_sources=[],
            **extra,
        )

        from ..core.cost import record_cost

        chunks: list[str] = []
        result: Any = None
        # Root agent-run span (becomes the trace). Tool spans (from the tool
        # wrapper) nest under it; a child generation span holds the model I/O,
        # token usage, and cost.
        with start_span(
            get_tracer(_TRACER_NAME),
            self._name,
            {
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.system": "anthropic",
                "gen_ai.request.model": self._sdk_model,
                # This span becomes the trace, so render system + user as chat
                # messages here too — the system prompt then shows at the trace
                # root, not only on the child generation.
                "langfuse.observation.input": _chat_messages_input(
                    system_prompt, prompt
                ),
            },
        ) as root:
            turn = [0]
            log.info(
                "%s: starting (model=%s, max_turns=%d)",
                self._name,
                self._sdk_model,
                self._max_turns,
            )

            async def _consume() -> None:
                nonlocal result
                async for message in query(prompt=prompt, options=options):
                    _log_stream_message(message, turn, self._name)
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                chunks.append(block.text)
                    elif isinstance(message, ResultMessage):
                        result = message

            from ..core import constants
            from .model import ClaudeSDKQueryTimeout

            try:
                # Hard wall-clock cap so a stalled CLI subprocess fails fast and
                # retryable instead of hanging on the SDK's own ~2h backstop.
                await asyncio.wait_for(_consume(), timeout=constants.SDK_QUERY_TIMEOUT)
            except (TimeoutError, asyncio.TimeoutError) as exc:
                raise ClaudeSDKQueryTimeout(
                    f"Claude Agent SDK query exceeded the "
                    f"{constants.SDK_QUERY_TIMEOUT:.0f}s per-call wall-clock cap "
                    f"({self._name}, model={self._sdk_model!r}) — the call "
                    f"stalled without completing. Treated as transient so the "
                    f"bounded retry re-runs it."
                ) from exc

            text = "".join(chunks).strip()
            if not text and result is not None:
                text = (getattr(result, "result", None) or "").strip()

            if root is not None:
                root.set_attribute("langfuse.observation.output", text)
            # Child generation span: the model exchange. Carries input/output +
            # token usage + the SDK cost estimate. Cost must sit on a child
            # observation to roll up — a root span becomes the trace, not summed.
            usage_obj = getattr(result, "usage", None) if result is not None else None
            with start_span(
                get_tracer(_TRACER_NAME),
                f"chat {self._sdk_model}",
                {
                    "gen_ai.operation.name": "chat",
                    "gen_ai.system": "anthropic",
                    "gen_ai.request.model": self._sdk_model,
                    # Record system + user as chat messages so the system prompt
                    # (sent to the SDK, but previously absent from traces) shows
                    # up on the generation in Langfuse.
                    "langfuse.observation.input": _chat_messages_input(
                        system_prompt, prompt
                    ),
                    "langfuse.observation.output": text,
                },
            ) as gen:
                if gen is not None and isinstance(usage_obj, dict):
                    in_tok = usage_obj.get("input_tokens")
                    out_tok = usage_obj.get("output_tokens")
                    if in_tok is not None:
                        gen.set_attribute("gen_ai.usage.input_tokens", int(in_tok))
                    if out_tok is not None:
                        gen.set_attribute("gen_ai.usage.output_tokens", int(out_tok))
                record_cost(result, lambda r: getattr(r, "total_cost_usd", None))

        output = _parse_output(text, output_type)

        # Build minimal message history (only the final text is available).
        from pydantic_ai.messages import ModelResponse, TextPart

        messages: list[Any] = [ModelResponse(parts=[TextPart(content=text)])]

        usage = getattr(result, "usage", None) if result is not None else None

        return _SdkToolResult(output=output, _messages=messages, _usage=usage)

    def close(self) -> None:
        """No-op — no HTTP client to close."""
        pass


class ClaudeSDKProvider(LLMProvider):
    """Builds :class:`~robotsix_llmio.claude_sdk.model.ClaudeSDKModel` instances,
    one per tier, authenticated by your ``claude login`` subscription."""

    def __init__(
        self,
        *,
        default_model: str = _DEFAULT_MODEL,
        cheap_model: str = _CHEAP_MODEL,
    ) -> None:
        self._models = {Tier.DEFAULT: default_model, Tier.CHEAP: cheap_model}

    def new_model(self, tier: Tier = Tier.DEFAULT) -> tuple[Any, Any]:
        from .model import ClaudeSDKModel

        # No http_client to manage — the CLI subprocess is the transport, and
        # the SDK tears it down per call. AgentHandle.close() tolerates None.
        return ClaudeSDKModel(self._models[tier]), None

    def _is_transient(self, exc: BaseException) -> bool:
        return is_claude_sdk_transient(exc)

    def build_agent(
        self,
        *,
        tier: Tier = Tier.DEFAULT,
        system_prompt: str,
        tools: list | None = None,
        output_type: Any = str,
        name: str | None = None,
        retries: int = 2,
        workspace_root: str | Path | None = None,
    ) -> Any:
        """Build a ready-to-run agent for *tier*.

        When *tools* is non-empty, returns a :class:`_SdkToolAgentHandle` that
        drives the SDK tool loop directly — intermediate ``ToolCallPart``
        objects are not surfaced.  When *tools* is empty/``None``, delegates
        to the standard pydantic-ai ``Agent`` path (unchanged).

        *workspace_root* confines the agent's built-in file-mutating tools
        (``Write``/``Edit``/``MultiEdit``/``NotebookEdit``) to that directory:
        the SDK runs with ``cwd=workspace_root`` and a ``PreToolUse`` hook
        denies any edit whose target resolves outside it. Without it a
        tool-bearing agent can edit files anywhere the process reaches (e.g.
        the host app's own source) because ``permission_mode`` is
        ``bypassPermissions``. All built-in tools stay available — only
        out-of-scope *writes* are refused. Ignored on the no-tools path
        (no tools → nothing to confine)."""
        if not tools:
            return super().build_agent(
                tier=tier,
                system_prompt=system_prompt,
                tools=tools,
                output_type=output_type,
                name=name,
                retries=retries,
            )

        sdk_model = self._models[tier]
        allowed_tools, server = _convert_tools(tools)
        return _SdkToolAgentHandle(
            sdk_model=sdk_model,
            system_prompt=system_prompt,
            server=server,
            allowed_tools=allowed_tools,
            output_type=output_type,
            name=name,
            workspace_root=workspace_root,
        )
