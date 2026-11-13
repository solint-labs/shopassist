"""Durable inbound queue on Redis Streams, plus the worker that drains it.

Why a queue? The webhook must answer Meta in well under a few seconds; an agent turn
(retrieval + LLM + tools) can take 3-10 s and occasionally fails. Decoupling them:
  * webhooks stay fast and never time out, whatever the LLM is doing,
  * API pods and worker pods scale independently (workers scale on queue depth),
  * a crashed worker loses nothing: unacknowledged entries are reclaimed,
  * poison messages go to a dead-letter stream after N attempts instead of looping.

Semantics: at-least-once delivery + idempotent processing (dedupe on message id)
= effectively-once replies.

Ordering: a consumer group spreads entries across workers, so two messages from one
customer could reach two workers. The per-conversation lock serialises them. If you
need strict ordering, partition streams by hash(conversation_key) and pin one consumer
per partition (Kafka-style).
"""

from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import asdict
from datetime import datetime
from typing import Any

from redis.asyncio import Redis

from app.domain import Channel, InboundMessage, MessageKind
from app.logging_config import log
from app.observability import metrics

STREAM = "shopassist:inbound"
DLQ = "shopassist:inbound:dead"
GROUP = "workers"


def serialize(msg: InboundMessage) -> dict[str, str]:
    d = asdict(msg)
    d["timestamp"] = msg.timestamp.isoformat()
    d.pop("raw", None)  # never persist raw platform payloads in the queue
    return {"msg": json.dumps(d, default=str)}


def deserialize(fields: dict[bytes | str, Any]) -> InboundMessage:
    raw = fields.get(b"msg") or fields.get("msg")
    d = json.loads(raw)
    d["channel"], d["kind"] = Channel(d["channel"]), MessageKind(d["kind"])
    d["timestamp"] = datetime.fromisoformat(d["timestamp"])
    return InboundMessage(**d)


class InboundQueue:
    def __init__(self, redis: Redis, stream: str = STREAM, maxlen: int = 1_000_000):
        self.redis, self.stream, self.maxlen = redis, stream, maxlen

    async def ensure_group(self) -> None:
        try:
            await self.redis.xgroup_create(self.stream, GROUP, id="0", mkstream=True)
        except Exception as e:  # noqa: BLE001 - BUSYGROUP: already exists
            if "BUSYGROUP" not in str(e):
                raise

    async def enqueue(self, msg: InboundMessage) -> str:
        entry_id = await self.redis.xadd(self.stream, serialize(msg), maxlen=self.maxlen, approximate=True)
        return entry_id.decode() if isinstance(entry_id, bytes) else entry_id


class Worker:
    """Consume the stream and run each message through the pipeline.

    Run as many replicas as you like: `python -m app.worker`. Each has its own consumer
    name; Redis hands every entry to exactly one consumer at a time.
    """

    def __init__(self, redis: Redis, pipeline, stream: str = STREAM, consumer: str | None = None,
                 batch: int = 10, block_ms: int = 5000, visibility_ms: int = 120_000, max_deliveries: int = 5,
                 concurrency: int = 8):
        self.redis, self.pipeline, self.stream = redis, pipeline, stream
        self.consumer = consumer or f"{socket.gethostname()}-{id(self) & 0xffff:x}"
        self.batch, self.block_ms, self.visibility_ms = batch, block_ms, visibility_ms
        self.max_deliveries = max_deliveries
        self.sem = asyncio.Semaphore(concurrency)
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        await InboundQueue(self.redis, self.stream).ensure_group()
        log.info("worker.started", consumer=self.consumer)
        while not self._stop.is_set():
            await self.run_once()

    async def run_once(self) -> int:
        """One poll: reclaim stuck entries, then read new ones. Returns entries handled."""
        handled = 0
        # 1. Entries another worker took but never acked (it crashed) come back to us.
        _, claimed, *_ = await self.redis.xautoclaim(self.stream, GROUP, self.consumer,
                                                     min_idle_time=self.visibility_ms, start_id="0-0", count=self.batch)
        # 2. New entries.
        resp = await self.redis.xreadgroup(GROUP, self.consumer, {self.stream: ">"}, count=self.batch,
                                           block=self.block_ms)
        fresh = resp[0][1] if resp else []
        entries = list(claimed) + list(fresh)
        await asyncio.gather(*(self._handle(eid, fields) for eid, fields in entries))
        handled += len(entries)
        info = await self.redis.xpending(self.stream, GROUP)
        metrics.QUEUE_DEPTH.labels(self.stream).set(info["pending"] if isinstance(info, dict) else 0)
        return handled

    async def _handle(self, entry_id, fields) -> None:
        async with self.sem:
            try:
                msg = deserialize(fields)
                await self.pipeline.process(msg)
                await self.redis.xack(self.stream, GROUP, entry_id)
            except Exception as e:  # noqa: BLE001
                deliveries = await self._deliveries(entry_id)
                log.exception("worker.failed", entry=str(entry_id), deliveries=deliveries, error=str(e)[:200])
                if deliveries >= self.max_deliveries:
                    await self.redis.xadd(DLQ, {**{k: v for k, v in fields.items()}, "error": str(e)[:500]})
                    await self.redis.xack(self.stream, GROUP, entry_id)
                    metrics.DEAD_LETTERS.inc()
                # otherwise leave it un-acked: xautoclaim retries it after the visibility timeout

    async def _deliveries(self, entry_id) -> int:
        rows = await self.redis.xpending_range(self.stream, GROUP, entry_id, entry_id, 1)
        return int(rows[0]["times_delivered"]) if rows else 1
