"""Parse Meta webhook payloads into channel-agnostic `InboundMessage`s.

One endpoint receives three different payload shapes, distinguished by `object`:

  object = "whatsapp_business_account"   -> entry[].changes[].value.messages[]
  object = "page"                        -> entry[].messaging[]      (Messenger)
  object = "instagram"                   -> entry[].messaging[]      (IG DMs)
                                            entry[].changes[] field=comments (IG comments)

A single POST can batch several entries/messages, so we always return a list.
Anything we don't handle yet (reactions, read receipts, delivery statuses, media
without captions...) is skipped deliberately rather than crashing the webhook.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.domain import Channel, InboundMessage, MessageKind


def _ts(value: Any) -> datetime:
    """Meta sends seconds (WhatsApp, as a string) or milliseconds (Messenger/IG)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
    if v > 1e12:  # milliseconds
        v /= 1000.0
    return datetime.fromtimestamp(v, tz=timezone.utc)


def parse_webhook(payload: dict[str, Any]) -> list[InboundMessage]:
    obj = payload.get("object")
    if obj == "whatsapp_business_account":
        return _parse_whatsapp(payload)
    if obj == "page":
        return _parse_messaging(payload, Channel.MESSENGER)
    if obj == "instagram":
        return _parse_messaging(payload, Channel.INSTAGRAM) + _parse_ig_comments(payload)
    return []


# ── WhatsApp Cloud API ────────────────────────────────────────────────────────


def _parse_whatsapp(payload: dict[str, Any]) -> list[InboundMessage]:
    out: list[InboundMessage] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") != "messages":
                continue
            value = change.get("value", {})
            phone_number_id = value.get("metadata", {}).get("phone_number_id", "")
            names = {c.get("wa_id"): c.get("profile", {}).get("name") for c in value.get("contacts", [])}
            # `statuses` (sent/delivered/read) arrive on the same field; we ignore them here.
            for m in value.get("messages", []):
                text, payload_value = _whatsapp_text(m)
                if text is None:
                    continue  # stickers, location, etc. - extend as needed
                out.append(
                    InboundMessage(
                        channel=Channel.WHATSAPP,
                        kind=MessageKind.DIRECT,
                        message_id=m["id"],
                        user_id=m["from"],
                        business_id=phone_number_id,
                        text=text,
                        timestamp=_ts(m.get("timestamp")),
                        user_name=names.get(m["from"]),
                        payload=payload_value,
                        raw=m,
                    )
                )
    return out


def _whatsapp_text(m: dict[str, Any]) -> tuple[str | None, str | None]:
    """Extract the user-visible text (and button payload, if any) from a WA message."""
    t = m.get("type")
    if t == "text":
        return m.get("text", {}).get("body", ""), None
    if t == "button":  # quick-reply button on a template message
        b = m.get("button", {})
        return b.get("text", ""), b.get("payload")
    if t == "interactive":  # reply buttons / list replies we sent
        i = m.get("interactive", {})
        reply = i.get("button_reply") or i.get("list_reply") or {}
        return reply.get("title", ""), reply.get("id")
    if t in {"image", "video", "document"}:
        caption = m.get(t, {}).get("caption")
        return (caption or f"[{t} received]"), None
    return None, None


# ── Messenger + Instagram DMs (same "messaging" envelope) ─────────────────────


def _parse_messaging(payload: dict[str, Any], channel: Channel) -> list[InboundMessage]:
    out: list[InboundMessage] = []
    for entry in payload.get("entry", []):
        business_id = str(entry.get("id", ""))
        for ev in entry.get("messaging", []):
            msg = ev.get("message")
            postback = ev.get("postback")
            if msg:
                if msg.get("is_echo") or msg.get("is_deleted"):
                    continue  # our own outbound message echoed back, or an unsend
                text = msg.get("text")
                qr = (msg.get("quick_reply") or {}).get("payload")
                if text is None:
                    if msg.get("attachments"):
                        text = "[attachment received]"
                    else:
                        continue
                out.append(
                    InboundMessage(
                        channel=channel,
                        kind=MessageKind.DIRECT,
                        message_id=msg.get("mid", ""),
                        user_id=str(ev["sender"]["id"]),
                        business_id=business_id,
                        text=text,
                        timestamp=_ts(ev.get("timestamp")),
                        payload=qr,
                        raw=ev,
                    )
                )
            elif postback:
                out.append(
                    InboundMessage(
                        channel=channel,
                        kind=MessageKind.DIRECT,
                        message_id=postback.get("mid") or f"pb-{ev.get('timestamp')}",
                        user_id=str(ev["sender"]["id"]),
                        business_id=business_id,
                        text=postback.get("title", ""),
                        timestamp=_ts(ev.get("timestamp")),
                        payload=postback.get("payload"),
                        raw=ev,
                    )
                )
    return out


# ── Instagram comments ────────────────────────────────────────────────────────


def _parse_ig_comments(payload: dict[str, Any]) -> list[InboundMessage]:
    out: list[InboundMessage] = []
    for entry in payload.get("entry", []):
        ig_account_id = str(entry.get("id", ""))
        for change in entry.get("changes", []):
            if change.get("field") not in {"comments", "live_comments"}:
                continue
            v = change.get("value", {})
            author = v.get("from", {})
            if str(author.get("id")) == ig_account_id:
                continue  # our own reply showing up as a comment - never answer ourselves
            out.append(
                InboundMessage(
                    channel=Channel.INSTAGRAM,
                    kind=MessageKind.COMMENT,
                    message_id=str(v.get("id", "")),
                    user_id=str(author.get("id", "")),
                    business_id=ig_account_id,
                    text=v.get("text", ""),
                    timestamp=_ts(entry.get("time")),
                    user_name=author.get("username"),
                    media_id=(v.get("media") or {}).get("id"),
                    parent_comment_id=v.get("parent_id"),
                    raw=v,
                )
            )
    return out
