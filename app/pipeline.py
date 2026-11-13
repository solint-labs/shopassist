"""Message pipeline: everything that happens to one inbound message.

    dedupe -> acknowledge (seen/typing) -> agent.respond -> gateway.deliver

Kept separate from the HTTP layer so the same pipeline can run inside a FastAPI
background task (Part 1) or a queue worker (Part 4) without changes.
"""

from __future__ import annotations

import time
from collections import OrderedDict

from app.agent.orchestrator import Agent
from app.channels.gateway import ChannelGateway
from app.domain import AgentReply, InboundMessage, MessageKind
from app.logging_config import log


class RecentIds:
    """Tiny in-process TTL set for webhook de-duplication.

    Meta retries webhooks that don't get a fast 200, so the same message can arrive
    more than once. This is good enough for a single instance; Part 4 replaces it with
    Redis so de-duplication works across many workers.
    """

    def __init__(self, ttl_s: float = 3600, max_items: int = 50_000):
        self.ttl_s, self.max_items = ttl_s, max_items
        self._seen: OrderedDict[str, float] = OrderedDict()

    def add_if_new(self, key: str) -> bool:
        now = time.monotonic()
        while self._seen and (now - next(iter(self._seen.values())) > self.ttl_s or len(self._seen) > self.max_items):
            self._seen.popitem(last=False)
        if key in self._seen:
            return False
        self._seen[key] = now
        return True


FALLBACK_TEXT = ("Sorry, I'm having a little trouble right now. Please try again in a moment, "
                 "or email us and the team will help.")


class MessagePipeline:
    def __init__(self, agent: Agent, gateway: ChannelGateway, dedupe: RecentIds | None = None):
        self.agent = agent
        self.gateway = gateway
        self.dedupe = dedupe or RecentIds()

    async def process(self, msg: InboundMessage) -> AgentReply | None:
        if not self.dedupe.add_if_new(f"{msg.channel}:{msg.message_id}"):
            log.info("pipeline.duplicate", message_id=msg.message_id)
            return None
        if await self.agent.is_bot_handling(msg):
            await self.gateway.acknowledge(msg)  # no typing bubble while a human owns the thread
        try:
            reply = await self.agent.respond(msg)
        except Exception as e:  # noqa: BLE001 - the customer must always get *something*
            log.exception("pipeline.agent_failed", error=str(e), channel=msg.channel)
            if msg.kind == MessageKind.COMMENT:
                return None  # never post an error message publicly
            reply = AgentReply(text=f"{FALLBACK_TEXT} ({self.agent.tenant.brand.support_email})",
                               meta={"fallback": True})
        if reply is not None:  # None = a human owns this conversation; stay silent
            await self.gateway.deliver(msg, reply)
        return reply
