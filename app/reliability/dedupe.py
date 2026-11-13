"""Webhook de-duplication and per-conversation locks.

Meta delivers webhooks *at least once*. Retries happen on slow responses, deploys,
network blips... Processing a message twice means answering twice, or worse,
cancelling twice. So every message id is claimed exactly once with an atomic
`SET key 1 NX EX ttl` in Redis, shared by every API replica and worker.

The per-conversation lock serialises turns for one customer across workers, so two
quick messages ("hi" / "where's my order") can't be answered concurrently with
interleaved state.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Protocol

from redis.asyncio import Redis


class Dedupe(Protocol):
    async def claim(self, key: str) -> bool: ...


class MemoryDedupe:
    """Single-process fallback (dev/tests)."""

    def __init__(self, ttl_s: float = 86400, max_items: int = 100_000):
        self.ttl_s, self.max_items = ttl_s, max_items
        self._seen: OrderedDict[str, float] = OrderedDict()

    async def claim(self, key: str) -> bool:
        now = time.monotonic()
        while self._seen and (now - next(iter(self._seen.values())) > self.ttl_s or len(self._seen) > self.max_items):
            self._seen.popitem(last=False)
        if key in self._seen:
            return False
        self._seen[key] = now
        return True


class RedisDedupe:
    def __init__(self, redis: Redis, ttl_s: int = 86400, prefix: str = "shopassist:seen:"):
        self.redis, self.ttl_s, self.prefix = redis, ttl_s, prefix

    async def claim(self, key: str) -> bool:
        return bool(await self.redis.set(self.prefix + key, b"1", nx=True, ex=self.ttl_s))

    async def release(self, key: str) -> None:
        """Un-claim so a redelivery can be processed (used when processing failed)."""
        await self.redis.delete(self.prefix + key)


_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end
"""


class ConversationLocks:
    """Distributed mutex per conversation (SET NX PX + token-checked release)."""

    def __init__(self, redis: Redis | None, ttl_ms: int = 60_000, wait_s: float = 30.0):
        self.redis, self.ttl_ms, self.wait_s = redis, ttl_ms, wait_s
        self._local: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def hold(self, conversation_key: str):
        if self.redis is None:
            lock = self._local.setdefault(conversation_key, asyncio.Lock())
            async with lock:
                yield
            return
        key, token = f"shopassist:lock:{conversation_key}", uuid.uuid4().hex
        deadline = time.monotonic() + self.wait_s
        while not await self.redis.set(key, token, nx=True, px=self.ttl_ms):
            if time.monotonic() > deadline:
                raise TimeoutError(f"conversation busy: {conversation_key}")
            await asyncio.sleep(0.05)
        try:
            yield
        finally:
            await self.redis.eval(_RELEASE, 1, key, token)
