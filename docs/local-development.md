# Local development

This guide takes you from a fresh clone to a working agent on your laptop. You start fully offline
with the fake LLM, then add a real LLM, the knowledge base, the full stack (Postgres, Redis, Qdrant,
workers), and finally real Meta and Shopify connections through a tunnel.

## 1. Prerequisites

| Tool | Version | Needed for |
|---|---|---|
| Python | 3.11+ (3.12 recommended) | everything |
| git | any recent | checking out the `part-N` tags |
| Docker + Compose v2 | recent | optional: Postgres/Redis/Qdrant locally, full-stack runs |
| An LLM | Anthropic or OpenAI key, **or** Ollama/vLLM locally | real conversations (not needed for tests) |
| cloudflared or ngrok | any | optional: receiving real Meta webhooks on your laptop |

## 2. Install

```bash
git clone https://github.com/solint-labs/shopassist.git
cd shopassist
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env
```

Every stage of the article series has its own branch (`part-1` to `part-4`). To work on a specific part, run `git checkout part-2`
(or another part). Each branch has its own README and passing tests.

## 3. Run the tests and evals (no keys needed)

```bash
make test        # ~150 tests: webhooks, RAG, Shopify tools, flows, escalation, privacy, workers, RBAC
make evals       # deterministic golden conversations (evals/cases.yaml) with the fake LLM
```

Both run offline. HTTP to Meta and Shopify is mocked, Qdrant runs in memory, and `RAG_ENCODERS=hashing`
avoids model downloads.

## 4. Talk to the agent in your terminal

With `LLM_PROVIDER=fake` (the default), replies are canned but the whole pipeline runs. Pick a real LLM
in `.env` for real conversations:

```bash
# Anthropic
LLM_PROVIDER=anthropic
LLM_MODEL=claude-haiku-5-5
LLM_API_KEY=sk-ant-...

# Any OpenAI-compatible API
LLM_PROVIDER=openai
LLM_MODEL=<model name>
LLM_API_KEY=...

# Fully local, open-weights (Ollama: `ollama pull <model>` first)
LLM_PROVIDER=openai
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=<model tag>
```

Then run:

```bash
make chat                                              # WhatsApp-style chat
python -m scripts.chat_cli --channel instagram --comment   # simulate a public IG comment
```

## 5. Knowledge base (RAG)

By default `RAG_ENCODERS=fastembed` uses open-source models (BGE embeddings, BM25, BGE reranker).
They download once (a few hundred MB) into `RAG_MODEL_CACHE` or the fastembed default cache.
Offline, or in a hurry? Set `RAG_ENCODERS=hashing`.

```bash
make ingest ARGS='--query "can I return opened coffee?"'   # index knowledge/brewline and show scores
```

Edit the Markdown files in `knowledge/brewline/` and re-run. Unchanged documents are skipped.
With `RAG_INGEST_ON_STARTUP=true`, the API indexes on boot.

## 6. Run the API and send test webhooks

```bash
# .env: META_APP_SECRET=dev-secret  DRY_RUN_OUTBOUND=true
make run                                               # http://localhost:8000/docs for the OpenAPI UI
python -m scripts.send_test_webhook whatsapp "Do you ship to Pune?"
python -m scripts.send_test_webhook ig-comment "my order is late"
```

`send_test_webhook` signs the payload with `META_APP_SECRET` exactly as Meta does. With
`DRY_RUN_OUTBOUND=true`, every Graph API call the agent would make is logged instead of sent. You'll
see the read receipt, the typing indicator, the reply, the private reply and so on in the log.

## 7. Full stack locally (Postgres, Redis, Qdrant, workers)

```bash
make deps-up                                           # Postgres :5432, Redis :6379, Qdrant :6333 on localhost
```

Add these to `.env`:

```
DATABASE_URL=postgresql+asyncpg://shopassist:shopassist@localhost:5432/shopassist
REDIS_URL=redis://localhost:6379/0
QDRANT_URL=http://localhost:6333
QUEUE_MODE=redis
```

Then, in two terminals:

```bash
make run          # terminal 1: API (webhooks enqueue to Redis)
make worker       # terminal 2: worker (agent turns); start more for parallelism
```

Or run everything in containers with `make up` (API on http://localhost:8000).

## 8. Operator APIs

```bash
make keys                                   # prints ADMIN_BOOTSTRAP_KEY etc.; put them in .env, restart
export ADMIN=<bootstrap key>
curl -s -XPOST localhost:8000/admin/api-keys -H "authorization: Bearer $ADMIN" \
     -H 'content-type: application/json' -d '{"name":"priya","role":"agent","tenant_id":"brewline"}'
curl -s localhost:8000/agent/conversations -H "authorization: Bearer <agent key>"
curl -s localhost:8000/admin/analytics -H "authorization: Bearer $ADMIN"
curl -s localhost:8000/metrics | grep shopassist_
```

## 9. Real channels from your laptop (optional)

1. Expose port 8000 over HTTPS: `cloudflared tunnel --url http://localhost:8000`, which gives you
   `https://<random>.trycloudflare.com`.
2. Set `PUBLIC_BASE_URL` to that URL and `DRY_RUN_OUTBOUND=false`.
3. Follow [meta-setup.md](meta-setup.md), using `https://<tunnel>/webhooks/meta` as the callback URL.
   WhatsApp gives you a free test number and up to 5 recipient numbers for development.
4. For Shopify, create a **development store** and a Dev Dashboard app ([shopify-setup.md](shopify-setup.md)),
   then set `SHOPIFY_SHOP`, `SHOPIFY_CLIENT_ID` and `SHOPIFY_CLIENT_SECRET`. Run `make ingest ARGS=--shopify`.

## 10. Evals against your LLM

```bash
make evals-llm            # all cases + LLM judge; writes evals/report.json and evals/junit.xml
python -m evals.run --category returns -v
JUDGE_PROVIDER=anthropic JUDGE_MODEL=<stronger model> python -m evals.run   # separate judge model
```

Add a case to `evals/cases.yaml` for every bug you fix.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Could not load model BAAI/bge-small-en-v1.5` | No internet access to the model hub. Use `RAG_ENCODERS=hashing`, or pre-download into `RAG_MODEL_CACHE` |
| Webhook returns 401 | `META_APP_SECRET` doesn't match the signer (Meta app, or `send_test_webhook`'s `.env`) |
| Meta webhook verification fails | `META_VERIFY_TOKEN` must equal the token typed into the Meta dashboard |
| No reply, log shows `tenancy.unknown_business_id` | With `TENANTS_DIR`, add the page/phone/IG id to the tenant's `channels:` block |
| `privacy.ephemeral_keys` warning | Fine for dev. Set `PII_ENCRYPTION_KEYS` / `PII_BLIND_INDEX_KEY` (`make keys`) to keep vault data across restarts |
| WhatsApp reply rejected (outside 24h window) | Message the test number again from your phone to reopen the window |
