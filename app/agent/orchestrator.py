"""The agent's turn loop (Part 1: basic, conversational replies).

    inbound message
        -> load/create conversation, store the user turn
        -> build system prompt (persona + scope + channel)
        -> call the LLM with recent history
        -> store and return the reply

Instagram comments take a separate path: one structured (JSON) LLM call decides on a
public reply and whether to move the conversation to a private DM.
"""

from __future__ import annotations

import json
import time

from app.agent.memory import ConversationStore
from app.agent.prompts import COMMENT_TRIAGE_INSTRUCTIONS, build_system_prompt
from app.agent.tenant import TenantConfig
from app.domain import AgentReply, InboundMessage, MessageKind
from app.llm.base import ChatMessage, LLMClient
from app.logging_config import log


class Agent:
    def __init__(self, llm: LLMClient, store: ConversationStore, tenant: TenantConfig, history_turns: int = 12):
        self.llm = llm
        self.store = store
        self.tenant = tenant
        self.history_turns = history_turns

    async def respond(self, msg: InboundMessage) -> AgentReply:
        started = time.perf_counter()
        conv = await self.store.get_or_create(msg, self.tenant.tenant_id)
        await self.store.append(conv.id, "user", msg.text, channel_message_id=msg.message_id)

        if msg.kind == MessageKind.COMMENT:
            reply = await self._respond_to_comment(msg)
        else:
            reply = await self._respond_to_dm(conv.id, msg)

        reply.meta["latency_ms"] = round((time.perf_counter() - started) * 1000)
        reply.meta["conversation_id"] = conv.id
        stored = reply.text if msg.kind == MessageKind.DIRECT else json.dumps(
            {"public": reply.text, "dm": reply.private_text})
        await self.store.append(conv.id, "assistant", stored, meta=reply.meta)
        log.info("agent.replied", channel=str(msg.channel), **reply.meta)
        return reply

    async def _respond_to_dm(self, conversation_id: str, msg: InboundMessage) -> AgentReply:
        system = build_system_prompt(self.tenant, msg.channel, msg.kind)
        history = await self.store.history(conversation_id, self.history_turns)
        resp = await self.llm.chat(system, history)
        return AgentReply(text=resp.text.strip(), meta={"model": resp.model, "in_tok": resp.input_tokens,
                                                         "out_tok": resp.output_tokens})

    async def _respond_to_comment(self, msg: InboundMessage) -> AgentReply:
        policy = self.tenant.comment_policy
        system = build_system_prompt(self.tenant, msg.channel, MessageKind.COMMENT, extra_sections=[
            COMMENT_TRIAGE_INSTRUCTIONS.format(topics=", ".join(policy.move_to_dm_topics))])
        user = ChatMessage(role="user", content=f"Comment from @{msg.user_name or 'customer'}: {msg.text}")
        resp = await self.llm.chat(system, [user], json_mode=True, temperature=0.2)
        decision = _parse_json(resp.text)

        public = str(decision.get("public_reply") or "").strip() if policy.reply_publicly else ""
        send_dm = bool(decision.get("send_dm"))
        # Belt and braces: even if the model forgets, sensitive keywords always move to DM.
        if any(t in msg.text.lower() for t in policy.move_to_dm_topics):
            send_dm = True
        dm = str(decision.get("dm_text") or "").strip()
        if send_dm and not dm:
            dm = (f"Hi{(' @' + msg.user_name) if msg.user_name else ''}! It's {self.tenant.persona.agent_name} "
                  f"from {self.tenant.brand.name}, following up on your comment. I'm happy to help here "
                  "privately. Could you share a bit more detail?")
        if send_dm and not public and policy.reply_publicly:
            public = "Thanks for reaching out! We've sent you a DM so we can help privately."
        if not public and not send_dm:
            public = "Thank you! 💛" if self.tenant.persona.emoji != "none" else "Thank you!"
        return AgentReply(text=public, private_text=dm if send_dm else None,
                          meta={"model": resp.model, "comment_to_dm": send_dm})


def _parse_json(text: str) -> dict:
    """Tolerant JSON parsing: models sometimes wrap JSON in prose or code fences."""
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        value = json.loads(text[start:end + 1])
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}
