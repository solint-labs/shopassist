"""Pseudonymisation with an encrypted token vault.

    customer:  "my email is asha@example.com, order 1042"
    stored:    "my email is <EMAIL_1>, order 1042"           (messages table)
    LLM sees:  "my email is <EMAIL_1>, order 1042"           (provider never gets raw PII)
    vault:     <EMAIL_1> -> Fernet(asha@example.com)         (pii_tokens table, encrypted)
    tool call: get_order_status(email="<EMAIL_1>")  -> rehydrated in code before Shopify is called
    reply:     "...sent to <EMAIL_1>" -> rehydrated just before delivery to the customer

Why tokens rather than masking? The agent still needs to *use* the value (look up the
order by email) without the LLM provider, our logs, or our message store ever holding it.

Encryption: Fernet (AES-128-CBC + HMAC-SHA256). MultiFernet supports key rotation:
put the new key first in PII_ENCRYPTION_KEYS, keep old keys after it, run
`python -m scripts.retention --rotate-keys`.

Blind index: HMAC-SHA256(normalised value) lets us find every conversation containing
an email/phone (for GDPR erasure requests) without storing or decrypting the values.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, delete, select
from sqlalchemy.orm import Mapped, mapped_column

from app.privacy.pii import IRREVERSIBLE_LABELS, PIIDetector
from app.storage.db import Base, Database

TOKEN_RE = re.compile(r"<(EMAIL|PHONE|NAME|ADDRESS)_(\d+)>")


class PIIToken(Base):
    __tablename__ = "pii_tokens"
    __table_args__ = (UniqueConstraint("conversation_id", "token"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    token: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(16))
    ciphertext: Mapped[str] = mapped_column(Text)
    blind_index: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


def normalise(kind: str, value: str) -> str:
    if kind == "EMAIL":
        return value.strip().lower()
    if kind == "PHONE":
        return re.sub(r"\D", "", value)[-10:]
    return " ".join(value.lower().split())


@dataclass
class RedactionResult:
    text: str
    kinds: set[str] = field(default_factory=set)  # everything detected
    blocked: set[str] = field(default_factory=set)  # irreversible kinds (card, gov id...) that were removed


class PIIVault:
    def __init__(self, db: Database, encryption_keys: list[str], blind_index_key: str,
                 detector: PIIDetector | None = None):
        if not encryption_keys:
            raise ValueError("At least one PII encryption key is required")
        self.db = db
        self.fernet = MultiFernet([Fernet(k.encode()) for k in encryption_keys])
        self.bkey = blind_index_key.encode()
        self.detector = detector or PIIDetector()

    def blind_index(self, kind: str, value: str) -> str:
        return hmac.new(self.bkey, f"{kind}:{normalise(kind, value)}".encode(), hashlib.sha256).hexdigest()

    async def pseudonymize(self, conversation_id: str, text: str) -> RedactionResult:
        dets = self.detector.detect(text)
        if not dets:
            return RedactionResult(text)
        result = RedactionResult("")
        async with self.db.session() as s:
            rows = (await s.execute(select(PIIToken).where(PIIToken.conversation_id == conversation_id))).scalars().all()
            by_index = {r.blind_index: r.token for r in rows}
            counters: dict[str, int] = {}
            for r in rows:
                counters[r.kind] = max(counters.get(r.kind, 0), int(r.token.rsplit("_", 1)[1].rstrip(">")))
            out, last = [], 0
            for d in dets:
                out.append(text[last:d.start])
                result.kinds.add(d.kind)
                if not d.reversible:
                    out.append(IRREVERSIBLE_LABELS.get(d.kind, "[removed]"))
                    result.blocked.add(d.kind)
                else:
                    bi = self.blind_index(d.kind, d.value)
                    token = by_index.get(bi)
                    if token is None:  # same value in the same conversation -> same token
                        counters[d.kind] = counters.get(d.kind, 0) + 1
                        token = f"<{d.kind}_{counters[d.kind]}>"
                        by_index[bi] = token
                        s.add(PIIToken(conversation_id=conversation_id, token=token, kind=d.kind,
                                       ciphertext=self.fernet.encrypt(d.value.encode()).decode(), blind_index=bi))
                    out.append(token)
                last = d.end
            out.append(text[last:])
        result.text = "".join(out)
        return result

    async def rehydrate(self, conversation_id: str, text: str) -> str:
        """Replace this conversation's tokens with real values. Unknown/expired tokens become [KIND]."""
        if not TOKEN_RE.search(text or ""):
            return text
        mapping = await self._mapping(conversation_id)
        return TOKEN_RE.sub(lambda m: mapping.get(m.group(0), f"[{m.group(1)}]"), text)

    async def rehydrate_args(self, conversation_id: str, args: dict) -> dict:
        out = {}
        for k, v in args.items():
            out[k] = await self.rehydrate(conversation_id, v) if isinstance(v, str) else v
        return out

    async def _mapping(self, conversation_id: str) -> dict[str, str]:
        async with self.db.session() as s:
            rows = (await s.execute(select(PIIToken).where(PIIToken.conversation_id == conversation_id))).scalars().all()
        mapping = {}
        for r in rows:
            try:
                mapping[r.token] = self.fernet.decrypt(r.ciphertext.encode()).decode()
            except InvalidToken:  # key removed after rotation: value is unrecoverable by design
                continue
        return mapping

    async def conversations_for(self, kind: str, value: str) -> set[str]:
        """Find conversations that mention a given email/phone, via the blind index."""
        async with self.db.session() as s:
            rows = (await s.execute(select(PIIToken.conversation_id)
                                    .where(PIIToken.blind_index == self.blind_index(kind, value)))).scalars().all()
        return set(rows)

    async def forget_conversation(self, conversation_id: str) -> None:
        async with self.db.session() as s:
            await s.execute(delete(PIIToken).where(PIIToken.conversation_id == conversation_id))

    async def rotate(self) -> int:
        """Re-encrypt every value with the primary (first) key."""
        n = 0
        async with self.db.session() as s:
            for row in (await s.execute(select(PIIToken))).scalars():
                row.ciphertext = self.fernet.rotate(row.ciphertext.encode()).decode()
                n += 1
        return n


def generate_key() -> str:
    return Fernet.generate_key().decode()
