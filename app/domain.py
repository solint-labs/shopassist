"""Channel-agnostic domain types.

Every channel (WhatsApp, Messenger, Instagram DM, Instagram comment) is normalised into
an `InboundMessage` at the edge. Everything downstream - the agent, memory, RAG,
escalation - only ever sees these types, so adding a channel never touches the brain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum


class Channel(StrEnum):
    WHATSAPP = "whatsapp"
    MESSENGER = "messenger"
    INSTAGRAM = "instagram"


class MessageKind(StrEnum):
    DIRECT = "direct"  # a 1:1 private message (DM / chat)
    COMMENT = "comment"  # a public comment on an Instagram post or reel


@dataclass(slots=True)
class InboundMessage:
    """A single customer message, normalised from any channel."""

    channel: Channel
    kind: MessageKind
    message_id: str  # platform id (wamid / mid / comment id) - used for de-duplication
    user_id: str  # wa_id / PSID / IGSID / IG user id of the commenter
    business_id: str  # phone_number_id / page id / IG account id that received it
    text: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    user_name: str | None = None
    # Comment-only fields
    media_id: str | None = None
    parent_comment_id: str | None = None
    # Postbacks / quick-reply payloads (button taps) arrive here
    payload: str | None = None
    # True for Messenger/IG "standby" events: the customer wrote while another app
    # (e.g. the Page inbox, after a hand-off) owns the thread. We record, never reply.
    standby: bool = False
    raw: dict | None = None  # original event, kept for debugging; never logged in prod

    @property
    def conversation_key(self) -> str:
        """Stable key for the conversation thread.

        DMs are keyed by (channel, business, user). Comments are a separate thread per
        commenter so a public comment never leaks DM history into a public reply.
        """
        scope = "c" if self.kind == MessageKind.COMMENT else "d"
        return f"{self.channel}:{self.business_id}:{scope}:{self.user_id}"


@dataclass(slots=True)
class QuickReply:
    """A tappable option: WhatsApp reply button / Messenger & Instagram quick reply."""

    id: str  # comes back as InboundMessage.payload when tapped
    title: str  # max 20 characters on every channel


@dataclass(slots=True)
class AgentReply:
    """What the agent decided to say. Delivery is a separate concern."""

    text: str
    # For Instagram comments: an optional private DM sent in addition to the public reply.
    private_text: str | None = None
    quick_replies: list[QuickReply] = field(default_factory=list)
    # Side effects for the gateway, e.g. "hide_comment" (Instagram moderation).
    actions: list[str] = field(default_factory=list)
    # Free-form metadata for logs/analytics (model, latency, sources used...)
    meta: dict = field(default_factory=dict)
