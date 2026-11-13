"""FastAPI application factory and dependency wiring.

`create_app()` builds every component once and hangs it on `app.state`. Tests call
`create_app(settings, llm=..., helpdesk=...)` to swap in fakes without monkeypatching.

Shared across tenants: database, LLM (with fallbacks), Qdrant, PII vault, Graph API
clients, Redis (dedupe, locks, queue). Per tenant: config, Shopify client, tools,
hand-off, the Agent itself.

Run the API:      uvicorn --factory app.main:create_app
Run a worker:     python -m app.worker          (QUEUE_MODE=redis)
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Response
from qdrant_client import AsyncQdrantClient
from sqlalchemy import text

from app import __version__
from app.agent.memory import ConversationStore
from app.agent.orchestrator import Agent
from app.agent.tenant import TenantConfig
from app.channels.gateway import ChannelGateway
from app.channels.graph_client import GraphClient
from app.channels.webhook import router as webhook_router
from app.commerce.shopify import ShopifyClient
from app.commerce.tools import ToolRegistry, build_commerce_tools
from app.enterprise.admin_api import router as admin_router
from app.enterprise.rbac import Auditor, KeyStore
from app.escalation.agent_api import router as agent_router
from app.escalation.handoff import HandoffService, NullHelpdesk, WebhookHelpdesk, ZendeskHelpdesk
from app.flows.engine import FlowEngine
from app.flows.orders import CancelOrderFlow, ForgetMeFlow, ReturnRequestFlow
from app.flows.tools import build_flow_tools
from app.llm.factory import build_llm
from app.logging_config import configure_logging, log
from app.observability import metrics
from app.personalization.profiles import ProfileStore, build_profile_tools
from app.pipeline import MessagePipeline
from app.privacy.compliance import PrivacyService
from app.privacy.compliance import router as privacy_router
from app.privacy.pii import PIIDetector
from app.privacy.vault import PIIVault, generate_key
from app.rag.encoders import build_encoders
from app.rag.ingest import Ingestor, load_markdown_dir
from app.rag.retriever import Retriever
from app.rag.store import KnowledgeStore
from app.reliability.dedupe import ConversationLocks, MemoryDedupe, RedisDedupe
from app.reliability.queue import InboundQueue
from app.settings import Settings, get_settings
from app.storage.db import Database
from app.tenancy.registry import TenantRegistry, TenantRuntime, load_tenant_configs, resolve_secret


def build_qdrant(s: Settings) -> AsyncQdrantClient:
    if s.qdrant_url:
        return AsyncQdrantClient(url=s.qdrant_url, api_key=s.qdrant_api_key.get_secret_value() if s.qdrant_api_key else None)
    if s.qdrant_path != ":memory:":
        Path(s.qdrant_path).mkdir(parents=True, exist_ok=True)
    return AsyncQdrantClient(location=":memory:") if s.qdrant_path == ":memory:" else AsyncQdrantClient(path=s.qdrant_path)


def build_shopify(s: Settings, tenant: TenantConfig | None = None) -> ShopifyClient | None:
    """Tenant-level Shopify config wins; otherwise fall back to the global settings."""
    if tenant and tenant.shopify:
        return ShopifyClient(tenant.shopify.shop, tenant.shopify.api_version,
                             access_token=resolve_secret(tenant.secrets.shopify_access_token),
                             client_id=tenant.shopify.client_id,
                             client_secret=resolve_secret(tenant.secrets.shopify_client_secret))
    if not s.shopify_shop:
        return None
    return ShopifyClient(
        s.shopify_shop, s.shopify_api_version,
        access_token=s.shopify_access_token.get_secret_value() if s.shopify_access_token else None,
        client_id=s.shopify_client_id,
        client_secret=s.shopify_client_secret.get_secret_value() if s.shopify_client_secret else None,
    )


def build_vault(s: Settings, db: Database) -> PIIVault:
    keys = [k.strip() for k in s.pii_encryption_keys.get_secret_value().split(",") if k.strip()]
    bkey = s.pii_blind_index_key.get_secret_value()
    if not keys or not bkey:
        if s.env in ("staging", "prod"):
            raise RuntimeError("PII_ENCRYPTION_KEYS and PII_BLIND_INDEX_KEY are required outside dev/test")
        log.warning("privacy.ephemeral_keys", detail="dev/test only: vault data won't survive a restart")
        keys, bkey = keys or [generate_key()], bkey or generate_key()
    return PIIVault(db, keys, bkey, PIIDetector(use_ner=s.pii_ner))


def build_helpdesk(s: Settings):
    if s.helpdesk == "zendesk":
        return ZendeskHelpdesk(s.zendesk_subdomain, s.zendesk_email, s.zendesk_api_token.get_secret_value())
    if s.helpdesk == "webhook":
        return WebhookHelpdesk(s.helpdesk_webhook_url, s.helpdesk_webhook_secret.get_secret_value())
    return NullHelpdesk()


def build_redis(s: Settings):
    if not s.redis_url:
        return None
    from redis.asyncio import Redis

    return Redis.from_url(s.redis_url)


def create_app(settings: Settings | None = None, **overrides: Any) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    # ── Shared components ───────────────────────────────────────────────────
    db = overrides.get("db") or Database(settings.database_url)
    llm = overrides.get("llm") or build_llm(settings)
    redis = overrides.get("redis", build_redis(settings))
    registry = TenantRegistry(settings)
    gateway = overrides.get("gateway") or ChannelGateway(
        fb=GraphClient(settings.meta_graph_host, settings.meta_graph_version, dry_run=settings.dry_run_outbound),
        ig=GraphClient(settings.instagram_api_host, settings.meta_graph_version, dry_run=settings.dry_run_outbound),
        creds=registry,  # per-tenant tokens, falling back to global settings
    )
    if "knowledge_store" in overrides:
        knowledge, reranker = overrides["knowledge_store"], overrides["reranker"]
    else:
        dense, sparse, reranker = build_encoders(settings.rag_encoders, settings.rag_dense_model,
                                                 settings.rag_sparse_model, settings.rag_reranker_model,
                                                 settings.rag_model_cache)
        knowledge = KnowledgeStore(build_qdrant(settings), dense, sparse)
    retriever = overrides.get("retriever") or Retriever(
        knowledge, reranker, llm, settings.rag_top_k, settings.rag_top_n, settings.rag_min_score,
        settings.rag_condense_query)
    store = ConversationStore(db)
    vault = overrides.get("vault") or build_vault(settings, db)
    helpdesk = overrides.get("helpdesk") or build_helpdesk(settings)
    profiles = ProfileStore(db)
    flows = FlowEngine([CancelOrderFlow(), ReturnRequestFlow(), ForgetMeFlow()])

    # ── One runtime per tenant ──────────────────────────────────────────────
    tenant_configs = [overrides["tenant"]] if "tenant" in overrides else load_tenant_configs(settings)
    shopify_clients: list[ShopifyClient] = []
    for cfg in tenant_configs:
        shopify = overrides["shopify"] if "shopify" in overrides else build_shopify(settings, cfg)
        if shopify:
            shopify_clients.append(shopify)
        tools = overrides.get("tools") or ToolRegistry(
            (build_commerce_tools(shopify) if shopify else []) + build_flow_tools()
            + build_profile_tools(profiles, cfg.tenant_id))
        handoff = HandoffService(store, gateway, helpdesk, llm, cfg, settings.meta_app_id, settings.meta_handover)
        agent = Agent(llm, store, cfg, settings.history_turns, retriever=retriever, tools=tools,
                      max_tool_rounds=settings.max_tool_rounds, append_source_link=settings.rag_append_source_link,
                      vault=vault, handoff=handoff, flows=flows, shopify=shopify, profiles=profiles)
        registry.add(TenantRuntime(cfg, agent))

    dedupe = RedisDedupe(redis) if redis else MemoryDedupe()
    pipeline = MessagePipeline(registry, gateway, dedupe, ConversationLocks(redis))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.create_all()
        if settings.rag_ingest_on_startup:
            for tid in registry.tenants:
                kdir = Path(settings.knowledge_root) / tid if settings.knowledge_root else Path(settings.knowledge_dir)
                if kdir.exists():
                    await Ingestor(knowledge).ingest(tid, load_markdown_dir(kdir))
        if redis and settings.queue_mode == "redis":
            await app.state.queue.ensure_group()
        log.info("app.started", env=settings.env, version=__version__, tenants=list(registry.tenants),
                 queue=settings.queue_mode, llm=getattr(llm, "name", "?"))
        yield
        await gateway.fb.aclose()
        await gateway.ig.aclose()
        for c in shopify_clients:
            await c.aclose()
        if redis:
            await redis.aclose()
        await db.dispose()

    app = FastAPI(title="ShopAssist", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db
    app.state.llm = llm
    app.state.redis = redis
    app.state.gateway = gateway
    app.state.knowledge = knowledge
    app.state.registry = registry
    app.state.profiles = profiles
    app.state.keys = KeyStore(db)
    app.state.auditor = Auditor(db)
    app.state.queue = InboundQueue(redis) if redis else None
    app.state.pipeline = pipeline
    app.state.privacy = PrivacyService(store, vault)
    # Single-tenant conveniences (used by Part 1-3 code paths and tests).
    app.state.tenant = registry.default.config
    app.state.agent = registry.default.agent
    app.state.handoff = registry.default.agent.handoff
    app.state.shopify = registry.default.agent.shopify

    app.include_router(webhook_router)
    app.include_router(agent_router)
    app.include_router(privacy_router)
    app.include_router(admin_router)

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict:
        """Liveness: the process is up. Don't check dependencies here (avoids restart storms)."""
        return {"status": "ok"}

    @app.get("/readyz", tags=["ops"])
    async def readyz(response: Response) -> dict:
        """Readiness: dependencies reachable. The load balancer stops routing here if not."""
        checks: dict[str, str] = {}
        try:
            async with db.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception as e:  # noqa: BLE001
            checks["database"] = f"error: {type(e).__name__}"
        if redis:
            try:
                await redis.ping()
                checks["redis"] = "ok"
            except Exception as e:  # noqa: BLE001
                checks["redis"] = f"error: {type(e).__name__}"
        try:
            await knowledge.client.get_collections()
            checks["qdrant"] = "ok"
        except Exception as e:  # noqa: BLE001
            checks["qdrant"] = f"error: {type(e).__name__}"
        ok = all(v == "ok" for v in checks.values())
        response.status_code = 200 if ok else 503
        return {"status": "ok" if ok else "degraded", "checks": checks}

    @app.get("/metrics", tags=["ops"])
    async def prometheus() -> Response:
        body, ctype = metrics.render()
        return Response(content=body, media_type=ctype)

    return app
