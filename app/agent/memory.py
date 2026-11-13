"""Conversation memory backed by the database.

We store every turn (for audit, analytics and human hand-off) but only feed the LLM
the last N turns: older turns cost tokens and rarely help a support conversation.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, select

from app.domain import InboundMessage
from app.llm.base import ChatMessage
from app.storage.db import Database
from app.storage.models import Conversation, Handoff, Message


class ConversationStore:
    def __init__(self, db: Database):
        self.db = db

    async def get_or_create(self, msg: InboundMessage, tenant_id: str) -> Conversation:
        async with self.db.session() as s:
            conv = (await s.execute(select(Conversation).where(Conversation.key == msg.conversation_key))).scalar_one_or_none()
            if conv is None:
                conv = Conversation(
                    key=msg.conversation_key, tenant_id=tenant_id, channel=str(msg.channel), kind=str(msg.kind),
                    user_id=msg.user_id, business_id=msg.business_id, user_name=msg.user_name, meta={},
                )
                s.add(conv)
                await s.flush()
            return conv

    async def append(self, conversation_id: str, role: str, text: str, channel_message_id: str | None = None,
                     meta: dict | None = None) -> Message:
        async with self.db.session() as s:
            m = Message(conversation_id=conversation_id, role=role, text=text,
                        channel_message_id=channel_message_id, meta=meta or {},
                        created_at=datetime.now(timezone.utc))
            s.add(m)
            conv = await s.get(Conversation, conversation_id)
            if conv is not None:
                conv.updated_at = m.created_at  # touch for "recent conversations" queries
                if role == "user":
                    conv.last_inbound_at = m.created_at  # drives the 24h messaging-window check
            return m

    async def get(self, conversation_id: str) -> Conversation | None:
        async with self.db.session() as s:
            return await s.get(Conversation, conversation_id)

    async def find_by_key(self, key: str) -> Conversation | None:
        async with self.db.session() as s:
            return (await s.execute(select(Conversation).where(Conversation.key == key))).scalar_one_or_none()

    async def set_status(self, conversation_id: str, status: str) -> None:
        async with self.db.session() as s:
            conv = await s.get(Conversation, conversation_id)
            if conv is not None:
                conv.status = status

    async def list_by_status(self, status: str, tenant_id: str | None = None, limit: int = 100) -> list[Conversation]:
        async with self.db.session() as s:
            q = select(Conversation).where(Conversation.status == status)
            if tenant_id:
                q = q.where(Conversation.tenant_id == tenant_id)
            return list((await s.execute(q.order_by(Conversation.updated_at.desc()).limit(limit))).scalars())

    async def transcript(self, conversation_id: str) -> list[Message]:
        async with self.db.session() as s:
            return list((await s.execute(select(Message).where(Message.conversation_id == conversation_id)
                                         .order_by(Message.created_at))).scalars())

    async def delete(self, conversation_id: str) -> None:
        """Hard delete: messages and vault entries cascade with the conversation."""
        from app.privacy.vault import PIIToken

        async with self.db.session() as s:
            await s.execute(delete(PIIToken).where(PIIToken.conversation_id == conversation_id))
            await s.execute(delete(Message).where(Message.conversation_id == conversation_id))
            await s.execute(delete(Handoff).where(Handoff.conversation_id == conversation_id))
            await s.execute(delete(Conversation).where(Conversation.id == conversation_id))

    async def get_state(self, conversation_id: str) -> dict:
        """Per-conversation working state (verified orders, flow progress...)."""
        async with self.db.session() as s:
            conv = await s.get(Conversation, conversation_id)
            return dict((conv.meta or {}).get("state", {})) if conv else {}

    async def save_state(self, conversation_id: str, state: dict) -> None:
        async with self.db.session() as s:
            conv = await s.get(Conversation, conversation_id)
            if conv is not None:
                conv.meta = {**(conv.meta or {}), "state": state}  # reassign so SQLAlchemy sees the change

    async def history(self, conversation_id: str, limit: int) -> list[ChatMessage]:
        """Most recent `limit` turns, oldest first, mapped to LLM roles."""
        async with self.db.session() as s:
            rows = (await s.execute(
                select(Message).where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at.desc()).limit(limit)
            )).scalars().all()
        out: list[ChatMessage] = []
        for m in reversed(rows):
            if m.role == "user":
                out.append(ChatMessage(role="user", content=m.text))
            elif m.role in {"assistant", "human_agent"}:
                out.append(ChatMessage(role="assistant", content=m.text))
        return out
