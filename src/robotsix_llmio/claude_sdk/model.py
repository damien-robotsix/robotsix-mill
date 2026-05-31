"""Claude Agent SDK transport — a pydantic-ai ``Model`` over the ``claude`` CLI.

Drives the Claude Agent SDK (``claude_agent_sdk``) in **single-turn** mode and
adapts it to pydantic-ai's :class:`~pydantic_ai.models.Model` contract. The
appeal: it authenticates with your local ``claude login`` (Claude Code
subscription / OAuth) credentials — **no API key** — because the SDK spawns the
``claude`` CLI subprocess, which carries that auth.

Scope / limitations (by construction):
- The SDK runs its *own* agent loop and executes tools internally; it returns
  only final assistant text, never raw ``tool_use`` blocks. So this transport
  supports ``output_type=str`` and pydantic-ai's ``PromptedOutput`` (JSON in
  text), but **not** function/tool calling or the default tool-based structured
  output — those raise a clear :class:`UserError` instead of misbehaving.
- Every request spawns a fresh CLI subprocess and pays Claude Code's injected
  system-prompt overhead. This is a convenience transport, not a hot path.

Runtime requirements (beyond the ``claude_sdk`` extra): Node.js and the
``claude`` CLI installed and logged in (``claude login``).
"""

from __future__ import annotations

from typing import Any

from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RequestUsage

PROVIDER_NAME = "claude-sdk"

# Output modes this transport can satisfy with a plain text completion. The
# tool-based modes ('tool', 'native') need raw tool_use passthrough we can't do.
_TEXT_OUTPUT_MODES = {"text", "prompted"}

# Turn headroom for the SDK loop. With ``allowed_tools=[]`` the model has no
# tools to call, so it answers in a single turn and ends with ``end_turn`` well
# under this cap. The cap exists only as a runaway backstop — but it must NOT be
# tight: the SDK raises ("Reached maximum number of turns") instead of returning
# the answer if the budget is actually hit, so 1 is unsafe.
_MAX_TURNS = 8


