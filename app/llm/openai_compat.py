"""Client for any OpenAI-compatible Chat Completions endpoint.

Works with OpenAI, Azure OpenAI, and the open-source serving stack (vLLM, Ollama,
LM Studio, llama.cpp server, TGI), so the same code can run on a hosted model or a
self-hosted open-weights model behind your firewall.
"""

from __future__ import annotations

import json
from typing import Any

import openai

from app.llm.base import ChatMessage, LLMError, LLMResponse, ToolCall, ToolSpec


class OpenAICompatLLM:
    def __init__(self, model: str, api_key: str | None, base_url: str | None, timeout: float = 30.0,
                 temperature: float = 0.3, max_tokens: int = 600, name: str = "openai"):
        if not model:
            raise ValueError("LLM_MODEL must be set for the openai provider")
        self.name = name
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        # Local servers (Ollama/vLLM) accept any key; the SDK insists on a non-empty one.
        self._client = openai.AsyncOpenAI(api_key=api_key or "not-needed", base_url=base_url,
                                          timeout=timeout, max_retries=0)  # retries live in our layer

    async def chat(self, system: str, messages: list[ChatMessage], tools: list[ToolSpec] | None = None,
                   temperature: float | None = None, max_tokens: int | None = None,
                   json_mode: bool = False) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}] + [self._to_wire(m) for m in messages],
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.max_tokens,
        }
        if tools:
            kwargs["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in tools
            ]
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = await self._client.chat.completions.create(**kwargs)
        except (openai.RateLimitError, openai.APITimeoutError, openai.APIConnectionError, openai.InternalServerError) as e:
            raise LLMError(str(e), retryable=True, provider=self.name) from e
        except openai.APIError as e:
            raise LLMError(str(e), retryable=False, provider=self.name) from e

        choice = resp.choices[0]
        calls = [
            ToolCall(id=tc.id, name=tc.function.name, arguments=_safe_json(tc.function.arguments))
            for tc in (choice.message.tool_calls or [])
        ]
        usage = resp.usage
        return LLMResponse(
            text=choice.message.content or "",
            tool_calls=calls,
            model=resp.model,
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            stop_reason=choice.finish_reason or "",
        )

    @staticmethod
    def _to_wire(m: ChatMessage) -> dict[str, Any]:
        if m.role == "tool":
            return {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content}
        msg: dict[str, Any] = {"role": m.role, "content": m.content or None}
        if m.tool_calls:
            msg["tool_calls"] = [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                for c in m.tool_calls
            ]
        return msg


def _safe_json(s: str | None) -> dict[str, Any]:
    try:
        v = json.loads(s or "{}")
        return v if isinstance(v, dict) else {}
    except json.JSONDecodeError:
        return {}
