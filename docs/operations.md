# Operating ShopAssist in production

## Topology

```
Meta ──HTTPS──► api (N replicas) ──XADD──► Redis Stream ──XREADGROUP──► worker (M replicas)
                   │                                                      │
                   ├─ /agent, /admin (RBAC, audit)                        ├─ LLM chain (primary → fallbacks)
                   ├─ /webhooks/shopify/compliance                        ├─ Qdrant (per-tenant collections)
                   └─ /metrics /readyz /healthz                           ├─ Shopify Admin GraphQL
                                                                          └─ Graph API (replies)
                       Postgres: conversations, messages, vault, profiles, handoffs, api_keys, audit_log
```

## Golden signals and alerts

| Metric | Alert when | Usually means |
|---|---|---|
| `shopassist_queue_pending` | > 200 for 5 min | workers saturated or an LLM is slow; scale out |
| `shopassist_dead_letters_total` | any increase | a bug or a poison payload; inspect `shopassist:inbound:dead` |
| `shopassist_circuit_open{name=~"llm:.*"}` | = 1 | primary LLM down; traffic is on the fallbacks |
| `shopassist_replies_total{outcome="fallback"}` | > 1% of replies | every LLM in the chain is failing |
| `histogram_quantile(0.95, shopassist_turn_seconds)` | > 8 s | slow model/reranker; check `shopassist_llm_seconds` |
| containment (`/admin/analytics`) | drops > 10 pts week over week | a KB gap or a broken flow; look at escalations by reason |

## Runbooks

**Dead letters.** `XRANGE shopassist:inbound:dead - + COUNT 20` shows the payload and the error. After fixing,
re-enqueue with `XADD shopassist:inbound * msg <payload>`. Dedupe keys were released on failure, so the
message processes once.

**LLM outage.** Nothing to do if fallbacks are configured: the circuit opens after `LLM_BREAKER_THRESHOLD`
failures and re-tests the primary after `LLM_BREAKER_COOLDOWN_S`. Without fallbacks, customers get the
fallback message with the support email. Keep at least one fallback in a different vendor or self-hosted.

**Key rotation (PII vault).** Prepend the new key to `PII_ENCRYPTION_KEYS`, deploy, run
`python -m scripts.retention --rotate-keys`, then remove the old key. Never rotate `PII_BLIND_INDEX_KEY`
casually: it re-keys GDPR lookups.

**Bootstrap admin.** Use `ADMIN_BOOTSTRAP_KEY` once to create named admin/supervisor/agent keys via
`POST /admin/api-keys`, then unset it.

**Meta Graph API version.** Bump `META_GRAPH_VERSION` about once a year and run the test suite. Meta keeps
each version for about two years after the next one is released.

## Scaling notes

- API pods are cheap. They verify, parse and enqueue. Size them for webhook bursts, not for LLM latency.
- Worker throughput ≈ replicas × `WORKER_CONCURRENCY` / mean turn time. The cross-encoder reranker is
  CPU-bound: give workers real CPU, or move reranking to a GPU inference server.
- Retrieval is per tenant (one Qdrant collection each). Use Qdrant replication for HA. Point-in-time
  snapshots make re-indexing safe.
- Postgres: add Alembic migrations before your first schema change in production (`create_all` only
  creates missing tables).
