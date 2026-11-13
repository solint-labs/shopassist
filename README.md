# ShopAssist

An LLM-powered customer-support agent for a Shopify store. It answers on **WhatsApp**,
**Facebook Messenger** and **Instagram**, including DMs and public comments.

This is the companion code to a four-part article series. Each part is a git tag, so you
can check out the code exactly as it stood at the end of that article:

| Tag      | Article                                   | What the agent can do                                                        |
|----------|-------------------------------------------|------------------------------------------------------------------------------|
| `part-1` | Foundations: one brain, three channels    | Talk on WhatsApp, Messenger and IG (DMs + comments), with persona and memory |
| `part-2` | Grounded answers: RAG + Shopify           | Answer from the store's knowledge base and look up live orders and products  |
| `part-3` | Trust: escalation, flows and privacy      | Hand off to humans, run return/cancel flows, redact PII, honour GDPR         |
| `part-4` | Production: scale, enterprise, persona    | Queue workers, multi-tenant, RBAC/audit, per-customer tone, evals            |

```bash
git checkout part-1   # then follow the README at that tag
```

The running example is **Brewline Coffee Co.**, a fictional specialty-coffee store
(see `config/tenants/brewline.yaml`).

---

## Part 1: what's in this stage

```
Meta webhook ──► verify signature ──► parse to InboundMessage ──► background task
                                                                   │
                     ┌─────────────────────────────────────────────┘
                     ▼
     dedupe ─► typing indicator ─► Agent (persona prompt + history ─► LLM) ─► format per channel ─► Graph API
```

- `app/channels/`: webhook verification, payload parsing for all channels, per-channel formatting and delivery
- `app/llm/`: one interface over OpenAI-compatible APIs (incl. vLLM/Ollama), Anthropic, and a fake for tests
- `app/agent/`: tenant/persona config, sectioned system prompt, DB-backed memory, the turn orchestrator
- Instagram comments: a structured triage call decides between a public reply and a private reply (DM)

## Part 2: what's added

```
history ─► condense follow-up ─► hybrid search (BGE dense + BM25 sparse, RRF in Qdrant)
        ─► cross-encoder rerank ─► relevance threshold ─► numbered <sources> in prompt
        ─► LLM ⇄ tools (get_order_status, search_products via Shopify GraphQL)
        ─► validate + strip citations ─► append policy link
```

- `app/rag/`: heading-aware chunking, fastembed encoders, Qdrant hybrid store, retriever with threshold, grounding
- `app/commerce/`: Shopify client (client-credentials auth, throttling) and tools with identity checks in code
- `knowledge/brewline/`: the sample help center; `scripts/ingest.py` indexes it plus Shopify policies, pages and products
- Setup: [docs/shopify-setup.md](docs/shopify-setup.md)

## Part 3: what's added

```
inbound ─► pseudonymise PII (<EMAIL_1>, encrypted vault) ─► human owns thread? → stay silent
        ─► escalation rules (safety · legal · human request · frustration · repeated failure)
        ─► active flow? (cancel order · return request · forget me): code, not the LLM
        ─► RAG + LLM ⇄ tools (+ start_* flow tools, escalate_to_human)
        ─► rehydrate placeholders only for delivery to this customer
```

- `app/privacy/`: regex + Luhn + Presidio detection, Fernet vault with blind index and key rotation,
  log redaction, Shopify GDPR webhooks (`customers/data_request`, `customers/redact`, `shop/redact`), retention
- `app/escalation/`: rule-based detector with business hours, hand-off with LLM summary, Zendesk/webhook helpdesk,
  Meta handover protocol (Page/IG inbox), and a human-agent API (`/agent/...`) that respects messaging windows
- `app/flows/`: a small flow engine plus cancel-order, return-request and forget-me flows with confirmation buttons
- Instagram moderation: hide spam/abuse and comments that expose personal data, then move to DM
- `scripts/retention.py`: daily retention, key rotation, auto-release of idle hand-offs

## Part 4: what's added

```
webhook ─► verify ─► resolve tenant ─► Redis Stream ─► worker: dedupe ─► conversation lock
        ─► agent turn (LLM chain with retries, circuit breakers, failover) ─► deliver with retries
        ─► metrics, audit
persona = tenant ▸ channel override ▸ A/B variant ▸ customer profile  ─►  StyleGuard enforces it
```

- `app/reliability/`: Redis dedupe and locks, a Streams queue with reclaim and dead-letter, `ResilientLLM`
- `app/tenancy/`: many stores per deployment; routing by Meta asset id; secrets as `env:`/`file:` references
- `app/enterprise/`: API keys with roles (admin, supervisor, agent, analyst, auditor) and tenant scope,
  an audit log, and admin/analytics APIs
- `app/personalization/`: customer profiles (language, tone, length, emoji, VIP), A/B voice variants,
  few-shot brand examples, StyleGuard
- `app/observability/`: Prometheus metrics, `/readyz`
- `evals/`: golden conversations and a runner (`python -m evals.run`). Use it as the CI gate.
- `deploy/k8s/`, `docker-compose.yml` (api + workers + Redis + Postgres + Qdrant), [docs/operations.md](docs/operations.md)

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # set LLM_PROVIDER / LLM_MODEL / LLM_API_KEY

make test                       # runs offline with the fake LLM
make chat                       # talk to the agent in your terminal
make run                        # start the API on :8000
python -m scripts.send_test_webhook whatsapp "Do you ship to Pune?"   # signed sample webhook
```

To connect real channels, see [docs/meta-setup.md](docs/meta-setup.md).

## Deploy

`docker compose up --build` runs the full stack: API, workers, Postgres, Redis and Qdrant.
For Kubernetes, see `deploy/k8s/shopassist.yaml` (HPA for the API, KEDA scaling for workers
on queue backlog, a retention CronJob). The image is a slim, non-root, multi-stage build.
Put it behind HTTPS (Meta only calls HTTPS webhooks). Operating guide: [docs/operations.md](docs/operations.md).

```bash
make test                               # 110+ tests, offline
python -m evals.run                     # golden conversations against your real LLM
```

## License

MIT
