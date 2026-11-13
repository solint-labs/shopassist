"""Chat with the agent in your terminal - no Meta app, no webhook, no tunnel needed.

    python -m scripts.chat_cli --channel whatsapp
    python -m scripts.chat_cli --channel instagram --comment   # simulate a public comment

Uses whatever LLM is configured in .env (LLM_PROVIDER / LLM_MODEL / LLM_API_KEY).
Replies are printed exactly as they would be formatted for the chosen channel.
"""

from __future__ import annotations

import argparse
import asyncio
import uuid

from app.agent.memory import ConversationStore
from app.agent.orchestrator import Agent
from app.agent.tenant import load_tenant_config
from app.channels.formatting import format_for_channel
from app.domain import Channel, InboundMessage, MessageKind
from app.llm.factory import build_llm
from app.logging_config import configure_logging
from app.settings import get_settings
from app.storage.db import Database


async def main(channel: Channel, comment: bool, user: str) -> None:
    s = get_settings()
    configure_logging("WARNING")
    db = Database(s.database_url)
    await db.create_all()
    agent = Agent(build_llm(s), ConversationStore(db), load_tenant_config(s.tenant_config_path), s.history_turns)
    kind = MessageKind.COMMENT if comment else MessageKind.DIRECT
    print(f"Chatting as '{user}' on {channel} ({kind}). Ctrl+C to exit.\n")
    while True:
        try:
            text = input("you › ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        msg = InboundMessage(channel=channel, kind=kind, message_id=uuid.uuid4().hex, user_id=user,
                             business_id="local", text=text, user_name=user)
        reply = await agent.respond(msg)
        print(f"bot › {format_for_channel(reply.text, channel, kind)}")
        if reply.private_text:
            print(f"bot (DM) › {format_for_channel(reply.private_text, channel)}")
        print()
    await db.dispose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", choices=[c.value for c in Channel], default="whatsapp")
    ap.add_argument("--comment", action="store_true", help="simulate an Instagram comment")
    ap.add_argument("--user", default="local-tester")
    a = ap.parse_args()
    asyncio.run(main(Channel(a.channel), a.comment, a.user))
