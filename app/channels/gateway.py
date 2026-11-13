"""Outbound delivery for every channel.

The agent decides *what* to say (`AgentReply`); the gateway decides *how* to deliver it:

  WhatsApp        POST /{PHONE_NUMBER_ID}/messages                (Cloud API)
  Messenger       POST /{PAGE_ID}/messages                        (Send API)
  Instagram DM    POST /{PAGE_ID or IG_ID}/messages               (Instagram Messaging)
  IG comment      POST /{COMMENT_ID}/replies                      (public reply)
  IG private      POST /{PAGE_ID or IG_ID}/messages               (recipient.comment_id)
                  -> one DM to the commenter, within 7 days of the comment
  IG moderation   POST /{COMMENT_ID}?hide=true                    (Part 3)
  Hand-off        POST /{PAGE_ID}/pass_thread_control             (Part 3, Messenger/IG)

Buttons (Part 3) render natively per channel: WhatsApp interactive reply buttons
(max 3) and Messenger/Instagram quick replies (max 13).

Credentials are looked up per business id through a `CredentialResolver`, so the same
gateway serves one store (Parts 1-3) or many tenants (Part 4).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.channels.formatting import format_for_channel
from app.channels.graph_client import GraphAPIError, GraphClient
from app.domain import AgentReply, Channel, InboundMessage, MessageKind, QuickReply
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
            if "hide_comment" in reply.actions:
                await self.hide_comment(msg)
            if reply.text:
                await self.reply_to_comment(msg, format_for_channel(reply.text, msg.channel, MessageKind.COMMENT))
            if reply.private_text:
                await self.private_reply(msg, format_for_channel(reply.private_text, msg.channel))
            return
        if reply.text:
            await self.send_text(msg, format_for_channel(reply.text, msg.channel), reply.quick_replies)

    # ── Primitives ───────────────────────────────────────────────────────────

    async def send_text(self, msg: InboundMessage, text: str, quick_replies: list[QuickReply] | None = None,
                        human_agent_tag: bool = False) -> dict:
        c = self.creds.for_message(msg)
        quick_replies = quick_replies or []
        if msg.channel == Channel.WHATSAPP:
            body: dict = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": msg.user_id}
            if quick_replies:
                body |= {"type": "interactive", "interactive": {
                    "type": "button", "body": {"text": text[:1024]},
                    "action": {"buttons": [{"type": "reply", "reply": {"id": q.id, "title": q.title[:20]}}
                                           for q in quick_replies[:3]]}}}
            else:
                body |= {"type": "text", "text": {"body": text, "preview_url": False}}
            if msg.message_id:
                # Threads our answer under the customer's message in WhatsApp's UI.
                body["context"] = {"message_id": msg.message_id}
        else:
            message: dict = {"text": text}
            if quick_replies:
                message["quick_replies"] = [{"content_type": "text", "title": q.title[:20], "payload": q.id}
                                            for q in quick_replies[:13]]
            body = {"recipient": {"id": msg.user_id}, "message": message}
            if human_agent_tag:
                # Human agents may reply up to 7 days after the customer's last message.
                body |= {"messaging_type": "MESSAGE_TAG", "tag": "HUMAN_AGENT"}
            else:
                # RESPONSE = replying inside the 24h customer-service window.
                body["messaging_type"] = "RESPONSE"
        return await self._client(msg.channel).post(f"{c.sender_node}/messages", c.token, body)

    async def reply_to_comment(self, msg: InboundMessage, text: str) -> dict:
        c = self.creds.for_message(msg)
        return await self.ig.post(f"{msg.message_id}/replies", c.token, {"message": text})

    async def private_reply(self, msg: InboundMessage, text: str) -> dict:
        """Instagram private reply: opens a DM thread from a comment (one message only)."""
        c = self.creds.for_message(msg)
        body = {"recipient": {"comment_id": msg.message_id}, "message": {"text": text}}
        return await self.ig.post(f"{c.sender_node}/messages", c.token, body)

    async def hide_comment(self, msg: InboundMessage) -> dict:
        """Hide a comment: it stays visible to its author, hidden from everyone else."""
        c = self.creds.for_message(msg)
        return await self.ig.post(f"{msg.message_id}?hide=true", c.token, {})

    async def pass_thread_control(self, msg: InboundMessage, target_app_id: str, metadata: str = "") -> dict:
        c = self.creds.for_message(msg)
        body = {"recipient": {"id": msg.user_id}, "target_app_id": target_app_id, "metadata": metadata[:1000]}
        return await self._client(msg.channel).post(f"{c.sender_node}/pass_thread_control", c.token, body)

    async def take_thread_control(self, msg: InboundMessage, metadata: str = "") -> dict:
        c = self.creds.for_message(msg)
        body = {"recipient": {"id": msg.user_id}, "metadata": metadata[:1000]}
        return await self._client(msg.channel).post(f"{c.sender_node}/take_thread_control", c.token, body)

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
