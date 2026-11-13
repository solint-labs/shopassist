"""The agent's turn loop.

Part 1: persona prompt + history -> LLM -> reply.
Part 2 adds grounding and live data:

    inbound message
        -> store user turn, load history + conversation state
        -> condense follow-ups into a standalone query
        -> hybrid retrieval + rerank + threshold        (knowledge base)
        -> LLM with sources + tools
             ↺ tool calls (Shopify orders / products), max N rounds
        -> validate citations, strip them, add source link
        -> store assistant turn + state

Instagram comments use a structured triage call, now also grounded in the knowledge
base so product questions get correct public answers.
"""

from __future__ import annotations

import json
import time

from app.agent.memory import ConversationStore
from app.agent.prompts import COMMENT_TRIAGE_INSTRUCTIONS, build_system_prompt
from app.agent.tenant import TenantConfig
from app.commerce.tools import TOOLS_GUIDANCE, ToolContext, ToolRegistry
from app.domain import AgentReply, InboundMessage, MessageKind
from app.llm.base import ChatMessage, LLMClient
from app.logging_config import log
from app.rag.grounding import finalize_answer, knowledge_section
from app.rag.retriever import Retrieval, Retriever


class Agent:
    def __init__(self, llm: LLMClient, store: ConversationStore, tenant: TenantConfig, history_turns: int = 12,
                 retriever: Retriever | None = None, tools: ToolRegistry | None = None,
                 max_tool_rounds: int = 4, append_source_link: bool = True):
        self.llm = llm
        self.store = store
        self.tenant = tenant
        self.history_turns = history_turns
        self.retriever = retriever
        self.tools = tools or ToolRegistry()
        self.max_tool_rounds = max_tool_rounds
        self.append_source_link = append_source_link

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

    # ── Retrieval ────────────────────────────────────────────────────────────

    async def _retrieve(self, history: list[ChatMessage]) -> Retrieval:
        if not self.retriever:
            return Retrieval(query="")
        query = await self.retriever.standalone_query(history)
        return await self.retriever.retrieve(self.tenant.tenant_id, query)

    # ── Direct messages ──────────────────────────────────────────────────────

    async def _respond_to_dm(self, conversation_id: str, msg: InboundMessage) -> AgentReply:
        history = await self.store.history(conversation_id, self.history_turns)
        state = await self.store.get_state(conversation_id)
        retrieval = await self._retrieve(history)

        sections = []
        if self.retriever:
            sections.append(knowledge_section(retrieval))
        if self.tools:
            sections.append(TOOLS_GUIDANCE)
        system = build_system_prompt(self.tenant, msg.channel, msg.kind, extra_sections=sections)

        text, tools_used, usage = await self._run_with_tools(system, history, ToolContext(msg, conversation_id, state))
        await self.store.save_state(conversation_id, state)

        answer = finalize_answer(text, retrieval, self.append_source_link, used_tools=bool(tools_used))
        return AgentReply(text=answer.text, meta={
            **usage, "tools": tools_used, "kb_hits": len(retrieval.hits), "kb_top": round(retrieval.top_score, 3),
            "cited": answer.cited, "grounded": answer.grounded, "query": retrieval.query,
        })

    async def _run_with_tools(self, system: str, history: list[ChatMessage], ctx: ToolContext
                              ) -> tuple[str, list[str], dict]:
        """Call the LLM, execute any tool calls, feed results back; repeat until it answers."""
        messages = list(history)
        specs = self.tools.specs() if self.tools else None
        used: list[str] = []
        usage = {"model": "", "in_tok": 0, "out_tok": 0}
        for _ in range(self.max_tool_rounds + 1):
            resp = await self.llm.chat(system, messages, tools=specs)
            usage["model"] = resp.model
            usage["in_tok"] += resp.input_tokens
            usage["out_tok"] += resp.output_tokens
            if not resp.tool_calls:
                return resp.text.strip(), used, usage
            messages.append(ChatMessage(role="assistant", content=resp.text, tool_calls=resp.tool_calls))
            for call in resp.tool_calls:
                result = await self.tools.execute(call, ctx)
                used.append(call.name)
                messages.append(ChatMessage.tool_result(call, result))
        # The model kept calling tools: stop and ask it to answer with what it has.
        resp = await self.llm.chat(system + "\n\nAnswer the customer now without calling tools.", messages)
        return resp.text.strip(), used, usage

    # ── Instagram comments ───────────────────────────────────────────────────

    async def _respond_to_comment(self, msg: InboundMessage) -> AgentReply:
        policy = self.tenant.comment_policy
        retrieval = await self._retrieve([ChatMessage(role="user", content=msg.text)])
        sections = [knowledge_section(retrieval)] if self.retriever else []
        sections.append(COMMENT_TRIAGE_INSTRUCTIONS.format(topics=", ".join(policy.move_to_dm_topics)))
        system = build_system_prompt(self.tenant, msg.channel, MessageKind.COMMENT, extra_sections=sections)
        user = ChatMessage(role="user", content=f"Comment from @{msg.user_name or 'customer'}: {msg.text}")
        resp = await self.llm.chat(system, [user], json_mode=True, temperature=0.2)
        decision = _parse_json(resp.text)

        public = str(decision.get("public_reply") or "").strip() if policy.reply_publicly else ""
        public = finalize_answer(public, retrieval, append_link=False).text if public else ""
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
                          meta={"model": resp.model, "comment_to_dm": send_dm, "kb_hits": len(retrieval.hits)})


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
