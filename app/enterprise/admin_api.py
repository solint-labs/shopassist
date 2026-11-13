"""Admin and analytics API.

    POST   /admin/api-keys                         create a key (returned once)
    GET    /admin/api-keys                         list keys (no secrets)
    DELETE /admin/api-keys/{id}                    revoke
    GET    /admin/audit                            audit log (filter by tenant/action)
    GET    /admin/analytics?tenant=&days=          containment, escalations by reason, volume by channel
    POST   /admin/tenants/{tenant}/reindex         re-ingest markdown + Shopify content
    PUT    /admin/tenants/{tenant}/profiles/{key}  set customer preferences / VIP (CRM sync)
    DELETE /admin/conversations/{id}               right-to-erasure by an operator
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select

from app.enterprise.rbac import ROLES, Principal, require
from app.rag.ingest import Ingestor, load_markdown_dir, shopify_documents
from app.storage.models import Conversation, Handoff, Message

router = APIRouter(prefix="/admin", tags=["admin"])


def _scope(who: Principal, tenant_id: str | None) -> str | None:
    if who.tenant_id and tenant_id and tenant_id != who.tenant_id:
        raise HTTPException(404)
    return who.tenant_id or tenant_id


class NewKey(BaseModel):
    name: str
    role: Literal["admin", "supervisor", "agent", "analyst", "auditor"]
    tenant_id: str | None = None


@router.post("/api-keys")
async def create_key(request: Request, body: NewKey, who: Principal = Depends(require("*"))) -> dict:
    if body.role not in ROLES:
        raise HTTPException(400, "unknown role")
    key, row = await request.app.state.keys.create(body.name, body.role, body.tenant_id, who.name)
    await request.app.state.auditor.record(who, "api_key.create", f"api_key:{row.id}", body.tenant_id,
                                           role=body.role, name=body.name)
    return {"id": row.id, "key": key, "note": "Store this key now; it cannot be shown again."}


@router.get("/api-keys")
async def list_keys(request: Request, who: Principal = Depends(require("*"))) -> list[dict]:
    return [{"id": k.id, "name": k.name, "role": k.role, "tenant_id": k.tenant_id, "prefix": k.prefix,
             "created_at": k.created_at, "revoked": k.revoked_at is not None}
            for k in await request.app.state.keys.list()]


@router.delete("/api-keys/{key_id}")
async def revoke_key(request: Request, key_id: str, who: Principal = Depends(require("*"))) -> dict:
    if not await request.app.state.keys.revoke(key_id):
        raise HTTPException(404)
    await request.app.state.auditor.record(who, "api_key.revoke", f"api_key:{key_id}")
    return {"revoked": True}


@router.get("/audit")
async def audit(request: Request, tenant_id: str | None = None, action: str | None = None, limit: int = 200,
                who: Principal = Depends(require("audit:read"))) -> list[dict]:
    rows = await request.app.state.auditor.query(_scope(who, tenant_id), action, min(limit, 1000))
    return [{"at": r.at, "actor": r.actor, "role": r.role, "tenant_id": r.tenant_id, "action": r.action,
             "resource": r.resource, "detail": r.detail} for r in rows]


@router.get("/analytics")
async def analytics(request: Request, tenant_id: str | None = None, days: int = 7,
                    who: Principal = Depends(require("analytics:read"))) -> dict:
    """Containment = share of conversations in the window that never needed a human."""
    tenant_id = _scope(who, tenant_id)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    db = request.app.state.db
    async with db.session() as s:
        conv_q = select(Conversation.channel, func.count()).where(Conversation.created_at >= since)
        ho_q = select(Handoff.reason, func.count(func.distinct(Handoff.conversation_id))).join(
            Conversation, Conversation.id == Handoff.conversation_id).where(Handoff.created_at >= since)
        msg_q = select(func.count()).select_from(Message).join(Conversation).where(
            Message.created_at >= since, Message.role == "user")
        if tenant_id:
            conv_q, ho_q = conv_q.where(Conversation.tenant_id == tenant_id), ho_q.where(Conversation.tenant_id == tenant_id)
            msg_q = msg_q.where(Conversation.tenant_id == tenant_id)
        by_channel = dict((await s.execute(conv_q.group_by(Conversation.channel))).all())
        by_reason = dict((await s.execute(ho_q.group_by(Handoff.reason))).all())
        inbound = (await s.execute(msg_q)).scalar_one()
    total, escalated = sum(by_channel.values()), sum(by_reason.values())
    return {"window_days": days, "tenant_id": tenant_id, "conversations": total, "inbound_messages": inbound,
            "conversations_by_channel": by_channel, "escalations_by_reason": by_reason,
            "containment_rate": round(1 - escalated / total, 3) if total else None}


@router.post("/tenants/{tenant_id}/reindex")
async def reindex(request: Request, tenant_id: str, include_shopify: bool = True,
                  who: Principal = Depends(require("kb:reindex"))) -> dict:
    _scope(who, tenant_id)
    rt = request.app.state.registry.tenants.get(tenant_id)
    if rt is None:
        raise HTTPException(404)
    s = request.app.state.settings
    kdir = Path(s.knowledge_root) / tenant_id if s.knowledge_root else Path(s.knowledge_dir)
    docs = load_markdown_dir(kdir) if kdir.exists() else []
    if include_shopify and rt.agent.shopify:
        docs += await shopify_documents(rt.agent.shopify)
    stats = await Ingestor(request.app.state.knowledge).ingest(tenant_id, docs)
    await request.app.state.auditor.record(who, "kb.reindex", f"tenant:{tenant_id}", tenant_id, **stats)
    return stats


class ProfileUpdate(BaseModel):
    language: str | None = None
    formality: Literal["casual", "neutral", "formal"] | None = None
    verbosity: Literal["brief", "balanced", "detailed"] | None = None
    emoji: Literal["none", "light", "expressive"] | None = None
    vip: bool | None = None
    tags: list[str] | None = None
    notes: str | None = None


@router.put("/tenants/{tenant_id}/profiles/{customer_key}")
async def set_profile(request: Request, tenant_id: str, customer_key: str, body: ProfileUpdate,
                      who: Principal = Depends(require("profiles:write"))) -> dict:
    _scope(who, tenant_id)
    p = await request.app.state.profiles.update(tenant_id, customer_key, **body.model_dump(exclude_none=True))
    await request.app.state.auditor.record(who, "profile.update", f"profile:{customer_key}", tenant_id,
                                           fields=sorted(body.model_dump(exclude_none=True)))
    return {"customer_key": p.customer_key, "vip": p.vip, "language": p.language, "formality": p.formality,
            "verbosity": p.verbosity, "emoji": p.emoji}


@router.delete("/conversations/{conversation_id}")
async def erase_conversation(request: Request, conversation_id: str, who: Principal = Depends(require("*"))) -> dict:
    store = request.app.state.registry.default.agent.store
    conv = await store.get(conversation_id)
    if conv is None or not who.can_access_tenant(conv.tenant_id):
        raise HTTPException(404)
    await store.delete(conversation_id)
    await request.app.state.auditor.record(who, "conversation.erase", f"conversation:{conversation_id}", conv.tenant_id)
    return {"erased": True}
