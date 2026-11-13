"""Client for Anthropic's Messages API (Claude models).

Differences from the OpenAI wire format that this adapter hides:
  * the system prompt is a top-level field, not a message,
  * tool calls are `tool_use` content blocks and results are `tool_result` blocks
    inside a *user* message,
  * consecutive tool results must be grouped into a single user message.
"""

from __future__ import annotations

from typing import Any

import anthropic

from app.llm.base import ChatMessage, LLMError, LLMResponse, ToolCall, ToolSpec


class AnthropicLLM:
    def __init__(self, model: str, api_key: str | None, timeout: float = 30.0,
                 temperature: float = 0.3, max_tokens: int = 600, name: str = "anthropic"):
        if not model:
            raise ValueError("LLM_MODEL must be set for the anthropic provider (e.g. claude-haiku-5-5)")
        self.name = name
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout, max_retries=0)

    async def chat(self, system: str, messages: list[ChatMessage], tools: list[ToolSpec] | None = None,
                   temperature: float | None = None, max_tokens: int | None = None,
                   json_mode: bool = False) -> LLMResponse:
        if json_mode:
            system += "\n\nRespond with a single valid JSON object and nothing else."
        kwargs: dict[str, Any] = {
            "model": self.model,
            "system": system,
            "messages": self._to_wire(messages),
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.max_tokens,
        }
        if tools:
            kwargs["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools]
        try:
            resp = await self._client.messages.create(**kwargs)
        except (anthropic.RateLimitError, anthropic.APITimeoutError, anthropic.APIConnectionError,
                anthropic.InternalServerError) as e:
            raise LLMError(str(e), retryable=True, provider=self.name) from e
        except anthropic.APIStatusError as e:
            raise LLMError(str(e), retryable=e.status_code in (429, 529) or e.status_code >= 500, provider=self.name) from e

        text_parts, calls = [], []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                calls.append(ToolCall(id=block.id, name=block.name, arguments=dict(block.input or {})))
        return LLMResponse(
            text="".join(text_parts),
            tool_calls=calls,
            model=resp.model,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            stop_reason=resp.stop_reason or "",
        )

    @staticmethod
    def _to_wire(messages: list[ChatMessage]) -> list[dict[str, Any]]:
        wire: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "tool":
                block = {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}
                # Group consecutive tool results into one user turn.
                if wire and wire[-1]["role"] == "user" and isinstance(wire[-1]["content"], list) \
                        and wire[-1]["content"] and wire[-1]["content"][0].get("type") == "tool_result":
                    wire[-1]["content"].append(block)
                else:
                    wire.append({"role": "user", "content": [block]})
            elif m.role == "assistant" and m.tool_calls:
                content: list[dict[str, Any]] = []
                if m.content:
                    content.append({"type": "text", "text": m.content})
                content += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments} for c in m.tool_calls]
                wire.append({"role": "assistant", "content": content})
            else:
                wire.append({"role": m.role, "content": m.content})
        return wire
