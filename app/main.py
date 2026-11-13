"""FastAPI application factory and dependency wiring.

`create_app()` builds every component once and hangs it on `app.state`. Tests call
`create_app(overrides)` to swap in fakes (LLM, gateway) without monkeypatching.

Run locally:   uvicorn --factory app.main:create_app --reload
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from app.agent.memory import ConversationStore
from app.agent.orchestrator import Agent
from app.agent.tenant import load_tenant_config
from app.channels.gateway import ChannelGateway, SettingsCredentialResolver
from app.channels.graph_client import GraphClient
from app.channels.webhook import router as webhook_router
from app.llm.factory import build_llm
from app.logging_config import configure_logging, log
from app.pipeline import MessagePipeline
from app.settings import Settings, get_settings
from app.storage.db import Database


def create_app(settings: Settings | None = None, **overrides: Any) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db: Database = app.state.db
        await db.create_all()
        log.info("app.started", env=settings.env, llm=settings.llm_provider)
        yield
        await app.state.gateway.fb.aclose()
        await app.state.gateway.ig.aclose()
        await db.dispose()

    app = FastAPI(title="ShopAssist", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings

    db = overrides.get("db") or Database(settings.database_url)
    tenant = overrides.get("tenant") or load_tenant_config(settings.tenant_config_path)
    llm = overrides.get("llm") or build_llm(settings)
    gateway = overrides.get("gateway") or ChannelGateway(
        fb=GraphClient(settings.meta_graph_host, settings.meta_graph_version, dry_run=settings.dry_run_outbound),
        ig=GraphClient(settings.instagram_api_host, settings.meta_graph_version, dry_run=settings.dry_run_outbound),
        creds=SettingsCredentialResolver(settings),
    )
    agent = Agent(llm, ConversationStore(db), tenant, settings.history_turns)

    app.state.db = db
    app.state.tenant = tenant
    app.state.llm = llm
    app.state.gateway = gateway
    app.state.agent = agent
    app.state.pipeline = MessagePipeline(agent, gateway)

    app.include_router(webhook_router)

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict:
        """Liveness: the process is up."""
        return {"status": "ok"}

    return app

