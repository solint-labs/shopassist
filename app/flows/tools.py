"""Tools that let the LLM hand control to code: start a flow, or escalate to a human.

The model is good at recognising "I want to send this back" as a return request. It
should not run the return itself. These tools only *signal* intent; the orchestrator
starts the deterministic flow or the hand-off after the LLM turn.
"""

from __future__ import annotations

from typing import Any

from app.commerce.tools import Tool, ToolContext
from app.llm.base import ToolSpec


def build_flow_tools() -> list[Tool]:
    async def start_order_cancellation(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        number = str(args["order_number"]).strip().lstrip("#")
        if number not in ctx.state.get("verified_orders", []):
            return {"status": "needs_verification",
                    "instruction": "Verify the order first with get_order_status (order number + checkout email)."}
        ctx.signals["start_flow"] = ("cancel_order", {"order_number": number})
        return {"status": "flow_started", "instruction": "The cancellation flow will take over. Say nothing else."}

    async def start_return_request(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        number = str(args["order_number"]).strip().lstrip("#")
        if number not in ctx.state.get("verified_orders", []):
            return {"status": "needs_verification",
                    "instruction": "Verify the order first with get_order_status (order number + checkout email)."}
        ctx.signals["start_flow"] = ("return_request", {"order_number": number})
        return {"status": "flow_started", "instruction": "The returns flow will take over. Say nothing else."}

    async def escalate_to_human(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        ctx.signals["escalate"] = (str(args.get("reason") or "model_requested")[:64],
                                   args.get("urgency") if args.get("urgency") in ("normal", "high", "urgent") else "normal")
        return {"status": "escalated", "instruction": "A human teammate is being brought in."}

    order_param = {"type": "object", "properties": {
        "order_number": {"type": "string", "description": "The verified order number"}}, "required": ["order_number"]}
    return [
        Tool(ToolSpec("start_order_cancellation",
                      "Start the guided cancellation for a VERIFIED order when the customer wants to cancel it.",
                      order_param), start_order_cancellation),
        Tool(ToolSpec("start_return_request",
                      "Start the guided return/refund/exchange request for a VERIFIED order (damaged, wrong, unwanted items).",
                      order_param), start_return_request),
        Tool(ToolSpec("escalate_to_human",
                      "Hand the conversation to a human teammate. Use when you can't resolve the issue, the customer is "
                      "upset, the request needs judgement (exceptions, compensation, complaints), or policy requires it.",
                      {"type": "object", "properties": {
                          "reason": {"type": "string", "description": "short snake_case reason, e.g. complaint, exception_request"},
                          "urgency": {"type": "string", "enum": ["normal", "high", "urgent"]}},
                       "required": ["reason"]}), escalate_to_human),
    ]


FLOW_GUIDANCE = """Actions:
- To cancel an order or start a return, first verify the order with get_order_status, then call
  start_order_cancellation or start_return_request. These start guided steps; don't promise outcomes yourself.
- Call escalate_to_human when you can't resolve something, for complaints or exception requests (refunds outside
  policy, compensation), or when the customer asks for a person.

Privacy: personal details in this conversation appear as placeholders like <EMAIL_1> or <PHONE_1>. Use them
exactly as written (including in tool arguments). Never ask the customer to repeat a detail that already has a
placeholder."""
