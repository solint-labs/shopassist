"""Daily privacy maintenance. Schedule it (cron, Kubernetes CronJob, Cloud Scheduler...).

    python -m scripts.retention                 # apply retention windows
    python -m scripts.retention --rotate-keys   # re-encrypt the vault with the newest key
    python -m scripts.retention --release-idle  # hand idle human-owned chats back to the bot
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone

from app.agent.memory import ConversationStore
from app.logging_config import configure_logging
from app.main import build_vault
from app.privacy.compliance import PrivacyService
from app.settings import get_settings
from app.storage.db import Database


async def main(rotate: bool, release_idle: bool) -> None:
    s = get_settings()
    configure_logging(s.log_level, s.log_json)
    db = Database(s.database_url)
    await db.create_all()
    store, vault = ConversationStore(db), build_vault(s, db)
    print(await PrivacyService(store, vault).apply_retention(s.retention_days_messages, s.retention_days_pii))
    if rotate:
        print({"re_encrypted": await vault.rotate()})
    if release_idle:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=s.human_auto_release_hours)
        released = 0
        for conv in await store.list_by_status("human", limit=10_000):
            updated = conv.updated_at if conv.updated_at.tzinfo else conv.updated_at.replace(tzinfo=timezone.utc)
            if updated < cutoff:
                await store.set_status(conv.id, "bot")
                released += 1
        print({"released": released})
    await db.dispose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--rotate-keys", action="store_true")
    ap.add_argument("--release-idle", action="store_true")
    a = ap.parse_args()
    asyncio.run(main(a.rotate_keys, a.release_idle))
