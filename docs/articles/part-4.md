# Part 4: Production: Reliability, Scale, Enterprise and Personality

*Building a production LLM virtual agent for e-commerce support, Part 4 of 4*

> **Series:** Part 1: Foundations · Part 2: Grounded answers (RAG + Shopify) · Part 3: Trust (escalation, flows, privacy) · **Part 4: Production (scale, enterprise, personality)**
>
> **Code:** [github.com/solint-labs/shopassist](https://github.com/solint-labs/shopassist), `git checkout part-4` (also `main`)

Parts 1-3 built an agent that does the right thing. This part makes sure it *keeps* doing the right thing at volume, through provider outages and deploys, for many stores at once, under the controls an enterprise security review will ask about, and in a voice each customer actually wants.

```
webhook ─► verify ─► resolve tenant ─► Redis Stream ─► worker: dedupe ─► per-conversation lock
        ─► agent turn (LLM chain: retries, circuit breakers, failover) ─► deliver with retries
        ─► metrics + audit
persona = tenant ▸ channel override ▸ A/B variant ▸ customer profile  ─►  StyleGuard enforces it
```

## 1. Reliability: designing for failure

The failure modes are predictable, so we design for each one:

| Failure | Design response |
|---|---|
| Meta redelivers a webhook | Idempotency: claim each message id once, in Redis, across all replicas |
| An agent turn takes 3-10 s; webhooks must return fast | Durable queue between the webhook and the agent |
| A worker crashes mid-turn | Unacknowledged entries are reclaimed by another worker |
| A message crashes the agent every time | Dead-letter after N attempts instead of looping forever |
| Two quick messages from one customer | Per-conversation distributed lock serialises turns |
| LLM provider throttles or goes down | Retries with jittered backoff, circuit breaker, failover to the next provider |
| Graph API throttling on reply | Retry the delivery only. Never re-run the LLM turn |
| Everything is down | The fallback message from Part 1: the customer is never left on read |

### Queue: Redis Streams with consumer groups

With `QUEUE_MODE=redis`, the webhook handler `XADD`s the normalised message to a stream and returns. Workers (`python -m app.worker`) read with `XREADGROUP`, so each entry goes to exactly one consumer at a time. A worker `XACK`s after the pipeline finishes. If it crashes, the entry stays pending, and other workers pick it up with `XAUTOCLAIM` once a visibility timeout passes. After `WORKER_MAX_DELIVERIES` attempts, the entry moves to a dead-letter stream with its error.

```python
# app/reliability/queue.py
_, claimed, *_ = await self.redis.xautoclaim(self.stream, GROUP, self.consumer,
                                             min_idle_time=self.visibility_ms, start_id="0-0", count=self.batch)
resp = await self.redis.xreadgroup(GROUP, self.consumer, {self.stream: ">"}, count=self.batch, block=self.block_ms)
```

That's **at-least-once delivery**. Combined with **idempotent processing** (`SET seen:<message id> NX EX 86400`), it gives effectively-once replies. If processing fails, the dedupe claim is released, so the redelivery isn't mistaken for a duplicate.

Ordering needs care. A consumer group spreads entries across workers, so "hi" and "where's my order" from the same customer could land on two workers. The **per-conversation lock** (`SET NX PX` with a token-checked release in Lua) serialises them. If you need strict ordering guarantees, partition streams by conversation hash and pin one consumer per partition, Kafka-style. For chat support, the lock is enough in practice.

Why Redis Streams rather than Kafka or SQS? We already need Redis for dedupe and locks, Streams give consumer groups, reclaim and backlog metrics, and KEDA can scale workers on stream backlog. Swap in a managed queue if your platform standardises on one; the `Worker` contract stays the same.

### LLM resilience: retries, breakers, failover

`ResilientLLM` wraps an ordered list of providers and implements the same `LLMClient` interface, so nothing else changes:

- **Retries** with *full-jitter* exponential backoff, only for retryable errors (429, 5xx, timeouts), never for 400s
- a **circuit breaker** per provider: after N consecutive failures it opens and skips that provider for a cooldown, then lets one trial request through
- **failover** to the next provider in the chain

```bash
LLM_PROVIDER=anthropic  LLM_MODEL=claude-haiku-5-5
LLM_FALLBACKS='[{"provider":"openai","model":"<model>","api_key_env":"OPENAI_API_KEY"},
                {"provider":"openai","model":"<open-weights model>","base_url":"http://vllm:8000/v1"}]'
```

Put at least one fallback with a *different vendor*, and ideally a self-hosted open-weights model behind vLLM as the last resort. Then a single vendor outage degrades answer quality slightly instead of taking support down. Tool schemas and JSON mode work the same across the chain because of the interface from Part 1.

Delivery retries are separate. If the Graph API throttles the reply, the pipeline retries **only the delivery**, so the customer doesn't get two different answers.

## 2. Observability

Prometheus metrics (`/metrics` on the API, port 9100 on workers) cover the numbers a support-automation team runs on:

| Metric | Why it matters |
|---|---|
| `replies_total{outcome}`: answered / escalated / flow / fallback / silent | containment and health in one chart |
| `escalations_total{reason}` | which knowledge or flow to build next |
| `answers_total{grounded}` | hallucination pressure; ungrounded answers trend up after KB changes |
| `turn_seconds`, `llm_seconds{provider}` | p95 latency, which customers feel on chat |
| `llm_tokens_total{provider,direction}` | cost |
| `queue_pending`, `dead_letters_total`, `circuit_open` | operational health; these drive alerts |

Labels stay low-cardinality (no user or conversation ids). `/healthz` is liveness and checks nothing external, to avoid restart storms. `/readyz` checks Postgres, Redis and Qdrant, so the load balancer stops routing to a replica that can't work. `docs/operations.md` has alert thresholds and runbooks for dead letters, LLM outages and key rotation.

## 3. Enterprise requirements

### Multi-tenancy

One deployment can serve many stores: brands in a group, regions, or an agency's clients. Each tenant is a YAML file (persona, policies, escalation rules) that also declares **which Meta assets belong to it** and **references** to its secrets:

```yaml
channels:
  whatsapp_phone_number_id: "109876543210"
  page_id: "222333444"
secrets:
  whatsapp_token: env:BREWLINE_WHATSAPP_TOKEN
  page_access_token: file:/run/secrets/brewline_page_token
shopify: { shop: brewline, client_id: "..." }
```

The webhook's business id (phone number id, page id, IG account id) routes each message to its tenant's `Agent`. Unknown assets are dropped and logged. Isolation is structural rather than a matter of discipline: one Qdrant collection per tenant (a retrieval physically can't cross stores), `tenant_id` on conversations, profiles and audit rows, tenant-scoped API keys, and secrets that never sit in YAML.

### RBAC and audit

Operators authenticate with API keys that carry a **role** and optionally a **tenant scope**. Keys are 256-bit random values shown once and stored as SHA-256 hashes.

| Role | Can |
|---|---|
| admin | everything, including key management and erasure |
| supervisor | agent rights + analytics, audit log, knowledge re-index, customer profiles |
| agent | queue, transcripts **with PII revealed**, reply, release |
| analyst | analytics; transcripts with PII still pseudonymised |
| auditor | audit log; transcripts with PII still pseudonymised |

This is where Part 3's privacy design pays off. The same transcript endpoint returns `<EMAIL_1>` to an analyst and the real address to an agent, and **every PII reveal**, human reply, release, erasure, key change and re-index is written to an append-only `audit_log` with who, what, which tenant and when. Cross-tenant access returns `404`, not `403`, so a key can't even confirm a conversation exists elsewhere. For SSO, put the admin surface behind your identity-aware proxy (OIDC) and map groups to roles. API keys remain the machine-to-machine path.

### Analytics

`GET /admin/analytics` returns conversations by channel, escalations by reason and the **containment rate**: the share of conversations that never needed a human. Watch it week over week. A drop usually points to a knowledge gap or a broken flow, and the escalation reasons tell you which.

## 4. Tone, personality and per-customer customisation

Part 1 made the persona configurable per store. Real customers want more: some want Hindi, some want two-line answers, some find emoji unprofessional. A good human agent remembers those things, and the bot should too, across sessions and channels.

### Layered persona resolution

```
tenant base persona      warm barista, casual, light emoji          (brand)
  + channel override     Instagram: expressive emoji                (channel norms)
  + A/B variant          "concise" vs "warm", sticky by hash        (experiments)
  + customer profile     Hindi, formal, no emoji                    (the individual)
```

```python
# app/personalization/persona.py
persona = _apply(tenant.persona, tenant.personalization.channel_overrides.get(channel))
variant = assign_variant(tenant, customer_key)              # deterministic, weighted
if variant: persona = _apply(persona, variant_overrides)
if profile: persona = _apply(persona, PersonaOverride(language=profile.language, ...))
```

**Variants** let you test voices properly. Assignment is a weighted hash of the customer key, so each customer consistently sees one voice, and the variant name is recorded on every reply so you can compare containment and CSAT per arm. **Few-shot brand examples** in the tenant YAML (2-4 real exchanges) shape tone far better than a page of adjectives.

### Capturing preferences

Preferences are captured three ways:

1. **Explicit phrases, detected deterministically:** "reply in Hindi", "be brief", "no emojis", "be more formal". Cheap, reliable, and covered by tests.
2. **An LLM tool** (`set_customer_preferences`) for fuzzier requests.
3. **The admin API / CRM sync:** `PUT /admin/tenants/{t}/profiles/{customer}`, e.g. marking Shopify "VIP" tags. VIP customers get extra-attentive instructions in the prompt, and their hand-offs are bumped to high priority.

Profiles store *preferences*, not personal data: no names, emails or addresses.

### StyleGuard: prompts are requests, not guarantees

Even with good prompts, models drift: a customer who asked for no emoji gets "Sure thing! 😊". `StyleGuard` checks every DM reply against the resolved persona and fixes it deterministically. It strips emoji beyond the allowed count, removes banned phrases, and trims prose to the verbosity limit (lists and step-by-step instructions are left alone). The fixes are recorded in reply metadata, so you can see which rules the model breaks most.

## 5. Tests and evals: the deployment gate

Every change to a prompt, model, threshold or knowledge base changes behaviour somewhere. Without a fixed test set, regressions surface as angry customers. The repo verifies the agent at three levels.

**Unit and integration tests** (`pytest`, 150+ tests) drive real webhook payloads through the real app with the LLM, Meta and Shopify mocked. A dedicated suite, `tests/test_standard_responses.py`, checks **the exact message a customer receives** in standard journeys on each channel:

- an FAQ answer retrieves the right article and ends with the policy link, with no citation markers or source tags leaking
- WhatsApp gets `*bold*`, Messenger and Instagram get plain text, and DMs, comments and length limits are respected
- tracking details appear only for the verified customer
- confirmation buttons render as quick replies with titles of 20 characters or fewer
- the hand-off message changes with business hours
- the fallback includes the support email when every LLM is down

**Golden conversations** (`evals/cases.yaml`): 58 cases in 16 categories (shipping, returns, subscriptions, brewing, FAQ, grounding, scope, safety, orders, products, flows, escalation, privacy, comments, personalisation, conversation). Each case can carry three kinds of check:

```yaml
- id: returns_opened_coffee
  category: returns
  turns: ["Can I return an opened bag of coffee?"]
  retrieval: { section: "Returns and refunds > Coffee beans", within: 2 }    # right article? (every mode)
  expect: { contains_any: ["can't", "cannot"], grounded: true }             # hard assertions on the reply
  judge: "States opened coffee can't be returned because it's perishable. May mention the Taste Promise.
          Must not offer a refund for opened coffee."                         # graded by an LLM judge

- id: cancel_unfulfilled_order_with_confirmation
  category: flows
  channel: whatsapp
  user_id: "919800000001"
  turns: ["I want to cancel order 1043", { text: "Yes, cancel it", payload: "yes" }]
  expect: { contains_any: ["is cancelled"], shopify_mutation: cancel }     # side effects, not just text
```

- **Retrieval checks** confirm the knowledge base surfaced the right article. They're deterministic, so they run in every mode and catch chunking and threshold regressions early.
- **Expect checks** assert on the reply and its side effects: text present or forbidden, escalation reason, tools called, buttons shown, Shopify mutations made or *not* made, comments hidden, DMs sent, emoji stripped, length limits.
- **Judge rubrics** are graded 1-5 by an LLM against the retrieved reference facts. Substring checks catch hard failures. Only a judge can tell whether "you can't return opened coffee, but our Taste Promise covers a different one" is a *good* answer. Use a different, ideally stronger, model as the judge (`JUDGE_PROVIDER` / `JUDGE_MODEL`), and spot-check its verdicts when you change a rubric.

Shopify is replaced by a deterministic fake with five orders covering in transit, unfulfilled, delivered coffee, delivered equipment and high value. Results are reproducible, and flows can be tested end to end.

- `python -m evals.run --fake` runs every deterministic check with a scripted fake LLM. It's the **CI gate on every push**, needs no API keys, and writes JUnit XML for the CI test tab.
- `python -m evals.run` runs all cases plus the judge against your real LLM. CI runs it **nightly** and on demand, with a pass threshold, and saves a JSON report you can diff between runs.

Add a case for every bug you fix and every flow you build. That's how the eval set becomes the most valuable file in the repo.

## 6. Deployment

The repo ships two deployment paths with step-by-step guides (`docs/deployment.md`). Local setup is in `docs/local-development.md`.

**One server with Docker Compose.** `docker compose --profile tls up -d --build` starts the API, two workers, Postgres, Redis (with AOF on, so queued messages survive restarts), Qdrant, and **Caddy**. Caddy obtains Let's Encrypt certificates automatically for your domain and blocks `/admin`, `/agent` and `/metrics` from outside your office or VPN ranges. Databases bind to localhost only. Scale with `docker compose up -d --scale worker=4`. A daily cron runs retention.

**Kubernetes** (`deploy/k8s/`). The API is a Deployment with an HPA on CPU and readiness on `/readyz`. Workers are scaled by **KEDA's redis-streams scaler** on pending entries and handle `SIGTERM` gracefully, so in-flight turns finish. A retention CronJob handles cleanup, and Ingress with cert-manager handles TLS (public webhooks on one Ingress, IP-restricted operator paths on another). Postgres, Redis and Qdrant come from managed services. Secrets come from a Secret or External Secrets, and tenant YAML from a ConfigMap.

Either way, the go-live sequence is the same:

1. point DNS at the server or ingress, and set `PUBLIC_BASE_URL` (a `shopassist.example.com` placeholder until then)
2. generate keys with `make keys`
3. start the stack and check `/readyz`
4. ingest knowledge, including Shopify content
5. create named operator keys and retire the bootstrap key
6. register `https://<domain>/webhooks/meta` with Meta and `https://<domain>/webhooks/shopify/compliance` with Shopify
7. run the real-LLM evals against the production configuration before switching traffic on

**Capacity:** worker throughput ≈ replicas × concurrency ÷ mean turn time. The cross-encoder reranker is CPU-bound, so give workers real CPU or move reranking to a GPU inference server.

## A production checklist

- [ ] Webhook signature verified on raw bytes; HTTPS only
- [ ] Idempotency on message ids; queue with dead-letter; alerts on DLQ and queue depth
- [ ] At least one LLM fallback from a different vendor or self-hosted
- [ ] Knowledge base indexed with a relevance threshold calibrated on evals
- [ ] Identity checks in tool code; no order enumeration; minimal tool outputs
- [ ] Escalation scenarios configured; business hours and hand-off messages reviewed by the support lead
- [ ] PII vault keys in a secret manager; rotation rehearsed; log redaction on; retention job scheduled
- [ ] Shopify GDPR webhooks registered and tested
- [ ] RBAC keys issued per person or system; bootstrap key removed; audit log reviewed
- [ ] Deterministic evals in CI on every push; real-LLM evals with the judge nightly; a new case for every incident
- [ ] Meta Graph API and Shopify API versions on a calendar for annual and quarterly bumps

## Closing thoughts

Look back at where the code went across the four parts. The LLM call is a handful of lines. Everything else exists because **the model proposes and the system disposes**. Retrieval decides what the model may know. Tools decide what it may see. Flows decide what it may do. Escalation rules decide when it should step aside. The vault decides what it may hold. The queue and the breakers decide what happens when things break. StyleGuard and evals decide whether it sounded right and stayed right.

That's the difference between a demo and a support agent you can put your brand's name on. The full code, with 150+ tests, 58 golden conversations and four tagged stages, is at [github.com/solint-labs/shopassist](https://github.com/solint-labs/shopassist). Start from `part-1`, read the diffs between tags alongside each article, and build out the parts your store needs.
