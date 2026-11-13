"""Human-agent API: the minimal surface a helpdesk integration or internal console needs.

    GET  /agent/conversations?status=human         queue of handed-off conversations
    GET  /agent/conversations/{id}                  transcript (PII revealed for the agent)
    POST /agent/conversations/{id}/messages         reply to the customer as a human
    POST /agent/conversations/{id}/release          hand back to the bot

Auth here is a shared key (X-Agent-Key). Part 4 replaces it with per-user API keys,
roles (agent/admin/auditor) and an audit log entry for every PII reveal.

Messaging windows: WhatsApp only allows free-form replies within 24h of the customer's
last message. Messenger/Instagram allow human agents up to 7 days with the
HUMAN_AGENT tag. Outside those windows we return 409 so the console can offer a
template message instead.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from app.domain import Channel, InboundMessage, MessageKind

router = APIRouter(prefix="/agent", tags=["human-agent"])


def require_agent_key(request: Request, x_agent_key: str = Header(default="")) -> None:
    expected = request.app.state.settings.agent_api_key.get_secret_value()
    if not expected or x_agent_key != expected:
        raise HTTPException(status_code=401, detail="invalid agent key")


class AgentMessage(BaseModel):
    text: str
    agent_name: str | None = None


def _target(conv) -> InboundMessage:
    return InboundMessage(channel=Channel(conv.channel), kind=MessageKind.DIRECT, message_id="",
                          user_id=conv.user_id, business_id=conv.business_id, text="")


@router.get("/conversations", dependencies=[Depends(require_agent_key)])
async def list_conversations(request: Request, status: str = "human") -> list[dict]:
    convs = await request.app.state.agent.store.list_by_status(status)
    return [{"id": c.id, "channel": c.channel, "status": c.status, "user_name": c.user_name,
             "updated_at": c.updated_at, "last_inbound_at": c.last_inbound_at} for c in convs]


@router.get("/conversations/{conversation_id}", dependencies=[Depends(require_agent_key)])
async def get_conversation(request: Request, conversation_id: str) -> dict:
    agent = request.app.state.agent
    conv = await agent.store.get(conversation_id)
    if conv is None:
        raise HTTPException(404)
    msgs = await agent.store.transcript(conversation_id)
    reveal = agent.vault.rehydrate if agent.vault else None
    out = []
    for m in msgs:
        text = await reveal(conversation_id, m.text) if reveal else m.text
        out.append({"role": m.role, "text": text, "at": m.created_at})
    return {"id": conv.id, "channel": conv.channel, "status": conv.status, "messages": out}


@router.post("/conversations/{conversation_id}/messages", dependencies=[Depends(require_agent_key)])
async def send_message(request: Request, conversation_id: str, body: AgentMessage) -> dict:
    agent, gateway = request.app.state.agent, request.app.state.gateway
    conv = await agent.store.get(conversation_id)
    if conv is None:
        raise HTTPException(404)
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
    await agent.store.append(conversation_id, "human_agent", stored, meta={"agent": body.agent_name})
    if conv.status != "human":
        await agent.store.set_status(conversation_id, "human")  # an agent jumping in takes ownership
    return {"sent": True}


@router.post("/conversations/{conversation_id}/release", dependencies=[Depends(require_agent_key)])
async def release(request: Request, conversation_id: str) -> dict:
    agent, gateway = request.app.state.agent, request.app.state.gateway
    conv = await agent.store.get(conversation_id)
    if conv is None:
        raise HTTPException(404)
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
    return {"status": "bot"}
