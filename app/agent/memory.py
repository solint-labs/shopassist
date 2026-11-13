"""Conversation memory backed by the database.

We store every turn (for audit, analytics and human hand-off) but only feed the LLM
the last N turns: older turns cost tokens and rarely help a support conversation.
"""

from __future__ import annotations

from sqlalchemy import select

from app.domain import InboundMessage
from app.llm.base import ChatMessage
from app.storage.db import Database
from app.storage.models import Conversation, Message


class ConversationStore:
    def __init__(self, db: Database):
        self.db = db

    async def get_or_create(self, msg: InboundMessage, tenant_id: str) -> Conversation:
        async with self.db.session() as s:
            conv = (await s.execute(select(Conversation).where(Conversation.key == msg.conversation_key))).scalar_one_or_none()
            if conv is None:
                conv = Conversation(
                    key=msg.conversation_key, tenant_id=tenant_id, channel=str(msg.channel), kind=str(msg.kind),
                    user_id=msg.user_id, user_name=msg.user_name, meta={},
                )
                s.add(conv)
                await s.flush()
            return conv

    async def append(self, conversation_id: str, role: str, text: str, channel_message_id: str | None = None,
                     meta: dict | None = None) -> Message:
        async with self.db.session() as s:
            m = Message(conversation_id=conversation_id, role=role, text=text,
                        channel_message_id=channel_message_id, meta=meta or {})
            s.add(m)
            conv = await s.get(Conversation, conversation_id)
            if conv is not None:
                conv.updated_at = m.created_at  # touch for "recent conversations" queries
            return m

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
