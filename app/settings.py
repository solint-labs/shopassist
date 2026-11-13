"""Runtime configuration.

All configuration comes from environment variables (or a local `.env` file), which is
how every container platform (Kubernetes, ECS, Cloud Run, Fly, Render...) injects config.
Secrets are typed as `SecretStr` so they never show up in logs or reprs by accident.

Brand/persona configuration (things a support lead edits) lives in YAML under
`config/tenants/`, not here. This file is for infrastructure.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── General ────────────────────────────────────────────────────────────────
    env: Literal["dev", "test", "staging", "prod"] = "dev"
    # Public HTTPS origin of this deployment. Placeholder until you host it; used for the
    # webhook URLs printed at startup and for conversation links in hand-off tickets.
    public_base_url: str = "https://shopassist.example.com"
    log_level: str = "INFO"
    log_json: bool = False  # JSON logs in prod so your log pipeline can index fields

    # SQLite works out of the box for local dev; use Postgres in any shared environment:
    #   postgresql+asyncpg://user:pass@host:5432/shopassist
    database_url: str = "sqlite+aiosqlite:///./shopassist.db"

    # Brand, persona and policy configuration for the store we serve.
    tenant_config_path: str = "config/tenants/brewline.yaml"

    # ── LLM ────────────────────────────────────────────────────────────────────
    # "openai" covers any OpenAI-compatible server: OpenAI, Azure OpenAI, vLLM, Ollama,
    # LM Studio, Together, Groq... Point LLM_BASE_URL at it.
    llm_provider: Literal["openai", "anthropic", "fake"] = "fake"
    llm_model: str = ""
    llm_base_url: str | None = None
    llm_api_key: SecretStr | None = None
    llm_temperature: float = 0.3
    llm_max_tokens: int = 600
    llm_timeout_s: float = 30.0

    # ── Meta platform (WhatsApp Cloud API, Messenger, Instagram) ───────────────
    meta_app_secret: SecretStr = SecretStr("")  # used to verify X-Hub-Signature-256
    meta_verify_token: SecretStr = SecretStr("")  # echoed back during webhook setup
    meta_graph_version: str = "v27.0"  # keep in ONE place; Meta versions expire after ~2 years
    meta_graph_host: str = "https://graph.facebook.com"

    whatsapp_phone_number_id: str = ""
    whatsapp_token: SecretStr = SecretStr("")  # system-user token with whatsapp_business_messaging

    page_id: str = ""  # Facebook Page connected to Messenger (and to Instagram)
    page_access_token: SecretStr = SecretStr("")

    # Instagram can be reached two ways:
    #  * "Instagram API with Facebook Login": host graph.facebook.com, Page token, and
    #    DMs are sent from the Page node (/{PAGE_ID}/messages).            <- default
    #  * "Instagram API with Instagram Login": host graph.instagram.com, IG user token,
    #    and DMs are sent from the IG account node (/{IG_ID}/messages).
    instagram_account_id: str = ""
    instagram_api_host: str = "https://graph.facebook.com"
    instagram_access_token: SecretStr | None = None  # falls back to page_access_token
    instagram_messaging_node: str | None = None  # falls back to page_id
    instagram_app_secret: SecretStr | None = None  # only for Instagram Login webhooks

    # When true, outbound messages are logged instead of sent. Handy for local runs
    # against recorded webhook payloads and for staging environments.
    dry_run_outbound: bool = False

    # Conversation memory: how many past turns the LLM sees.
    history_turns: int = Field(default=12, ge=2, le=50)

    # ── RAG (Part 2) ───────────────────────────────────────────────────────────
    # Qdrant: QDRANT_URL for a server (prod), or QDRANT_PATH for embedded mode
    # (":memory:" in tests, a directory for single-node deployments).
    qdrant_url: str | None = None
    qdrant_api_key: SecretStr | None = None
    qdrant_path: str = "./data/qdrant"
    knowledge_dir: str = "knowledge/brewline"
    # "fastembed" = open-source ONNX models on CPU (prod). "hashing" = deterministic, offline (tests).
    rag_encoders: Literal["fastembed", "hashing"] = "fastembed"
    rag_dense_model: str = "BAAI/bge-small-en-v1.5"
    rag_sparse_model: str = "Qdrant/bm25"
    rag_reranker_model: str = "BAAI/bge-reranker-base"
    rag_model_cache: str | None = None
    rag_top_k: int = 20  # candidates from hybrid search
    rag_top_n: int = 4  # passages that go into the prompt
    rag_min_score: float = 0.3  # reranker relevance threshold (0..1); calibrate with evals
    rag_condense_query: bool = True
    rag_append_source_link: bool = True
    rag_ingest_on_startup: bool = False  # index knowledge_dir at boot (handy for demos)

    # ── Shopify (Part 2) ──────────────────────────────────────────────────────
    shopify_shop: str = ""  # "brewline" or "brewline.myshopify.com"
    shopify_api_version: str = "2026-10"
    shopify_access_token: SecretStr | None = None  # legacy custom app token (shpat_...)
    shopify_client_id: str | None = None  # Dev Dashboard app -> client credentials grant
    shopify_client_secret: SecretStr | None = None

    max_tool_rounds: int = 4  # safety cap on LLM <-> tool iterations per turn

    # ── Privacy (Part 3) ──────────────────────────────────────────────────────
    # Comma-separated Fernet keys, newest first (old keys stay for decryption during rotation).
    # Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    pii_encryption_keys: SecretStr = SecretStr("")
    pii_blind_index_key: SecretStr = SecretStr("")
    pii_ner: bool = False  # Presidio NER for names/addresses (needs presidio-analyzer + spaCy model)
    retention_days_messages: int = 90  # delete conversation messages after N days
    retention_days_pii: int = 30  # delete vault entries after N days (placeholders become [EMAIL])
    shopify_webhook_secret: SecretStr | None = None  # app client secret: verifies GDPR webhooks

    # ── Escalation (Part 3) ───────────────────────────────────────────────────
    meta_app_id: str = ""  # our app id: lets us recognise thread control coming back to us
    meta_handover: bool = False  # pass Messenger/IG threads to the Page/IG inbox on hand-off
    helpdesk: Literal["none", "webhook", "zendesk"] = "none"
    helpdesk_webhook_url: str = ""
    helpdesk_webhook_secret: SecretStr = SecretStr("")
    zendesk_subdomain: str = ""
    zendesk_email: str = ""
    zendesk_api_token: SecretStr = SecretStr("")
    agent_api_key: SecretStr = SecretStr("")  # X-Agent-Key for the human-agent API
    human_auto_release_hours: int = 48  # give idle hand-offs back to the bot

    # ── Scale & reliability (Part 4) ──────────────────────────────────────────
    redis_url: str | None = None  # redis://redis:6379/0 - dedupe, locks, queue
    # inline = process in the API process (dev). redis = enqueue to Redis Streams; run `python -m app.worker`.
    queue_mode: Literal["inline", "redis"] = "inline"
    worker_concurrency: int = 8
    worker_max_deliveries: int = 5  # then the message goes to the dead-letter stream
    # Fallback LLMs, tried in order when the primary fails or its circuit is open. JSON list, e.g.
    # [{"provider":"openai","model":"my-model","base_url":"http://vllm:8000/v1","api_key_env":"VLLM_API_KEY"}]
    llm_fallbacks: str = "[]"
    llm_breaker_threshold: int = 5
    llm_breaker_cooldown_s: float = 30.0

    # ── Enterprise (Part 4) ───────────────────────────────────────────────────
    tenants_dir: str | None = None  # load every *.yaml here (multi-tenant); else TENANT_CONFIG_PATH
    knowledge_root: str | None = None  # knowledge/<tenant_id>/ per tenant; else KNOWLEDGE_DIR
    admin_bootstrap_key: SecretStr = SecretStr("")  # first admin key; create real keys, then remove it

    @property
    def ig_token(self) -> SecretStr:
        return self.instagram_access_token or self.page_access_token

    @property
    def ig_messaging_node(self) -> str:
        return self.instagram_messaging_node or self.page_id


@lru_cache
def get_settings() -> Settings:
    """Cached accessor so every module shares one Settings instance."""
    return Settings()
