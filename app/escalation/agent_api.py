"""Human-agent API: the minimal surface a helpdesk integration or internal console needs.

    GET  /agent/conversations?status=human         queue of handed-off conversations
    GET  /agent/conversations/{id}?reveal_pii=true transcript (PII revealed only with permission, audited)
    POST /agent/conversations/{id}/messages         reply to the customer as a human
    POST /agent/conversations/{id}/release          hand back to the bot

Auth (Part 4): Bearer API keys with roles and tenant scope (see app/enterprise/rbac.py);
Part 3's shared X-Agent-Key still works and maps to the `agent` role. Every PII reveal,
reply and release is written to the audit log.

Messaging windows: WhatsApp only allows free-form replies within 24h of the customer's
last message. Messenger/Instagram allow human agents up to 7 days with the
HUMAN_AGENT tag. Outside those windows we return 409 so the console can offer a
template message instead.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.domain import Channel, InboundMessage, MessageKind
from app.enterprise.rbac import Principal, require

router = APIRouter(prefix="/agent", tags=["human-agent"])


class AgentMessage(BaseModel):
    text: str
    agent_name: str | None = None


def _target(conv) -> InboundMessage:
    return InboundMessage(channel=Channel(conv.channel), kind=MessageKind.DIRECT, message_id="",
                          user_id=conv.user_id, business_id=conv.business_id, text="")


async def _load(request: Request, conversation_id: str, who: Principal):
    """Fetch a conversation and the agent of its tenant, enforcing tenant scope."""
    registry = request.app.state.registry
    conv = await registry.default.agent.store.get(conversation_id)
    if conv is None or not who.can_access_tenant(conv.tenant_id):
        raise HTTPException(404)  # 404, not 403: don't confirm it exists in another tenant
    return conv, registry.for_tenant(conv.tenant_id).agent


@router.get("/conversations")
async def list_conversations(request: Request, status: str = "human",
                             who: Principal = Depends(require("conversations:read"))) -> list[dict]:
    store = request.app.state.registry.default.agent.store
    convs = await store.list_by_status(status, tenant_id=who.tenant_id)
    return [{"id": c.id, "tenant": c.tenant_id, "channel": c.channel, "status": c.status, "user_name": c.user_name,
             "updated_at": c.updated_at, "last_inbound_at": c.last_inbound_at} for c in convs]


@router.get("/conversations/{conversation_id}")
async def get_conversation(request: Request, conversation_id: str, reveal_pii: bool = True,
                           who: Principal = Depends(require("conversations:read"))) -> dict:
    conv, agent = await _load(request, conversation_id, who)
    reveal = reveal_pii and who.can("conversations:reveal_pii") and agent.vault is not None
    msgs = await agent.store.transcript(conversation_id)
    out = []
    for m in msgs:
        text = await agent.vault.rehydrate(conversation_id, m.text) if reveal else m.text
        out.append({"role": m.role, "text": text, "at": m.created_at})
    if reveal:
        await request.app.state.auditor.record(who, "pii.reveal", f"conversation:{conversation_id}", conv.tenant_id)
    return {"id": conv.id, "channel": conv.channel, "status": conv.status, "pii_revealed": reveal, "messages": out}


@router.post("/conversations/{conversation_id}/messages")
async def send_message(request: Request, conversation_id: str, body: AgentMessage,
                       who: Principal = Depends(require("conversations:reply"))) -> dict:
    conv, agent = await _load(request, conversation_id, who)
    gateway = request.app.state.gateway
    if conv.kind != MessageKind.DIRECT:
        raise HTTPException(400, "reply to comments from the Instagram app")
    last = conv.last_inbound_at
    if last and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - last if last else timedelta(0)
    if age > timedelta(days=7) or (conv.channel == Channel.WHATSAPP and age > timedelta(hours=24)):
        raise HTTPException(409, "outside the messaging window; use an approved template message")
    use_tag = conv.channel != Channel.WHATSAPP and age > timedelta(hours=24)
    await gateway.send_text(_target(conv), body.text, human_agent_tag=use_tag)
    # Store pseudonymised, like every other turn.
    stored = (await agent.vault.pseudonymize(conversation_id, body.text)).text if agent.vault else body.text
    await agent.store.append(conversation_id, "human_agent", stored, meta={"agent": body.agent_name or who.name})
    if conv.status != "human":
        await agent.store.set_status(conversation_id, "human")  # an agent jumping in takes ownership
    await request.app.state.auditor.record(who, "conversation.reply", f"conversation:{conversation_id}", conv.tenant_id)
    return {"sent": True}


@router.post("/conversations/{conversation_id}/release")
async def release(request: Request, conversation_id: str,
                  who: Principal = Depends(require("conversations:release"))) -> dict:
    conv, agent = await _load(request, conversation_id, who)
    gateway = request.app.state.gateway
    if agent.handoff:
        await agent.handoff.release(conversation_id)
    else:
        await agent.store.set_status(conversation_id, "bot")
    settings = request.app.state.settings
    if settings.meta_handover and conv.channel in (Channel.MESSENGER, Channel.INSTAGRAM):
        try:
            await gateway.take_thread_control(_target(conv), "released by agent")
        except Exception:  # noqa: BLE001 - status already says bot
            pass
    await request.app.state.auditor.record(who, "conversation.release", f"conversation:{conversation_id}",
                                           conv.tenant_id)
    return {"status": "bot"}
