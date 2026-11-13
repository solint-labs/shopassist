"""Hand-off to humans.

A good hand-off has four parts:
  1. Tell the customer what happens next, honestly: in hours vs out of hours.
  2. Give the human a summary so the customer doesn't have to repeat themselves.
  3. Put the ticket where the team already works (Zendesk, Freshdesk, Gorgias... or a webhook).
  4. Make the bot step back: conversation status = "human", and on Messenger/Instagram
     pass thread control to the Page/IG inbox via Meta's handover protocol so agents
     can reply natively. The bot stays silent until the conversation is released.

WhatsApp has no handover protocol. Human agents reply through our agent API
(`app/escalation/agent_api.py`), which a helpdesk integration or an internal console calls.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Protocol

import httpx

from app.agent.memory import ConversationStore
from app.agent.tenant import TenantConfig
from app.channels.gateway import ChannelGateway
from app.domain import AgentReply, Channel, InboundMessage
from app.escalation.detector import EscalationDecision, within_business_hours
from app.llm.base import ChatMessage, LLMClient
from app.logging_config import log
from app.storage.models import Handoff

# Meta's documented app ids for the native inboxes (handover protocol target_app_id).
PAGE_INBOX_APP_ID = "263902037430900"
IG_INBOX_APP_ID = "1217981644879628"


@dataclass
class TicketRequest:
    conversation_id: str
    tenant_id: str
    channel: str
    subject: str
    summary: str
    priority: str
    reason: str
    transcript: list[dict] = field(default_factory=list)  # [{role, text}], pseudonymised


class Helpdesk(Protocol):
    async def create_ticket(self, t: TicketRequest) -> str | None: ...
    async def add_note(self, ticket_id: str, text: str) -> None: ...


class NullHelpdesk:
    """No external helpdesk: tickets live only in our `handoffs` table and agent API."""

    async def create_ticket(self, t: TicketRequest) -> str | None:
        log.info("helpdesk.ticket", reason=t.reason, priority=t.priority, conversation_id=t.conversation_id)
        return None

    async def add_note(self, ticket_id: str, text: str) -> None:
        return None


class WebhookHelpdesk:
    """POST tickets as signed JSON to any URL (Zapier/Make/n8n, or your own service)."""

    def __init__(self, url: str, secret: str, http: httpx.AsyncClient | None = None):
        self.url, self.secret = url, secret
        self.http = http or httpx.AsyncClient(timeout=10)

    async def _post(self, body: dict) -> dict:
        raw = json.dumps(body).encode()
        sig = hmac.new(self.secret.encode(), raw, hashlib.sha256).hexdigest()
        r = await self.http.post(self.url, content=raw, headers={"content-type": "application/json",
                                                                  "x-shopassist-signature": f"sha256={sig}"})
        r.raise_for_status()
        return r.json() if r.content else {}

    async def create_ticket(self, t: TicketRequest) -> str | None:
        return str((await self._post({"event": "ticket.create", "ticket": asdict(t)})).get("id") or "") or None

    async def add_note(self, ticket_id: str, text: str) -> None:
        await self._post({"event": "ticket.note", "ticket_id": ticket_id, "text": text})


class ZendeskHelpdesk:
    """Zendesk Support API: tickets.json with API-token auth."""

    def __init__(self, subdomain: str, email: str, api_token: str, http: httpx.AsyncClient | None = None):
        self.base = f"https://{subdomain}.zendesk.com/api/v2"
        self.http = http or httpx.AsyncClient(timeout=10, auth=(f"{email}/token", api_token))

    async def create_ticket(self, t: TicketRequest) -> str | None:
        body = "\n\n".join([t.summary, "─── Transcript ───",
                            *[f"{m['role']}: {m['text']}" for m in t.transcript[-20:]]])
        r = await self.http.post(f"{self.base}/tickets.json", json={"ticket": {
            "subject": t.subject, "comment": {"body": body, "public": False},
            "priority": t.priority, "tags": ["shopassist", t.channel, t.reason],
            "external_id": t.conversation_id}})
        r.raise_for_status()
        return str(r.json()["ticket"]["id"])

    async def add_note(self, ticket_id: str, text: str) -> None:
        r = await self.http.put(f"{self.base}/tickets/{ticket_id}.json",
                                json={"ticket": {"comment": {"body": text, "public": False}}})
        r.raise_for_status()


SUMMARY_PROMPT = """Summarise this support conversation for a human agent taking over.
Return JSON: {"summary": "2-3 sentences: what the customer wants and what has been tried",
"order_numbers": [..], "sentiment": "calm|annoyed|angry|distressed"}.
Keep placeholders like <EMAIL_1> exactly as they are."""


class HandoffService:
    def __init__(self, store: ConversationStore, gateway: ChannelGateway, helpdesk: Helpdesk, llm: LLMClient,
                 tenant: TenantConfig, meta_app_id: str = "", meta_handover: bool = False):
        self.store, self.gateway, self.helpdesk, self.llm = store, gateway, helpdesk, llm
        self.tenant, self.meta_app_id, self.meta_handover = tenant, meta_app_id, meta_handover

    async def escalate(self, conversation_id: str, msg: InboundMessage, decision: EscalationDecision) -> AgentReply:
        transcript = [{"role": m.role, "text": m.text} for m in await self.store.transcript(conversation_id)]
        summary = await self._summarise(transcript)
        ticket_id = None
        try:
            ticket_id = await self.helpdesk.create_ticket(TicketRequest(
                conversation_id=conversation_id, tenant_id=self.tenant.tenant_id, channel=str(msg.channel),
                subject=f"[{decision.priority.upper()}] {decision.reason.replace('_', ' ')} via {msg.channel}",
                summary=summary, priority=decision.priority, reason=decision.reason, transcript=transcript))
        except Exception as e:  # noqa: BLE001 - never lose the hand-off because a helpdesk API failed
            log.exception("handoff.helpdesk_failed", error=str(e))

        async with self.store.db.session() as s:
            s.add(Handoff(conversation_id=conversation_id, reason=decision.reason, priority=decision.priority,
                          summary=summary, external_ticket_id=ticket_id))
        await self.store.set_status(conversation_id, "human")
        await self._pass_thread_control(msg, decision)

        esc = self.tenant.escalation
        text = esc.in_hours_message if within_business_hours(esc.business_hours) else esc.out_of_hours_message
        if decision.reason == "safety":
            text = ("I'm really sorry to hear that. If anyone is unwell or hurt, please get medical help right away. "
                    + text)
        log.info("handoff.created", reason=decision.reason, priority=decision.priority, ticket=ticket_id)
        return AgentReply(text=text.strip(), meta={"escalated": decision.reason, "priority": decision.priority,
                                                   "ticket": ticket_id})

    async def note_customer_message(self, conversation_id: str, text: str) -> None:
        """While a human owns the conversation, mirror new customer messages to the ticket."""
        async with self.store.db.session() as s:
            from sqlalchemy import select

            h = (await s.execute(select(Handoff).where(Handoff.conversation_id == conversation_id)
                                 .order_by(Handoff.created_at.desc()).limit(1))).scalar_one_or_none()
        if h and h.external_ticket_id:
            try:
                await self.helpdesk.add_note(h.external_ticket_id, f"Customer: {text}")
            except Exception as e:  # noqa: BLE001
                log.warning("handoff.note_failed", error=str(e))

    async def release(self, conversation_id: str) -> None:
        """Give the conversation back to the bot (agent clicked 'resolve' / thread control returned)."""
        await self.store.set_status(conversation_id, "bot")
        async with self.store.db.session() as s:
            from sqlalchemy import update

            await s.execute(update(Handoff).where(Handoff.conversation_id == conversation_id,
                                                  Handoff.resolved_at.is_(None))
                            .values(resolved_at=datetime.now(timezone.utc)))
        state = await self.store.get_state(conversation_id)
        state["failed_answers"] = 0
        await self.store.save_state(conversation_id, state)

    async def _summarise(self, transcript: list[dict]) -> str:
        convo = "\n".join(f"{m['role']}: {m['text']}" for m in transcript[-20:])
        try:
            resp = await self.llm.chat(SUMMARY_PROMPT, [ChatMessage(role="user", content=convo)], json_mode=True,
                                       temperature=0, max_tokens=250)
            data = json.loads(resp.text[resp.text.find("{"):resp.text.rfind("}") + 1] or "{}")
            if data.get("summary"):
                orders = ", ".join(data.get("order_numbers") or []) or "none mentioned"
                return f"{data['summary']}\nOrders: {orders}\nSentiment: {data.get('sentiment', 'unknown')}"
        except Exception as e:  # noqa: BLE001
            log.warning("handoff.summary_failed", error=str(e))
        last = [f"{m['role']}: {m['text']}" for m in transcript[-6:]]
        return "Recent messages:\n" + "\n".join(last)

    async def _pass_thread_control(self, msg: InboundMessage, decision: EscalationDecision) -> None:
        if not self.meta_handover or msg.channel not in (Channel.MESSENGER, Channel.INSTAGRAM):
            return
        target = PAGE_INBOX_APP_ID if msg.channel == Channel.MESSENGER else IG_INBOX_APP_ID
        try:
            await self.gateway.pass_thread_control(msg, target, f"escalated:{decision.reason}")
        except Exception as e:  # noqa: BLE001 - status=human already silences the bot
            log.warning("handoff.pass_thread_control_failed", error=str(e))
