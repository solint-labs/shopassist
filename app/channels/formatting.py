"""Per-channel output formatting.

LLMs love Markdown. Chat apps mostly don't render it:
  * WhatsApp has its own light syntax: *bold*, _italic_, ~strike~, ```mono```
  * Messenger and Instagram render plain text only
and each channel has a hard length limit. We normalise here so the prompt can stay
channel-neutral and the model never has to count characters.
"""

from __future__ import annotations

import re

from app.domain import Channel, MessageKind

# Hard limits documented by Meta (text bodies). We keep a little headroom.
MAX_CHARS = {
    Channel.WHATSAPP: 4000,  # 4096 max
    Channel.MESSENGER: 1900,  # 2000 max
    Channel.INSTAGRAM: 990,  # 1000 max for DMs
}
COMMENT_MAX_CHARS = 300  # public replies should be short, even though IG allows more

_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_HEADING = re.compile(r"^#{1,6}\s*", re.MULTILINE)
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
_MD_BULLET = re.compile(r"^\s*[-*]\s+", re.MULTILINE)


def format_for_channel(text: str, channel: Channel, kind: MessageKind = MessageKind.DIRECT) -> str:
    text = text.strip()
    text = _MD_HEADING.sub("", text)
    text = _MD_LINK.sub(r"\1: \2", text)  # links must be visible URLs in chat apps
    text = _MD_BULLET.sub("• ", text)
    if channel == Channel.WHATSAPP:
        text = _MD_BOLD.sub(r"*\1*", text)  # **bold** -> *bold*
    else:
        text = _MD_BOLD.sub(r"\1", text)  # no rich text: drop markers
    limit = COMMENT_MAX_CHARS if kind == MessageKind.COMMENT else MAX_CHARS[channel]
    return truncate(text, limit)


def truncate(text: str, limit: int) -> str:
    """Cut at a sentence boundary when possible, never mid-word."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in (". ", "! ", "? ", "\n"):
        idx = cut.rfind(sep)
        if idx > limit * 0.6:
            return cut[: idx + 1].strip()
    return cut.rsplit(" ", 1)[0].rstrip(",;:") + "…"
