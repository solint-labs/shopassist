"""A tiny flow engine for transactional conversations.

Why not let the LLM do it? Because cancelling an order or filing a return is a
*commitment*: it must check policy exactly, get explicit confirmation, and call the
right API exactly once. LLMs are great at understanding intent ("I don't want it
anymore" -> cancellation) and terrible at being an auditable state machine.

So the split is:
  * the LLM *recognises* intent and starts a flow via a tool call;
  * the flow (plain Python) owns the steps, the policy checks, the confirmation and
    the API call;
  * while a flow is active, customer messages go to the flow first. The customer can
    leave at any time ("never mind"), and anything a flow can't parse is re-asked
    with buttons.

Flow state lives in the conversation state (`state["flow"]`), so flows survive
restarts and work across multiple workers.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.agent.tenant import TenantConfig
from app.commerce.shopify import ShopifyClient
from app.domain import AgentReply, InboundMessage, QuickReply
from app.escalation.detector import EscalationDecision

EXIT_RE = re.compile(r"(?i)\b(never ?mind|forget it|stop|exit|quit|not now|leave it|nothing)\b")
YES_RE = re.compile(r"(?i)^\s*(yes|yeah|yep|yup|sure|ok(ay)?|confirm|go ahead|do it|please do|haan?|ha)\b")
NO_RE = re.compile(r"(?i)^\s*(no|nope|nah|don'?t|keep( it)?|nahi|not really)\b")

YES_NO = [QuickReply("yes", "Yes, go ahead"), QuickReply("no", "No, keep it")]


@dataclass
class FlowContext:
    msg: InboundMessage
    conversation_id: str
    state: dict[str, Any]
    tenant: TenantConfig
    shopify: ShopifyClient | None
    escalate: Callable[[EscalationDecision], Awaitable[AgentReply]]
    forget: Callable[[], Awaitable[None]]

    @property
    def flow(self) -> dict[str, Any]:
        return self.state.setdefault("flow", {})


def yes_no(msg: InboundMessage) -> bool | None:
    if msg.payload in ("yes", "no"):
        return msg.payload == "yes"
    if YES_RE.search(msg.text):
        return True
    if NO_RE.search(msg.text):
        return False
    return None


class Flow(ABC):
    name: str = ""

    @abstractmethod
    async def start(self, ctx: FlowContext, args: dict[str, Any]) -> AgentReply: ...

    @abstractmethod
    async def handle(self, ctx: FlowContext) -> AgentReply: ...

    # helpers
    def goto(self, ctx: FlowContext, step: str, **data: Any) -> None:
        ctx.state["flow"] = {"name": self.name, "step": step, "data": {**ctx.flow.get("data", {}), **data},
                             "retries": 0}

    @staticmethod
    def finish(ctx: FlowContext) -> None:
        ctx.state.pop("flow", None)

    def reask(self, ctx: FlowContext, text: str, quick: list[QuickReply]) -> AgentReply:
        """Unclear answer: re-ask with buttons, give up after two tries."""
        ctx.flow["retries"] = ctx.flow.get("retries", 0) + 1
        if ctx.flow["retries"] > 2:
            self.finish(ctx)
            return AgentReply("No worries, let's leave that for now. Is there anything else I can help with?")
        return AgentReply(text, quick_replies=quick)


class FlowEngine:
    def __init__(self, flows: list[Flow]):
        self.flows = {f.name: f for f in flows}

    @staticmethod
    def active(state: dict) -> str | None:
        return (state.get("flow") or {}).get("name")

    async def start(self, name: str, ctx: FlowContext, args: dict[str, Any]) -> AgentReply:
        ctx.state["flow"] = {"name": name, "step": "start", "data": {}, "retries": 0}
        return await self.flows[name].start(ctx, args)

    async def handle(self, ctx: FlowContext) -> AgentReply | None:
        name = self.active(ctx.state)
        if not name or name not in self.flows:
            return None
        if EXIT_RE.search(ctx.msg.text) and ctx.msg.payload is None:
            Flow.finish(ctx)
            return AgentReply("No problem, I've stopped that. Anything else I can help with?")
        return await self.flows[name].handle(ctx)
