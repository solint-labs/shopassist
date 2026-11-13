"""Prometheus metrics.

The numbers a support-automation team actually runs on:
  * containment: share of conversations resolved without a human (replies vs escalations)
  * escalations by reason: tells you which knowledge or flows to build next
  * grounding: answers with valid citations vs ungrounded/"don't know"
  * latency: end-to-end and per LLM provider (p95 matters on chat channels)
  * cost proxy: tokens in/out per provider
  * health: queue lag, dead letters, open circuits

Scrape GET /metrics. Labels stay low-cardinality on purpose (no user/conversation ids).
"""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

MESSAGES = Counter("shopassist_messages_total", "Inbound messages", ["tenant", "channel", "kind"])
REPLIES = Counter("shopassist_replies_total", "Outbound replies by outcome",
                  ["tenant", "channel", "outcome"])  # answered | escalated | flow | fallback | silent
ESCALATIONS = Counter("shopassist_escalations_total", "Hand-offs to humans", ["tenant", "reason"])
GROUNDED = Counter("shopassist_answers_total", "Answers by grounding", ["tenant", "grounded"])
TOOL_CALLS = Counter("shopassist_tool_calls_total", "Tool calls", ["tool"])
TURN_LATENCY = Histogram("shopassist_turn_seconds", "End-to-end agent turn latency", ["channel"],
                         buckets=(0.5, 1, 2, 3, 5, 8, 13, 21))
LLM_LATENCY = Histogram("shopassist_llm_seconds", "LLM call latency", ["provider"],
                        buckets=(0.25, 0.5, 1, 2, 4, 8, 16, 32))
LLM_TOKENS = Counter("shopassist_llm_tokens_total", "LLM tokens", ["provider", "direction"])
LLM_ERRORS = Counter("shopassist_llm_errors_total", "LLM provider failures", ["provider"])
CIRCUIT_STATE = Gauge("shopassist_circuit_open", "1 if the circuit breaker is open", ["name"])
QUEUE_DEPTH = Gauge("shopassist_queue_pending", "Messages waiting or in-flight", ["stream"])
DEAD_LETTERS = Counter("shopassist_dead_letters_total", "Messages moved to the dead-letter stream")
DUPLICATES = Counter("shopassist_duplicate_webhooks_total", "Webhook deliveries dropped as duplicates")


def render() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
