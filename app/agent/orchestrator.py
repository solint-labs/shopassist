"""The agent's turn loop.

Part 1: persona prompt + history -> LLM -> reply.
Part 2: + hybrid RAG with citations, + Shopify tools.
Part 3: + privacy, escalation and deterministic flows. A turn now looks like this:

    inbound message
      1. pseudonymise PII (vault) ─────────────── stored + sent to the LLM as <EMAIL_1>
      2. human owns the conversation? ─────────── record it, mirror it to the ticket, stay silent
      3. escalation rules (safety, legal, human request, frustration, repeated failure)
      4. "forget me" request? ─────────────────── erasure flow
      5. active flow? ─────────────────────────── the flow handles the message (cancel / return)
      6. RAG + LLM ⇄ tools ─────────────────────── may signal: start a flow / escalate
      7. track unresolved answers (feeds rule 3 next turn)
      8. store pseudonymised reply; rehydrate placeholders only for delivery to this customer

Instagram comments: moderation (hide spam/abuse, hide comments that expose PII), then
the grounded public-reply / move-to-DM triage from Parts 1-2.
"""

from __future__ import annotations

import json
import re
import time

from app.agent.memory import ConversationStore
from app.agent.prompts import COMMENT_TRIAGE_INSTRUCTIONS, build_system_prompt
from app.agent.tenant import TenantConfig
from app.commerce.shopify import ShopifyClient
from app.commerce.tools import TOOLS_GUIDANCE, ToolContext, ToolRegistry
from app.domain import AgentReply, InboundMessage, MessageKind
from app.escalation.detector import PROFANITY, EscalationDecision, EscalationDetector
from app.escalation.handoff import HandoffService
from app.flows.engine import FlowContext, FlowEngine
from app.flows.tools import FLOW_GUIDANCE
from app.llm.base import ChatMessage, LLMClient
from app.logging_config import log
from app.privacy.vault import TOKEN_RE, PIIVault, RedactionResult
from app.rag.grounding import finalize_answer, knowledge_section
from app.rag.retriever import Retrieval, Retriever

FORGET_RE = re.compile(r"(?i)\bforget me\b|\b(delete|erase|remove|wipe)\b.{0,25}\b(my|me)\b.{0,25}"
                       r"\b(data|details|info(rmation)?|conversation|chat|messages?)\b")
UNCERTAIN_RE = re.compile(r"(?i)\b(not sure|don'?t know|can'?t find|couldn'?t find|no information|unable to)\b")
BLOCKED_NOTE = ("For your security, please don't share card numbers, ID numbers or one-time codes in chat. "
                "I've removed it from our records.")


