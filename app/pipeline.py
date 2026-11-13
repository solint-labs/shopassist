"""Message pipeline: everything that happens to one inbound message.

    resolve tenant -> dedupe -> per-conversation lock -> ack (seen/typing)
        -> agent.respond -> gateway.deliver (with retries) -> metrics

Kept separate from transport, so the same pipeline runs inside a FastAPI background
task (dev, `QUEUE_MODE=inline`) or a Redis Streams worker (prod, `QUEUE_MODE=redis`).
"""

from __future__ import annotations

import time

from app.channels.gateway import ChannelGateway
from app.channels.graph_client import GraphAPIError
from app.domain import AgentReply, InboundMessage, MessageKind
from app.logging_config import log
from app.observability import metrics
from app.reliability.dedupe import ConversationLocks, Dedupe, MemoryDedupe
from app.reliability.resilience import retry_async

FALLBACK_TEXT = ("Sorry, I'm having a little trouble right now. Please try again in a moment, "
                 "or email us and the team will help.")


class MessagePipeline:
    def __init__(self, registry, gateway: ChannelGateway, dedupe: Dedupe | None = None,
                 locks: ConversationLocks | None = None):
        # `registry` maps an inbound message to its tenant's Agent (see app/tenancy). A
        # plain Agent is accepted too, for single-store setups and tests.
        self.registry = registry
        self.gateway = gateway
        self.dedupe = dedupe or MemoryDedupe()
        self.locks = locks or ConversationLocks(None)

    def _agent_for(self, msg: InboundMessage):
        return self.registry.agent_for(msg) if hasattr(self.registry, "agent_for") else self.registry

    async def process(self, msg: InboundMessage) -> AgentReply | None:
        key = f"{msg.channel}:{msg.message_id}"
        if not await self.dedupe.claim(key):
            metrics.DUPLICATES.inc()
            log.info("pipeline.duplicate", message_id=msg.message_id)
            return None
        try:
            async with self.locks.hold(msg.conversation_key):
                return await self._process(msg)
        except Exception:
            if hasattr(self.dedupe, "release"):  # let the queue's redelivery try again
                await self.dedupe.release(key)
            raise

    async def _process(self, msg: InboundMessage) -> AgentReply | None:
        agent = self._agent_for(msg)
        tenant = agent.tenant.tenant_id
        metrics.MESSAGES.labels(tenant, str(msg.channel), str(msg.kind)).inc()
        started = time.perf_counter()
        if await agent.is_bot_handling(msg):
            await self.gateway.acknowledge(msg)  # no typing bubble while a human owns the thread
        try:
            reply = await agent.respond(msg)
        except Exception as e:  # noqa: BLE001 - the customer must always get *something*
            log.exception("pipeline.agent_failed", error=str(e), channel=msg.channel)
            if msg.kind == MessageKind.COMMENT:
                return None  # never post an error message publicly
            reply = AgentReply(text=f"{FALLBACK_TEXT} ({agent.tenant.brand.support_email})", meta={"fallback": True})

        outcome = _outcome(reply)
        metrics.REPLIES.labels(tenant, str(msg.channel), outcome).inc()
        if reply is not None:
            if reply.meta.get("escalated"):
                metrics.ESCALATIONS.labels(tenant, reply.meta["escalated"]).inc()
            if "grounded" in reply.meta:
                metrics.GROUNDED.labels(tenant, str(reply.meta["grounded"]).lower()).inc()
            for tool in reply.meta.get("tools", []):
                metrics.TOOL_CALLS.labels(tool).inc()
            # Retry transient Graph API failures (throttling, 5xx) without re-running the LLM.
            await retry_async(lambda: self.gateway.deliver(msg, reply), attempts=3,
                              retry_if=lambda e: isinstance(e, GraphAPIError) and e.retryable)
        metrics.TURN_LATENCY.labels(str(msg.channel)).observe(time.perf_counter() - started)
        return reply


def _outcome(reply: AgentReply | None) -> str:
    if reply is None:
        return "silent"
    if reply.meta.get("fallback"):
        return "fallback"
    if reply.meta.get("escalated"):
        return "escalated"
    if reply.meta.get("action") or reply.quick_replies:
        return "flow"
    return "answered"
