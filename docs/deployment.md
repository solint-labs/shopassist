# Production deployment

Two supported paths:

- **Option A: one server with Docker Compose.** API, workers, Postgres, Redis, Qdrant and Caddy for
  automatic HTTPS. Good for one store, or a few, up to thousands of conversations a day.
- **Option B: Kubernetes.** API with an HPA, workers autoscaled on queue depth with KEDA, managed
  Postgres/Redis/Qdrant. Good for many tenants and high volume.

`shopassist.example.com` is a **placeholder** throughout (`.env.example`, `deploy/env.production.example`,
the Caddyfile, the Kubernetes Ingress). Replace it with your domain once you have one. Nothing else
depends on the name.

```
Meta / Shopify ──HTTPS──► Caddy or Ingress ──► api ──► Redis Stream ──► workers ──► LLM / Shopify / Graph API
                                                │                        │
                                                └────── Postgres ◄───────┴──► Qdrant
```

## Before you start

1. **Domain:** a DNS name you control, e.g. `support.yourstore.com`. Meta only calls HTTPS webhooks.
2. **Meta app:** WhatsApp, Messenger and Instagram products added ([meta-setup.md](meta-setup.md)).
   For production WhatsApp you need a verified business and a registered phone number.
3. **Shopify app:** created in the Dev Dashboard with scopes `read_orders`, `read_products`,
   `write_orders`, `read_returns`/`write_returns`, plus protected customer data access ([shopify-setup.md](shopify-setup.md)).
4. **LLM:** a primary provider plus at least one fallback (different vendor or self-hosted vLLM).
5. **Secrets:** generate them with `make keys` (PII encryption key, blind-index key, admin bootstrap key,
   Meta verify token). Store them in a password manager or secret store. **Losing
   `PII_ENCRYPTION_KEYS` makes stored placeholders unrecoverable.**

## Option A: single server with Docker Compose

Server sizing: 4 vCPU / 8 GB RAM handles a busy single store (the cross-encoder reranker is the main
CPU consumer). Ubuntu 24.04 LTS with Docker Engine and the Compose plugin.

### A1. DNS and firewall

- Create an `A` (and `AAAA`) record: `shopassist.example.com → <server IP>`.
- Open ports 80 and 443 only. Postgres, Redis, Qdrant and the API bind to `127.0.0.1` in `docker-compose.yml`.

### A2. Get the code and configure

```bash
git clone https://github.com/solint-labs/shopassist.git /opt/shopassist && cd /opt/shopassist
cp deploy/env.production.example .env
make keys                     # paste the generated values into .env
nano .env                     # set domain, LLM, Meta, Shopify, POSTGRES_PASSWORD, ...
```

Required in `.env`:

| Variable | Notes |
|---|---|
| `PUBLIC_BASE_URL`, `SHOPASSIST_DOMAIN`, `ACME_EMAIL` | your domain (placeholders until hosted) |
| `POSTGRES_PASSWORD` | strong random value |
| `LLM_PROVIDER`, `LLM_MODEL`, `LLM_API_KEY`, `LLM_FALLBACKS` | primary + fallbacks |
| `META_APP_ID`, `META_APP_SECRET`, `META_VERIFY_TOKEN` | Meta app |
| `WHATSAPP_*`, `PAGE_*`, `INSTAGRAM_ACCOUNT_ID` | single-store mode; multi-tenant stores use their YAML instead |
| `SHOPIFY_SHOP`, `SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET` | Dev Dashboard app |
| `PII_ENCRYPTION_KEYS`, `PII_BLIND_INDEX_KEY` | **required** when `ENV=prod`; the app refuses to start without them |
| `ADMIN_BOOTSTRAP_KEY` | temporary; remove after step A5 |

Store configuration lives in `config/tenants/*.yaml` (persona, policies, escalation rules, business hours)
and `knowledge/<tenant_id>/*.md`. Review both with your support lead before go-live.

### A3. Start

```bash
docker compose --profile tls up -d --build      # api, 2 workers, postgres, redis, qdrant, caddy (HTTPS)
docker compose ps
curl -s https://shopassist.example.com/healthz  # {"status":"ok"}
docker compose exec api python -c "import httpx; print(httpx.get('http://localhost:8000/readyz').json())"
```

Caddy obtains a Let's Encrypt certificate automatically on first request (DNS must already point at the
server). The first API start downloads the RAG models into the `models` volume. Expect a minute or two.

The `/admin`, `/agent` and `/metrics` paths are only reachable from the CIDR ranges in
`deploy/caddy/Caddyfile`. Replace the placeholder ranges with your office/VPN ranges.

### A4. Index knowledge

```bash
docker compose exec api python -m scripts.ingest --shopify       # markdown + Shopify policies, pages, products
```

### A5. Operator access

```bash
export ADMIN=<ADMIN_BOOTSTRAP_KEY>
curl -s -XPOST https://shopassist.example.com/admin/api-keys -H "authorization: Bearer $ADMIN" \
     -H 'content-type: application/json' -d '{"name":"ops-admin","role":"admin"}'
# create supervisor/agent keys per person or per integration, then:
#   remove ADMIN_BOOTSTRAP_KEY from .env  &&  docker compose up -d
```

### A6. Register webhooks