def _content_to_text(content: Any) -> str:
    """Flatten a pydantic-ai user/tool content (str or a list of parts) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        out: list[str] = []
        for item in content:  # type: ignore[misc]  # heterogeneous content parts
            text = getattr(item, "text", None)
            out.append(text if isinstance(text, str) else str(item))
        return "\n".join(out)
    return str(content)


def _retry_text(part: RetryPromptPart) -> str:
    """The corrective text pydantic-ai wants shown back to the model on a retry
    (e.g. a JSON-validation failure during PromptedOutput)."""
    model_response = getattr(part, "model_response", None)
    if callable(model_response):
        try:
            return model_response()
        except Exception:  # pragma: no cover - defensive
            pass
    return _content_to_text(getattr(part, "content", ""))


def render_prompt(messages: list[ModelMessage]) -> str:
    """Flatten the pydantic-ai message history into a single prompt string for
    the (stateless-per-call) SDK ``query``. A lone user turn is sent verbatim;
    multi-turn history is rendered as a labelled transcript so the model sees
    its own prior attempt and any correction."""
    turns: list[tuple[str, str]] = []
    for message in messages:
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, UserPromptPart):
                    turns.append(("user", _content_to_text(part.content)))
                elif isinstance(part, ToolReturnPart):
                    turns.append(
                        (
                            "user",
                            f"Tool result ({part.tool_name}): "
                            f"{_content_to_text(part.content)}",
                        )
                    )
                elif isinstance(part, RetryPromptPart):
                    turns.append(("user", _retry_text(part)))
        elif isinstance(message, ModelResponse):
            text = "\n".join(
                p.content for p in message.parts if isinstance(p, TextPart)
            )
            if text:
                turns.append(("assistant", text))

    if len(turns) == 1 and turns[0][0] == "user":
        return turns[0][1]
    return "\n\n".join(
        f"{'User' if role == 'user' else 'Assistant'}: {text}" for role, text in turns
    )


def _map_usage(result: Any) -> RequestUsage:
    """Map a Claude Agent SDK ``ResultMessage.usage`` dict onto pydantic-ai's
    :class:`RequestUsage`. Defensive: a missing/partial dict yields zeros."""
    usage = getattr(result, "usage", None) if result is not None else None
    if not isinstance(usage, dict):
        return RequestUsage()
    return RequestUsage(
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
        cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0),
    )


class ClaudeSDKModel(Model):
    """pydantic-ai model backed by the Claude Agent SDK (subscription auth).

    *sdk_model* is the value passed to the SDK's ``model`` option — a Claude
    Code alias (``"opus"``, ``"sonnet"``, ``"haiku"``) or a full model id.
    """

    def __init__(
        self,
        sdk_model: str,
        *,
        model_name: str | None = None,
        settings: ModelSettings | None = None,
    ) -> None:
        super().__init__(settings=settings)
        self._sdk_model = sdk_model
        self._model_name = model_name or sdk_model

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def system(self) -> str:
        return "anthropic"

    @property
    def provider(self) -> None:
        # No HTTP provider: the `claude` CLI subprocess is the transport, and
        # the SDK tears it down per call. The base ``__aenter__``/``__aexit__``
        # short-circuit on a None provider.
        return None

    # --- request ------------------------------------------------------------
    def _reject_unsupported(self, params: ModelRequestParameters) -> None:
        if params.function_tools:
            raise UserError(
                "ClaudeSDKModel does not support function/tool calling: the "
                "Claude Agent SDK executes tools inside its own loop and "
                "returns only final text. Build the agent without tools."
            )
        if params.output_mode not in _TEXT_OUTPUT_MODES:
            raise UserError(
                "ClaudeSDKModel supports only text or PromptedOutput results "
                f"(got output_mode={params.output_mode!r}). For structured "
                "output, wrap your type: output_type=PromptedOutput(MyModel)."
            )

    def _system_text(
        self, messages: list[ModelMessage], params: ModelRequestParameters
    ) -> str | None:
        """The system prompt for the SDK call: pydantic-ai's joined instructions
        (which already include any PromptedOutput JSON-schema directions) plus
        any classic ``SystemPromptPart`` content in the history."""
        parts: list[str] = []
        for message in messages:
            if isinstance(message, ModelRequest):
                parts.extend(
                    p.content
                    for p in message.parts
                    if isinstance(p, SystemPromptPart) and p.content
                )
        # pydantic-ai renamed/replaced the old ``_get_instructions`` (→ str)
        # with ``_get_instruction_parts`` (→ list[InstructionPart] | None);
        # join the parts' content into the system text.
        instruction_parts = self._get_instruction_parts(messages, params)
        if instruction_parts:
            parts.extend(p.content for p in instruction_parts if p.content)
        combined = "\n\n".join(dict.fromkeys(parts))  # de-dup, preserve order
        return combined or None

    async def _invoke(self, prompt: str, system_text: str | None) -> tuple[str, Any]:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            TextBlock,
            query,
        )

        options = ClaudeAgentOptions(
            system_prompt=system_text,
            model=self._sdk_model,
            max_turns=_MAX_TURNS,  # backstop only; no tools => answers in one turn
            allowed_tools=[],  # no built-in tools (Read/Write/Bash/...)
            permission_mode="default",
            setting_sources=[],  # ignore project/user CLAUDE.md + settings
        )

        chunks: list[str] = []
        result: Any = None
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)
            elif isinstance(message, ResultMessage):
                result = message

        text = "".join(chunks).strip()
        if not text and result is not None:
            text = (getattr(result, "result", None) or "").strip()
        return text, result

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        self._reject_unsupported(model_request_parameters)
        system_text = self._system_text(messages, model_request_parameters)
        prompt = render_prompt(messages)
        text, result = await self._invoke(prompt, system_text)
        return ModelResponse(
            parts=[TextPart(content=text)],
            usage=_map_usage(result),
            model_name=self._model_name,
            provider_name=PROVIDER_NAME,
            finish_reason="stop",
        )