class Agent:
    def __init__(self, llm: LLMClient, store: ConversationStore, tenant: TenantConfig, history_turns: int = 12,
                 retriever: Retriever | None = None, tools: ToolRegistry | None = None,
                 max_tool_rounds: int = 4, append_source_link: bool = True,
                 vault: PIIVault | None = None, handoff: HandoffService | None = None,
                 flows: FlowEngine | None = None, shopify: ShopifyClient | None = None):
        self.llm = llm
        self.store = store
        self.tenant = tenant
        self.history_turns = history_turns
        self.retriever = retriever
        self.tools = tools or ToolRegistry()
        self.max_tool_rounds = max_tool_rounds
        self.append_source_link = append_source_link
        self.vault = vault
        self.handoff = handoff
        self.flows = flows or FlowEngine([])
        self.shopify = shopify
        self.detector = EscalationDetector(tenant.escalation)

    async def is_bot_handling(self, msg: InboundMessage) -> bool:
        conv = await self.store.find_by_key(msg.conversation_key)
        return not msg.standby and (conv is None or conv.status == "bot")

    async def respond(self, msg: InboundMessage) -> AgentReply | None:
        """Return the reply to deliver, or None when the bot must stay silent."""
        started = time.perf_counter()
        conv = await self.store.get_or_create(msg, self.tenant.tenant_id)
        red = await self._pseudonymize(conv.id, msg.text)
        await self.store.append(conv.id, "user", red.text, channel_message_id=msg.message_id,
                                meta={"pii": sorted(red.kinds)} if red.kinds else None)

        if conv.status == "human" or msg.standby:
            if self.handoff:
                await self.handoff.note_customer_message(conv.id, red.text)
            log.info("agent.silent_human_owned", conversation_id=conv.id)
            return None

        if msg.kind == MessageKind.COMMENT:
            reply = await self._respond_to_comment(msg, red)
        else:
            reply = await self._respond_to_dm(conv.id, msg, red)
            if red.blocked:
                reply.text = f"{BLOCKED_NOTE}\n\n{reply.text}"

        reply.meta["latency_ms"] = round((time.perf_counter() - started) * 1000)
        reply.meta["conversation_id"] = conv.id
        if not reply.meta.get("forgotten"):
            stored = reply.text if msg.kind == MessageKind.DIRECT else json.dumps(
                {"public": reply.text, "dm": reply.private_text, "actions": reply.actions})
            await self.store.append(conv.id, "assistant", stored, meta=reply.meta)
        log.info("agent.replied", channel=str(msg.channel), **{k: v for k, v in reply.meta.items() if k != "query"})

        # Placeholders become real values only on the way out, and only for this customer.
        if msg.kind == MessageKind.DIRECT and self.vault and not reply.meta.get("forgotten"):
            reply.text = await self.vault.rehydrate(conv.id, reply.text)
        else:  # public comment replies never contain personal data
            reply.text = TOKEN_RE.sub("", reply.text)
        if reply.private_text and self.vault:
            reply.private_text = await self.vault.rehydrate(conv.id, reply.private_text)
        return reply

    # ── Privacy ──────────────────────────────────────────────────────────────

    async def _pseudonymize(self, conversation_id: str, text: str) -> RedactionResult:
        if not self.vault:
            return RedactionResult(text)
        return await self.vault.pseudonymize(conversation_id, text)

    # ── Retrieval ────────────────────────────────────────────────────────────

    async def _retrieve(self, history: list[ChatMessage]) -> Retrieval:
        if not self.retriever:
            return Retrieval(query="")
        query = await self.retriever.standalone_query(history)
        return await self.retriever.retrieve(self.tenant.tenant_id, TOKEN_RE.sub("", query))

    # ── Direct messages ──────────────────────────────────────────────────────

    def _flow_ctx(self, conversation_id: str, msg: InboundMessage, state: dict) -> FlowContext:
        async def escalate(decision: EscalationDecision) -> AgentReply:
            return await self._escalate(conversation_id, msg, decision)

        async def forget() -> None:
            await self.store.delete(conversation_id)

        return FlowContext(msg, conversation_id, state, self.tenant, self.shopify, escalate, forget)

    async def _escalate(self, conversation_id: str, msg: InboundMessage, decision: EscalationDecision) -> AgentReply:
        if self.handoff is None:  # no hand-off configured: at least tell the customer where to go
            return AgentReply(f"I'd like a teammate to help with this. Please email {self.tenant.brand.support_email}.",
                              meta={"escalated": decision.reason, "priority": decision.priority})
        return await self.handoff.escalate(conversation_id, msg, decision)

    async def _respond_to_dm(self, conversation_id: str, msg: InboundMessage, red: RedactionResult) -> AgentReply:
        history = await self.store.history(conversation_id, self.history_turns)
        state = await self.store.get_state(conversation_id)
        previous_user = [m.content for m in history[:-1] if m.role == "user"]
        ctx = self._flow_ctx(conversation_id, msg, state)

        # 3. deterministic escalation rules
        decision = self.detector.evaluate(red.text, previous_user, state)
        if decision:
            state.pop("flow", None)
            reply = await self._escalate(conversation_id, msg, decision)
            state["failed_answers"] = 0
            await self.store.save_state(conversation_id, state)
            return reply

        # 4-5. erasure requests and active flows bypass the LLM
        if FORGET_RE.search(red.text) and "forget_me" in self.flows.flows:
            reply = await self.flows.start("forget_me", ctx, {})
            await self.store.save_state(conversation_id, state)
            return reply
        if self.flows.active(state):
            reply = await self.flows.handle(ctx)
            if reply is not None:
                if not reply.meta.get("forgotten"):
                    await self.store.save_state(conversation_id, state)
                return reply

        # 6. RAG + LLM + tools
        retrieval = await self._retrieve(history)
        sections = []
        if self.retriever:
            sections.append(knowledge_section(retrieval))
        if self.tools:
            sections += [TOOLS_GUIDANCE, FLOW_GUIDANCE]
        system = build_system_prompt(self.tenant, msg.channel, msg.kind, extra_sections=sections)
        tctx = ToolContext(msg, conversation_id, state)
        text, tools_used, usage = await self._run_with_tools(system, history, tctx)

        if "escalate" in tctx.signals:
            reason, urgency = tctx.signals["escalate"]
            reply = await self._escalate(conversation_id, msg, EscalationDecision(reason, urgency, "requested by model"))
        elif "start_flow" in tctx.signals:
            name, args = tctx.signals["start_flow"]
            reply = await self.flows.start(name, ctx, args)
        else:
            answer = finalize_answer(text, retrieval, self.append_source_link, used_tools=bool(tools_used))
            reply = AgentReply(text=answer.text, meta={
                **usage, "tools": tools_used, "kb_hits": len(retrieval.hits), "kb_top": round(retrieval.top_score, 3),
                "cited": answer.cited, "grounded": answer.grounded, "query": retrieval.query})
            # 7. unresolved-answer tracking: two in a row triggers "repeated_failure" next turn
            unresolved = (not answer.grounded) or (not retrieval.found and not tools_used and UNCERTAIN_RE.search(text))
            state["failed_answers"] = state.get("failed_answers", 0) + 1 if unresolved else 0
        await self.store.save_state(conversation_id, state)
        return reply

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
                if self.vault:  # the model only knows <EMAIL_1>; Shopify needs the real value
                    call.arguments = await self.vault.rehydrate_args(ctx.conversation_id, call.arguments)
                result = await self.tools.execute(call, ctx)
                used.append(call.name)
                content = json.dumps(result, default=str)
                if self.vault:  # and tool results go back to the model pseudonymised
                    content = (await self.vault.pseudonymize(ctx.conversation_id, content)).text
                messages.append(ChatMessage(role="tool", content=content, tool_call_id=call.id, name=call.name))
            if ctx.signals:  # a flow or hand-off takes over; no need for another LLM round
                return "", used, usage
        # The model kept calling tools: stop and ask it to answer with what it has.
        resp = await self.llm.chat(system + "\n\nAnswer the customer now without calling tools.", messages)
        return resp.text.strip(), used, usage

    # ── Instagram comments ───────────────────────────────────────────────────

    async def _respond_to_comment(self, msg: InboundMessage, red: RedactionResult) -> AgentReply:
        policy = self.tenant.comment_policy
        # Moderation 1: the customer posted personal data publicly -> hide it, move to DM.
        if red.kinds:
            return AgentReply(text="", actions=["hide_comment"], private_text=(
                f"Hi{(' @' + msg.user_name) if msg.user_name else ''}! I've hidden your comment because it included "
                "personal details. Your privacy matters to us. I'm happy to help here instead. What do you need?"),
                meta={"moderation": "pii_hidden", "comment_to_dm": True})

        retrieval = await self._retrieve([ChatMessage(role="user", content=red.text)])
        sections = [knowledge_section(retrieval)] if self.retriever else []
        sections.append(COMMENT_TRIAGE_INSTRUCTIONS.format(topics=", ".join(policy.move_to_dm_topics)))
        system = build_system_prompt(self.tenant, msg.channel, MessageKind.COMMENT, extra_sections=sections)
        user = ChatMessage(role="user", content=f"Comment from @{msg.user_name or 'customer'}: {red.text}")
        resp = await self.llm.chat(system, [user], json_mode=True, temperature=0.2)
        decision = _parse_json(resp.text)

        # Moderation 2: spam / abuse -> hide quietly, never argue in public.
        lowered = red.text.lower()
        if decision.get("moderation") in ("spam", "abusive") or any(
                re.search(rf"(?<!\w){p}(?!\w)", lowered) for p in PROFANITY):
            return AgentReply(text="", actions=["hide_comment"],
                              meta={"moderation": decision.get("moderation") or "abusive"})

        public = str(decision.get("public_reply") or "").strip() if policy.reply_publicly else ""
        public = finalize_answer(public, retrieval, append_link=False).text if public else ""
        send_dm = bool(decision.get("send_dm"))
        # Belt and braces: even if the model forgets, sensitive keywords always move to DM.
        if any(t in lowered for t in policy.move_to_dm_topics):
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
