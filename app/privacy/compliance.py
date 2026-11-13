"""Data-subject rights: Shopify's mandatory privacy webhooks, erasure and retention.

Shopify requires every app to handle three compliance webhooks:

  customers/data_request  a customer asked the merchant for their data -> export what we hold
  customers/redact        erase a customer's data                          -> delete their conversations
  shop/redact             48h after uninstall, erase the shop's data       -> delete the tenant's data

Shopify signs these with HMAC-SHA256 of the raw body, keyed with the app's client
secret, base64-encoded in `X-Shopify-Hmac-SHA256`.

We find a customer's conversations without storing their email/phone in plaintext:
the vault's blind index (HMAC of the normalised value) plus the WhatsApp user id
(which is the phone number).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import delete, select

from app.agent.memory import ConversationStore
from app.logging_config import log
from app.privacy.vault import PIIToken, PIIVault
from app.storage.models import Conversation, Message

router = APIRouter(prefix="/webhooks/shopify", tags=["privacy"])


def verify_shopify_hmac(body: bytes, header: str | None, secret: str) -> bool:
    if not header or not secret:
        return False
    digest = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(digest, header)


class PrivacyService:
    def __init__(self, store: ConversationStore, vault: PIIVault | None):
        self.store, self.vault = store, vault

    async def conversations_for_customer(self, email: str | None, phone: str | None) -> set[str]:
        ids: set[str] = set()
        if self.vault:
            if email:
                ids |= await self.vault.conversations_for("EMAIL", email)
            if phone:
                ids |= await self.vault.conversations_for("PHONE", phone)
        if phone:  # WhatsApp conversations are keyed by the phone number itself
            digits = re.sub(r"\D", "", phone)[-10:]
            async with self.store.db.session() as s:
                rows = (await s.execute(select(Conversation.id, Conversation.user_id)
                                        .where(Conversation.channel == "whatsapp"))).all()
            ids |= {cid for cid, uid in rows if re.sub(r"\D", "", uid)[-10:] == digits and len(digits) >= 8}
        return ids

    async def export_customer(self, email: str | None, phone: str | None) -> list[dict]:
        out = []
        for cid in await self.conversations_for_customer(email, phone):
            msgs = await self.store.transcript(cid)
            texts = [await self.vault.rehydrate(cid, m.text) if self.vault else m.text for m in msgs]
            out.append({"conversation_id": cid,
                        "messages": [{"role": m.role, "text": t, "at": m.created_at.isoformat()} for m, t in zip(msgs, texts)]})
        return out

    async def erase_customer(self, email: str | None, phone: str | None) -> int:
        ids = await self.conversations_for_customer(email, phone)
        for cid in ids:
            await self.store.delete(cid)
        log.info("privacy.erased_customer", conversations=len(ids))
        return len(ids)

    async def erase_tenant(self, tenant_id: str) -> int:
        async with self.store.db.session() as s:
            ids = (await s.execute(select(Conversation.id).where(Conversation.tenant_id == tenant_id))).scalars().all()
        for cid in ids:
            await self.store.delete(cid)
        log.info("privacy.erased_tenant", tenant=tenant_id, conversations=len(ids))
        return len(ids)

    async def apply_retention(self, message_days: int, pii_days: int, now: datetime | None = None) -> dict[str, int]:
        """Delete old messages and old vault entries. Run daily (scripts/retention.py)."""
        now = now or datetime.now(timezone.utc)
        async with self.store.db.session() as s:
            m = await s.execute(delete(Message).where(Message.created_at < now - timedelta(days=message_days)))
            # Vault entries go once a conversation has been idle for `pii_days`. Deleting per
            # conversation (not per token) keeps placeholder numbering consistent in live chats.
            idle = select(Conversation.id).where(Conversation.updated_at < now - timedelta(days=pii_days))
            p = await s.execute(delete(PIIToken).where(PIIToken.conversation_id.in_(idle)))
        stats = {"messages_deleted": m.rowcount or 0, "pii_tokens_deleted": p.rowcount or 0}
        log.info("privacy.retention", **stats)
        return stats


@router.post("/compliance")
async def shopify_compliance(request: Request) -> dict:
    settings = request.app.state.settings
    body = await request.body()
    secret = (settings.shopify_webhook_secret or settings.shopify_client_secret)
    if not verify_shopify_hmac(body, request.headers.get("x-shopify-hmac-sha256"),
                               secret.get_secret_value() if secret else ""):
        raise HTTPException(401, "invalid hmac")
    topic = request.headers.get("x-shopify-topic", "")
    payload = json.loads(body or b"{}")
    privacy: PrivacyService = request.app.state.privacy
    customer = payload.get("customer") or {}
    if topic == "customers/redact":
        n = await privacy.erase_customer(customer.get("email"), customer.get("phone"))
        return {"erased_conversations": n}
    if topic == "customers/data_request":
        export = await privacy.export_customer(customer.get("email"), customer.get("phone"))
        # Hand the export to the merchant through your secure channel of choice (e.g. an
        # encrypted upload + email). We only log the size, never the content.
        log.info("privacy.data_request", conversations=len(export), request_id=payload.get("data_request", {}).get("id"))
        return {"conversations": len(export)}
    if topic == "shop/redact":
        return {"erased_conversations": await privacy.erase_tenant(request.app.state.tenant.tenant_id)}
    raise HTTPException(400, f"unsupported topic {topic}")
