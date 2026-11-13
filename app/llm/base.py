"""The one interface the rest of the codebase uses to talk to an LLM.

Why our own interface instead of using a vendor SDK everywhere?
  * Swap providers (or self-host an open-weights model) with an env var.
  * Fall back across providers when one is down (Part 4).
  * Test the agent deterministically with `FakeLLM`.

The interface supports tool calling from day one even though Part 1 doesn't use it,
so Part 2 can add Shopify tools without changing any client.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass(slots=True)
class ToolSpec:
    """A function the model may call. `parameters` is a JSON Schema object."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class ChatMessage:
    role: Literal["user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)  # assistant -> tools
    tool_call_id: str | None = None  # tool -> assistant (which call this answers)
    name: str | None = None  # tool name, for role="tool"

    @staticmethod
    def tool_result(call: ToolCall, result: Any) -> "ChatMessage":
        content = result if isinstance(result, str) else json.dumps(result, default=str)
        return ChatMessage(role="tool", content=content, tool_call_id=call.id, name=call.name)


@dataclass(slots=True)
class LLMResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = ""


class LLMError(RuntimeError):
    """Raised for provider failures. `retryable` drives retry/fallback logic."""

    def __init__(self, message: str, retryable: bool = True, provider: str = ""):
        super().__init__(message)
        self.retryable = retryable
        self.provider = provider


class LLMClient(Protocol):
    name: str

    async def chat(
        self,
        system: str,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> LLMResponse: ...
