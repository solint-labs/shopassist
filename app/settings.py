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

    # When true, outbound messages are logged instead of sent. Handy for local runs
    # against recorded webhook payloads and for staging environments.
    dry_run_outbound: bool = False

    # Conversation memory: how many past turns the LLM sees.
    history_turns: int = Field(default=12, ge=2, le=50)

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
