"""Part 4: retries, circuit breakers, LLM failover, Redis dedupe/locks, the stream worker, queue mode."""

import asyncio

import fakeredis
import pytest

from app.domain import Channel, InboundMessage, MessageKind
from app.llm.base import LLMError, LLMResponse
from app.llm.fake import FakeLLM
from app.main import create_app
from app.reliability.dedupe import ConversationLocks, RedisDedupe
from app.reliability.queue import DLQ, GROUP, STREAM, InboundQueue, Worker, deserialize, serialize
from app.reliability.resilience import CircuitBreaker, ResilientLLM, retry_async
from tests import payloads as p
from tests.conftest import sent_bodies, signed


class FlakyLLM(FakeLLM):
    def __init__(self, name, fail_times):
        super().__init__()
        self.name, self.fail_times, self.n = name, fail_times, 0

    async def chat(self, *a, **kw):
        self.n += 1
        if self.n <= self.fail_times:
            raise LLMError("503 overloaded", retryable=True, provider=self.name)
        return LLMResponse(text=f"answer from {self.name}", model=self.name)


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    real_sleep = asyncio.sleep

    async def fast(delay, *a, **kw):
        await real_sleep(0)
    monkeypatch.setattr("app.reliability.resilience.asyncio.sleep", fast)


async def test_retry_then_success():
    llm = ResilientLLM([FlakyLLM("primary", 1)], attempts_per_client=2)
    assert (await llm.chat("s", [])).text == "answer from primary"


async def test_failover_to_secondary_and_circuit_opens():
    primary, secondary = FlakyLLM("primary", 999), FlakyLLM("secondary", 0)
    llm = ResilientLLM([primary, secondary], attempts_per_client=2, breaker_threshold=2)
    for _ in range(3):
        assert (await llm.chat("s", [])).text == "answer from secondary"
    # After two failed turns the breaker opened: the third turn skipped the primary entirely.
    assert primary.n == 4
    assert llm.breakers[id(primary)].state == "open"


async def test_all_providers_down_raises():
    with pytest.raises(LLMError):
        await ResilientLLM([FlakyLLM("a", 99)], attempts_per_client=1).chat("s", [])


def test_breaker_half_opens_after_cooldown():
    b = CircuitBreaker("x", failure_threshold=1, cooldown_s=0)
    b.record_failure()
    assert b.state == "half_open" and b.allow()
    b.record_success()
    assert b.state == "closed"


async def test_retry_async_respects_predicate():
    calls = 0

    async def boom():
        nonlocal calls
        calls += 1
        raise ValueError("permanent")
    with pytest.raises(ValueError):
        await retry_async(boom, attempts=5, retry_if=lambda e: not isinstance(e, ValueError))
    assert calls == 1


# ── Redis primitives ─────────────────────────────────────────────────────────

async def test_redis_dedupe_and_release():
    r = fakeredis.FakeAsyncRedis()
    d = RedisDedupe(r)
    assert await d.claim("wamid.1") and not await d.claim("wamid.1")
    await d.release("wamid.1")
    assert await d.claim("wamid.1")


async def test_conversation_lock_serialises_turns():
    locks, order = ConversationLocks(fakeredis.FakeAsyncRedis()), []

    async def turn(name):
        async with locks.hold("whatsapp:1:d:9198"):
            order.append(f"{name}-start")
            await asyncio.sleep(0.01)
            order.append(f"{name}-end")
    await asyncio.gather(turn("a"), turn("b"))
    assert order in (["a-start", "a-end", "b-start", "b-end"], ["b-start", "b-end", "a-start", "a-end"])


# ── Stream worker ────────────────────────────────────────────────────────────

def _msg(i=1):
    return InboundMessage(channel=Channel.WHATSAPP, kind=MessageKind.DIRECT, message_id=f"wamid.{i}",
                          user_id="919800000001", business_id=p.WA_PHONE_ID, text="hi")


def test_serialization_roundtrip():
    m = _msg()
    m.raw = {"secret": "never queued"}
    back = deserialize(serialize(m))
    assert back.text == "hi" and back.channel == Channel.WHATSAPP and back.raw is None


class StubPipeline:
    def __init__(self, fail=False):
        self.fail, self.seen = fail, []

    async def process(self, msg):
        self.seen.append(msg.message_id)
        if self.fail:
            raise RuntimeError("downstream exploded")


async def test_worker_processes_and_acks():
    r = fakeredis.FakeAsyncRedis()
    q = InboundQueue(r)
    await q.ensure_group()
    await q.enqueue(_msg(1))
    await q.enqueue(_msg(2))
    pipe = StubPipeline()
    w = Worker(r, pipe, block_ms=1)
    assert await w.run_once() == 2 and pipe.seen == ["wamid.1", "wamid.2"]
    assert (await r.xpending(STREAM, GROUP))["pending"] == 0


async def test_poison_message_goes_to_dead_letter():
    r = fakeredis.FakeAsyncRedis()
    q = InboundQueue(r)
    await q.ensure_group()
    await q.enqueue(_msg(3))
    w = Worker(r, StubPipeline(fail=True), block_ms=1, visibility_ms=0, max_deliveries=3)
    for _ in range(4):
        await w.run_once()
    assert await r.xlen(DLQ) == 1
    assert (await r.xpending(STREAM, GROUP))["pending"] == 0


async def test_crashed_worker_entries_are_reclaimed():
    r = fakeredis.FakeAsyncRedis()
    q = InboundQueue(r)
    await q.ensure_group()
    await q.enqueue(_msg(4))
    await r.xreadgroup(GROUP, "dead-worker", {STREAM: ">"}, count=1)  # taken, never acked
    pipe = StubPipeline()
    await Worker(r, pipe, consumer="live", block_ms=1, visibility_ms=0).run_once()
    assert pipe.seen == ["wamid.4"]


async def test_queue_mode_webhook_enqueues_and_worker_replies(settings, fake_llm, graph_mock):
    settings.queue_mode = "redis"
    r = fakeredis.FakeAsyncRedis()
    app = create_app(settings, llm=fake_llm, redis=r)
    import httpx
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            body, headers = signed(p.whatsapp_text("Do you ship to Pune?", wamid="wamid.q1"))
            assert (await c.post("/webhooks/meta", content=body, headers=headers)).status_code == 200
            await asyncio.sleep(0)
            assert sent_bodies(graph_mock) == []  # nothing processed inline
            assert await r.xlen(STREAM) == 1
            await Worker(r, app.state.pipeline, block_ms=1).run_once()
            assert any(b.get("type") == "text" for _, b in sent_bodies(graph_mock))
            # Meta redelivers the same webhook: deduped across processes via Redis
            await c.post("/webhooks/meta", content=body, headers=headers)
            n = len(sent_bodies(graph_mock))
            await Worker(r, app.state.pipeline, block_ms=1).run_once()
            assert len(sent_bodies(graph_mock)) == n


async def test_health_ready_and_metrics(client):
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    ready = await client.get("/readyz")
    assert ready.status_code == 200 and ready.json()["checks"]["database"] == "ok"
    body, headers = signed(p.messenger_text("hello", mid="m_metrics"))
    await client.post("/webhooks/meta", content=body, headers=headers)
    for _ in range(10):
        await asyncio.sleep(0)
    text = (await client.get("/metrics")).text
    assert "shopassist_messages_total" in text and "shopassist_replies_total" in text
