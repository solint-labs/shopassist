"""Retries with backoff, circuit breakers, and a fallback chain of LLM providers.

Failure modes we design for:
  * transient errors (429, 5xx, timeouts)   -> retry with exponential backoff + jitter
  * a provider being down for minutes       -> circuit breaker stops hammering it, and
                                               we fail over to the next provider at once
  * every provider down                     -> raise; the pipeline sends the fallback
                                               message and the customer is never ignored

The fallback chain can mix vendors and self-hosted models, e.g.
  claude-haiku (primary) -> gpt-class model (secondary) -> local open-weights via vLLM (last resort)
so a single vendor outage never takes support down.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from app.llm.base import ChatMessage, LLMClient, LLMError, LLMResponse, ToolSpec
from app.logging_config import log
from app.observability import metrics

T = TypeVar("T")


async def retry_async(fn: Callable[[], Awaitable[T]], *, attempts: int = 3, base_delay: float = 0.5,
                      max_delay: float = 8.0, retry_if: Callable[[Exception], bool] = lambda e: True) -> T:
    """Full-jitter exponential backoff (AWS architecture blog's recommended variant)."""
    for i in range(attempts):
        try:
            return await fn()
        except Exception as e:  # noqa: BLE001
            if i == attempts - 1 or not retry_if(e):
                raise
            await asyncio.sleep(random.uniform(0, min(max_delay, base_delay * 2 ** i)))
    raise RuntimeError("unreachable")


class CircuitBreaker:
    """closed -> (N consecutive failures) -> open -> (cooldown) -> half-open -> closed/open."""

    def __init__(self, name: str, failure_threshold: int = 5, cooldown_s: float = 30.0):
        self.name, self.threshold, self.cooldown = name, failure_threshold, cooldown_s
        self.failures = 0
        self.opened_at: float | None = None

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half_open" if time.monotonic() - self.opened_at >= self.cooldown else "open"

    def allow(self) -> bool:
        return self.state != "open"

    def record_success(self) -> None:
        self.failures, self.opened_at = 0, None
        metrics.CIRCUIT_STATE.labels(self.name).set(0)

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold or self.state == "half_open":
            self.opened_at = time.monotonic()
            metrics.CIRCUIT_STATE.labels(self.name).set(1)
            log.warning("circuit.open", name=self.name, failures=self.failures)


class ResilientLLM:
    """An LLMClient that retries, breaks circuits and fails over across providers."""

    name = "resilient"

    def __init__(self, clients: list[LLMClient], attempts_per_client: int = 2, breaker_threshold: int = 5,
                 breaker_cooldown_s: float = 30.0, timeout_s: float = 45.0):
        if not clients:
            raise ValueError("at least one LLM client is required")
        self.clients = clients
        self.breakers = {id(c): CircuitBreaker(f"llm:{c.name}", breaker_threshold, breaker_cooldown_s) for c in clients}
        self.attempts, self.timeout = attempts_per_client, timeout_s

    async def chat(self, system: str, messages: list[ChatMessage], tools: list[ToolSpec] | None = None,
                   temperature: float | None = None, max_tokens: int | None = None,
                   json_mode: bool = False) -> LLMResponse:
        last_error: Exception | None = None
        for client in self.clients:
            breaker = self.breakers[id(client)]
            if not breaker.allow():
                continue
            started = time.perf_counter()
            try:
                resp = await retry_async(
                    lambda c=client: asyncio.wait_for(
                        c.chat(system, messages, tools, temperature, max_tokens, json_mode), self.timeout),
                    attempts=self.attempts,
                    retry_if=lambda e: isinstance(e, asyncio.TimeoutError) or getattr(e, "retryable", False))
            except Exception as e:  # noqa: BLE001
                breaker.record_failure()
                metrics.LLM_ERRORS.labels(client.name).inc()
                log.warning("llm.failover", provider=client.name, error=str(e)[:200])
                last_error = e
                continue
            breaker.record_success()
            metrics.LLM_LATENCY.labels(client.name).observe(time.perf_counter() - started)
            metrics.LLM_TOKENS.labels(client.name, "in").inc(resp.input_tokens)
            metrics.LLM_TOKENS.labels(client.name, "out").inc(resp.output_tokens)
            return resp
        raise LLMError(f"all LLM providers failed: {last_error}", retryable=True, provider="resilient")
