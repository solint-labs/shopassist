"""A deterministic stand-in for an LLM, used by tests and local demos.

Two modes:
  * scripted: pass a list of `LLMResponse`s (or callables) and they are returned in order;
  * rule-based default: simple, predictable behaviour good enough to exercise every
    code path (tool calls, grounded answers, JSON classification) without a network.

Every call is recorded in `self.calls` so tests can assert on the exact prompt sent.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from app.llm.base import ChatMessage, LLMResponse, ToolCall, ToolSpec

Scripted = LLMResponse | Callable[[str, list[ChatMessage], list[ToolSpec] | None], LLMResponse]


class FakeLLM:
    name = "fake"

    def __init__(self, script: list[Scripted] | None = None):
        self.script = list(script or [])
        self.calls: list[dict[str, Any]] = []

    async def chat(self, system: str, messages: list[ChatMessage], tools: list[ToolSpec] | None = None,
                   temperature: float | None = None, max_tokens: int | None = None,
                   json_mode: bool = False) -> LLMResponse:
        self.calls.append({"system": system, "messages": list(messages), "tools": tools, "json_mode": json_mode})
        if self.script:
            item = self.script.pop(0)
            return item(system, messages, tools) if callable(item) else item
        return self.default_response(system, messages, tools, json_mode)

    # Rules are intentionally simple and documented so tests read naturally.
    def default_response(self, system: str, messages: list[ChatMessage], tools: list[ToolSpec] | None,
                         json_mode: bool) -> LLMResponse:
        last = messages[-1] if messages else ChatMessage(role="user", content="")
        if json_mode:
            return LLMResponse(text="{}", model="fake")
        # After a tool ran, summarise its result.
        if last.role == "tool":
            return LLMResponse(text=f"Here's what I found: {_summarise(last.content)}", model="fake")
        # Ask for an order lookup when an order number appears and the tool exists.
        tool_names = {t.name for t in tools or []}
        m = re.search(r"#?(\d{4,})", last.content)
        if m and "get_order_status" in tool_names:
            return LLMResponse(text="", tool_calls=[ToolCall(id="call_1", name="get_order_status",
                                                             arguments={"order_number": m.group(1)})], model="fake")
        # Answer from retrieved context when present (Part 2+).
        ctx = re.search(r"<source id=\"(\d+)\"[^>]*>\s*(.+?)</source>", system, re.S)
        if ctx:
            first_sentence = re.split(r"(?<=[.!?])\s", ctx.group(2).strip())[0]
            return LLMResponse(text=f"{first_sentence} [{ctx.group(1)}]", model="fake")
        return LLMResponse(text=f"Thanks for reaching out! You said: {last.content[:120]}", model="fake")


def _summarise(content: str) -> str:
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return content[:200]
    if isinstance(data, dict):
        return ", ".join(f"{k}: {v}" for k, v in list(data.items())[:5])
    return str(data)[:200]
