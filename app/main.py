"""FastAPI application factory and dependency wiring.

`create_app()` builds every component once and hangs it on `app.state`. Tests call
`create_app(settings, llm=..., retriever=..., tools=...)` to swap in fakes without
monkeypatching.

Run locally:   uvicorn --factory app.main:create_app --reload
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from qdrant_client import AsyncQdrantClient

from app.agent.memory import ConversationStore
from app.agent.orchestrator import Agent
from app.agent.tenant import load_tenant_config
from app.channels.gateway import ChannelGateway, SettingsCredentialResolver
from app.channels.graph_client import GraphClient
from app.channels.webhook import router as webhook_router
from app.commerce.shopify import ShopifyClient
from app.commerce.tools import ToolRegistry, build_commerce_tools
from app.llm.factory import build_llm
from app.logging_config import configure_logging, log
from app.pipeline import MessagePipeline
from app.rag.encoders import build_encoders
from app.rag.ingest import Ingestor, load_markdown_dir
from app.rag.retriever import Retriever
from app.rag.store import KnowledgeStore
from app.settings import Settings, get_settings
from app.storage.db import Database


def build_qdrant(s: Settings) -> AsyncQdrantClient:
    if s.qdrant_url:
        return AsyncQdrantClient(url=s.qdrant_url, api_key=s.qdrant_api_key.get_secret_value() if s.qdrant_api_key else None)
    if s.qdrant_path != ":memory:":
        Path(s.qdrant_path).mkdir(parents=True, exist_ok=True)
    return AsyncQdrantClient(location=":memory:") if s.qdrant_path == ":memory:" else AsyncQdrantClient(path=s.qdrant_path)


def build_shopify(s: Settings) -> ShopifyClient | None:
    if not s.shopify_shop:
        return None
    return ShopifyClient(
        s.shopify_shop, s.shopify_api_version,
        access_token=s.shopify_access_token.get_secret_value() if s.shopify_access_token else None,
        client_id=s.shopify_client_id,
        client_secret=s.shopify_client_secret.get_secret_value() if s.shopify_client_secret else None,
    )


def create_app(settings: Settings | None = None, **overrides: Any) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    db = overrides.get("db") or Database(settings.database_url)
    tenant = overrides.get("tenant") or load_tenant_config(settings.tenant_config_path)
    llm = overrides.get("llm") or build_llm(settings)
    gateway = overrides.get("gateway") or ChannelGateway(
        fb=GraphClient(settings.meta_graph_host, settings.meta_graph_version, dry_run=settings.dry_run_outbound),
        ig=GraphClient(settings.instagram_api_host, settings.meta_graph_version, dry_run=settings.dry_run_outbound),
        creds=SettingsCredentialResolver(settings),
    )

    # ── Part 2: knowledge base + Shopify tools ──────────────────────────────
    if "knowledge_store" in overrides:
        knowledge = overrides["knowledge_store"]
        reranker = overrides["reranker"]
    else:
        dense, sparse, reranker = build_encoders(settings.rag_encoders, settings.rag_dense_model,
                                                 settings.rag_sparse_model, settings.rag_reranker_model,
                                                 settings.rag_model_cache)
        knowledge = KnowledgeStore(build_qdrant(settings), dense, sparse)
    retriever = overrides.get("retriever") or Retriever(
        knowledge, reranker, llm, settings.rag_top_k, settings.rag_top_n, settings.rag_min_score,
        settings.rag_condense_query)
    shopify = overrides.get("shopify", build_shopify(settings))
    tools = overrides.get("tools") or ToolRegistry(build_commerce_tools(shopify) if shopify else [])

    agent = Agent(llm, ConversationStore(db), tenant, settings.history_turns, retriever=retriever, tools=tools,
                  max_tool_rounds=settings.max_tool_rounds, append_source_link=settings.rag_append_source_link)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.create_all()
        if settings.rag_ingest_on_startup and Path(settings.knowledge_dir).exists():
            await Ingestor(knowledge).ingest(tenant.tenant_id, load_markdown_dir(settings.knowledge_dir))
        log.info("app.started", env=settings.env, llm=settings.llm_provider, shopify=bool(shopify), tools=len(tools.specs()))
        yield
        await gateway.fb.aclose()
        await gateway.ig.aclose()
        if shopify:
            await shopify.aclose()
        await db.dispose()

    app = FastAPI(title="ShopAssist", version="0.2.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db
    app.state.tenant = tenant
    app.state.llm = llm
    app.state.gateway = gateway
    app.state.knowledge = knowledge
    app.state.shopify = shopify
    app.state.agent = agent
    app.state.pipeline = MessagePipeline(agent, gateway)

    app.include_router(webhook_router)

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict:
        """Liveness: the process is up."""
        return {"status": "ok"}

    return app