| Where | URL | Settings |
|---|---|---|
| Meta app → Webhooks (WhatsApp Business Account, Page, Instagram) | `https://shopassist.example.com/webhooks/meta` | verify token = `META_VERIFY_TOKEN`; fields per [meta-setup.md](meta-setup.md) |
| Shopify Dev Dashboard → app → Compliance webhooks | `https://shopassist.example.com/webhooks/shopify/compliance` | `customers/data_request`, `customers/redact`, `shop/redact` |

The API logs both URLs at startup (`app.started meta_webhook=... shopify_compliance_webhook=...`).

### A7. Schedule maintenance

```bash
# /etc/cron.d/shopassist: retention, key-rotation readiness, idle hand-off release (daily 02:17)
17 2 * * * root cd /opt/shopassist && docker compose --profile jobs run --rm retention >> /var/log/shopassist-retention.log 2>&1
```

Backups: nightly `pg_dump` of Postgres (it holds conversations, the encrypted vault, profiles and the
audit log). Snapshot Qdrant, or simply re-ingest; it's derived data. Keep the PII keys backed up
separately from database backups.

### A8. Scale and upgrade

```bash
docker compose up -d --scale worker=4                          # more throughput
git pull && docker compose --profile tls up -d --build         # upgrade (rolling for workers)
```

## Option B: Kubernetes

Manifests: `deploy/k8s/shopassist.yaml` (API Deployment + Service + HPA, worker Deployment + KEDA
ScaledObject, retention CronJob, Ingress) and `deploy/k8s/secrets.example.yaml`.

### B1. Managed dependencies

- **Postgres** (RDS / Cloud SQL / Azure Database): create database `shopassist`.
- **Redis** (ElastiCache / Memorystore): enable persistence (AOF), because the queue lives here.
- **Qdrant**: Qdrant Cloud or the Qdrant Helm chart with replication.
- Cluster add-ons: an ingress controller (manifests assume ingress-nginx), **cert-manager** with a
  `letsencrypt` ClusterIssuer, **KEDA** for worker autoscaling, and Prometheus (pods are annotated for scraping).

### B2. Build and push the image

```bash
docker build -t ghcr.io/<your-org>/shopassist:1.0.0 .
docker push ghcr.io/<your-org>/shopassist:1.0.0
# update the image references in deploy/k8s/shopassist.yaml
```

### B3. Configure

```bash
kubectl create namespace shopassist
kubectl -n shopassist create secret generic shopassist-secrets --from-env-file=.env.production   # or External Secrets
kubectl -n shopassist create configmap shopassist-tenants --from-file=config/tenants/
```

Replace `shopassist.example.com` in the Ingress and ConfigMap with your domain, and the placeholder
CIDRs on the operator Ingress with your ranges. Bake `knowledge/` into the image (the default), or mount
it from a ConfigMap or volume.

### B4. Deploy and verify

```bash
kubectl -n shopassist apply -f deploy/k8s/shopassist.yaml
kubectl -n shopassist rollout status deploy/shopassist-api deploy/shopassist-worker
curl -s https://shopassist.example.com/healthz
kubectl -n shopassist exec deploy/shopassist-api -- python -m scripts.ingest --shopify
```

Then do steps A5 (operator keys) and A6 (webhooks) against your domain.

## Multi-tenant setup

Set `TENANTS_DIR=config/tenants` and `KNOWLEDGE_ROOT=knowledge`. For each store:

1. Add `config/tenants/<tenant_id>.yaml` with `channels:` (WhatsApp phone number id, Page id, IG account id),
   `secrets:` as `env:NAME` or `file:/path` references, and `shopify:`.
2. Add the referenced secrets to the environment or secret store.
3. Add `knowledge/<tenant_id>/*.md`, then `POST /admin/tenants/<tenant_id>/reindex`.
4. Subscribe the store's Page/IG account/WABA to your Meta app. They all use the same webhook URL;
   routing happens by asset id.
5. Issue tenant-scoped API keys for that store's agents (`"tenant_id": "<tenant_id>"`).

## Go-live checklist

- [ ] `https://<domain>/readyz` returns `ok` for database, redis and qdrant
- [ ] Meta webhook verified; a test message on each channel gets a reply (WhatsApp test number first)
- [ ] Shopify order lookup works for a real test order; compliance webhooks registered
- [ ] `python -m evals.run` against the production LLM config passes your threshold (run it from a staging copy)
- [ ] Hand-off reaches your helpdesk; agents can reply via the agent API or the Meta inbox
- [ ] PII keys backed up; bootstrap admin key removed; operator paths IP-restricted
- [ ] Alerts on queue depth, dead letters, open circuits and fallback replies ([operations.md](operations.md))
- [ ] Retention job scheduled; Postgres backups tested with a restore

## Upgrades, schema changes, versions

- **Rolling upgrades** are safe for the API and workers. Queued messages wait in Redis while workers restart.
- **Schema:** the app creates missing tables at startup (`create_all`). It doesn't alter existing ones.
  Before your first schema change in production, add Alembic and generate a baseline from the models in
  `app/storage/models.py`, `app/privacy/vault.py`, `app/personalization/profiles.py` and `app/enterprise/rbac.py`.
- **API versions:** `META_GRAPH_VERSION` (Meta keeps versions ~2 years) and `SHOPIFY_API_VERSION`
  (quarterly releases, each supported at least 12 months) are single settings. Bump them, run
  `make test && make evals`, then deploy.
