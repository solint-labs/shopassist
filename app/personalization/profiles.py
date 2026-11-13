"""Per-customer profiles: remembered preferences that shape every future reply.

Customers tell you how they want to be spoken to ("reply in Hindi", "shorter please",
"no emojis"). A good human agent remembers; so should the bot, across sessions and
channels. Preferences are captured three ways:
  1. explicit phrases, detected deterministically (below), so they're cheap and reliable,
  2. the LLM's `set_customer_preferences` tool for fuzzier requests,
  3. the admin API / CRM sync (e.g. VIP from Shopify customer tags).

Profiles hold *preferences*, not personal data: no names, emails or addresses.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, String, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column

from app.commerce.tools import Tool, ToolContext
from app.domain import InboundMessage
from app.llm.base import ToolSpec
from app.storage.db import Base, Database

LANGUAGES = {"english": "en", "hindi": "hi", "hinglish": "hi-Latn", "tamil": "ta", "telugu": "te", "bengali": "bn",
             "bangla": "bn", "marathi": "mr", "kannada": "kn", "gujarati": "gu", "malayalam": "ml", "punjabi": "pa",
             "urdu": "ur", "spanish": "es", "french": "fr", "german": "de", "arabic": "ar", "portuguese": "pt"}


class CustomerProfile(Base):
    __tablename__ = "customer_profiles"
    __table_args__ = (UniqueConstraint("tenant_id", "customer_key"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    customer_key: Mapped[str] = mapped_column(String(160))  # "<channel>:<platform user id>"
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    formality: Mapped[str | None] = mapped_column(String(16), nullable=True)
    verbosity: Mapped[str | None] = mapped_column(String(16), nullable=True)
    emoji: Mapped[str | None] = mapped_column(String(16), nullable=True)
    vip: Mapped[bool] = mapped_column(Boolean, default=False)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)  # e.g. "prefers decaf"; no PII
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


def customer_key(msg: InboundMessage) -> str:
    return f"{msg.channel}:{msg.user_id}"


PREF_FIELDS = ("language", "formality", "verbosity", "emoji", "vip", "tags", "notes")


class ProfileStore:
    def __init__(self, db: Database):
        self.db = db

    async def get(self, tenant_id: str, key: str) -> CustomerProfile | None:
        async with self.db.session() as s:
            return (await s.execute(select(CustomerProfile).where(
                CustomerProfile.tenant_id == tenant_id, CustomerProfile.customer_key == key))).scalar_one_or_none()

    async def update(self, tenant_id: str, key: str, **prefs: Any) -> CustomerProfile:
        async with self.db.session() as s:
            p = (await s.execute(select(CustomerProfile).where(
                CustomerProfile.tenant_id == tenant_id, CustomerProfile.customer_key == key))).scalar_one_or_none()
            if p is None:
                p = CustomerProfile(tenant_id=tenant_id, customer_key=key, tags=[])
                s.add(p)
            for k, v in prefs.items():
                if k in PREF_FIELDS and v is not None:
                    setattr(p, k, v)
            p.updated_at = datetime.now(timezone.utc)
            await s.flush()
            return p


_LANG = re.compile(r"(?i)\b(?:reply|respond|answer|talk|speak|write|chat)\b[^.?!]{0,20}\bin\s+(\w+)|^\s*(\w+)\s+(?:please|pls)\s*$|\bin\s+(\w+)\s+(?:please|pls)\b")
_BRIEF = re.compile(r"(?i)\b(be (?:more )?(?:brief|concise)|shorter (?:answers|replies|messages)|keep it short|too long)\b")
_DETAILED = re.compile(r"(?i)\b(more details?|in detail|explain (?:more|properly)|longer (?:answers|replies))\b")
_NO_EMOJI = re.compile(r"(?i)\b(no|stop(?: using)?|without|don'?t use)\s+(?:the\s+)?emojis?\b")
_MORE_EMOJI = re.compile(r"(?i)\b(more|love (?:the|your)) emojis?\b")
_FORMAL = re.compile(r"(?i)\b(be (?:more )?formal|formal (?:please|tone)|more professional)\b")
_CASUAL = re.compile(r"(?i)\b(no need to be formal|casual is fine|be (?:more )?casual|less formal)\b")


def detect_preferences(text: str) -> dict[str, str]:
    """Explicit style requests in a message -> profile fields. Deterministic and cheap."""
    prefs: dict[str, str] = {}
    m = _LANG.search(text)
    if m:
        word = next(g for g in m.groups() if g).lower()
        if word in LANGUAGES:
            prefs["language"] = LANGUAGES[word]
    if _BRIEF.search(text):
        prefs["verbosity"] = "brief"
    elif _DETAILED.search(text):
        prefs["verbosity"] = "detailed"
    if _NO_EMOJI.search(text):
        prefs["emoji"] = "none"
    elif _MORE_EMOJI.search(text):
        prefs["emoji"] = "expressive"
    if _FORMAL.search(text):
        prefs["formality"] = "formal"
    elif _CASUAL.search(text):
        prefs["formality"] = "casual"
    return prefs


def build_profile_tools(profiles: ProfileStore, tenant_id: str) -> list[Tool]:
    async def set_customer_preferences(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        allowed = {"language", "formality", "verbosity", "emoji"}
        prefs = {k: v for k, v in args.items() if k in allowed and v}
        await profiles.update(tenant_id, customer_key(ctx.msg), **prefs)
        ctx.state["prefs_changed"] = True
        return {"status": "saved", "preferences": prefs}

    return [Tool(ToolSpec(
        "set_customer_preferences",
        "Remember how this customer wants to be spoken to, when they ask for it (language, tone, length, emoji).",
        {"type": "object", "properties": {
            "language": {"type": "string", "description": "ISO code, e.g. en, hi, ta"},
            "formality": {"type": "string", "enum": ["casual", "neutral", "formal"]},
            "verbosity": {"type": "string", "enum": ["brief", "balanced", "detailed"]},
            "emoji": {"type": "string", "enum": ["none", "light", "expressive"]}}}),
        set_customer_preferences)]
