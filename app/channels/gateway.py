"""Outbound delivery for every channel.

The agent decides *what* to say (`AgentReply`); the gateway decides *how* to deliver it:

  WhatsApp        POST /{PHONE_NUMBER_ID}/messages                (Cloud API)
  Messenger       POST /{PAGE_ID}/messages                        (Send API)
  Instagram DM    POST /{PAGE_ID or IG_ID}/messages               (Instagram Messaging)
  IG comment      POST /{COMMENT_ID}/replies                      (public reply)
  IG private      POST /{PAGE_ID or IG_ID}/messages               (recipient.comment_id)
                  -> one DM to the commenter, within 7 days of the comment

Credentials are looked up per business id through a `CredentialResolver`, so the same
gateway serves one store (Part 1) or many tenants (Part 4).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.channels.formatting import format_for_channel
from app.channels.graph_client import GraphAPIError, GraphClient
from app.domain import AgentReply, Channel, InboundMessage, MessageKind
from app.logging_config import log
from app.settings import Settings


@dataclass(frozen=True)
class ChannelCredentials:
    token: str
    sender_node: str  # the Graph node that sends: phone_number_id, page_id or IG id


class CredentialResolver(Protocol):
    def for_message(self, msg: InboundMessage) -> ChannelCredentials: ...


class SettingsCredentialResolver:
    """Single-store resolver backed by environment variables (Parts 1-3)."""

    def __init__(self, settings: Settings):
        self.s = settings

    def for_message(self, msg: InboundMessage) -> ChannelCredentials:
        if msg.channel == Channel.WHATSAPP:
            return ChannelCredentials(self.s.whatsapp_token.get_secret_value(), msg.business_id or self.s.whatsapp_phone_number_id)
        if msg.channel == Channel.MESSENGER:
            return ChannelCredentials(self.s.page_access_token.get_secret_value(), msg.business_id or self.s.page_id)
        return ChannelCredentials(self.s.ig_token.get_secret_value(), self.s.ig_messaging_node)


class ChannelGateway:
    def __init__(self, fb: GraphClient, ig: GraphClient, creds: CredentialResolver):
        self.fb = fb  # graph.facebook.com - WhatsApp, Messenger, IG via Facebook Login
        self.ig = ig  # graph.facebook.com or graph.instagram.com, depending on IG login type
        self.creds = creds

    def _client(self, channel: Channel) -> GraphClient:
        return self.ig if channel == Channel.INSTAGRAM else self.fb

    # ── High-level entry point ───────────────────────────────────────────────

    async def deliver(self, msg: InboundMessage, reply: AgentReply) -> None:
        """Send `reply` back on the same surface the customer used."""
        if msg.kind == MessageKind.COMMENT:
            if reply.text:
                await self.reply_to_comment(msg, format_for_channel(reply.text, msg.channel, MessageKind.COMMENT))
            if reply.private_text:
                await self.private_reply(msg, format_for_channel(reply.private_text, msg.channel))
            return
        await self.send_text(msg, format_for_channel(reply.text, msg.channel))

    # ── Primitives ───────────────────────────────────────────────────────────

    async def send_text(self, msg: InboundMessage, text: str) -> dict:
        c = self.creds.for_message(msg)
        if msg.channel == Channel.WHATSAPP:
            body = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": msg.user_id,
                "type": "text",
                "text": {"body": text, "preview_url": False},
                # Threads our answer under the customer's message in WhatsApp's UI.
                "context": {"message_id": msg.message_id},
            }
        else:
            body = {
                "recipient": {"id": msg.user_id},
                # RESPONSE = replying inside the 24h customer-service window.
                "messaging_type": "RESPONSE",
                "message": {"text": text},
            }
        return await self._client(msg.channel).post(f"{c.sender_node}/messages", c.token, body)

    async def reply_to_comment(self, msg: InboundMessage, text: str) -> dict:
        c = self.creds.for_message(msg)
        return await self.ig.post(f"{msg.message_id}/replies", c.token, {"message": text})

    async def private_reply(self, msg: InboundMessage, text: str) -> dict:
        """Instagram private reply: opens a DM thread from a comment (one message only)."""
        c = self.creds.for_message(msg)
        body = {"recipient": {"comment_id": msg.message_id}, "message": {"text": text}}
        return await self.ig.post(f"{c.sender_node}/messages", c.token, body)

    async def acknowledge(self, msg: InboundMessage) -> None:
        """Show 'seen' + typing indicator while the LLM thinks. Best-effort only."""
        if msg.kind == MessageKind.COMMENT:
            return
        c = self.creds.for_message(msg)
        try:
            if msg.channel == Channel.WHATSAPP:
                body = {
                    "messaging_product": "whatsapp",
                    "status": "read",
                    "message_id": msg.message_id,
                    "typing_indicator": {"type": "text"},
                }
            else:
                body = {"recipient": {"id": msg.user_id}, "sender_action": "typing_on"}
            await self._client(msg.channel).post(f"{c.sender_node}/messages", c.token, body)
        except GraphAPIError as e:  # never fail a conversation because a typing bubble failed
            log.warning("gateway.ack_failed", channel=msg.channel, error=str(e))
