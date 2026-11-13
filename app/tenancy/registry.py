"""Tenant registry: route every inbound message to the right store.

One deployment serves many stores (brands, regions, or an agency's clients). Each
tenant brings its own YAML (persona, policies, escalation rules, Meta assets, secret
references, Shopify shop) and its own Qdrant collection, Shopify client, tools and
hand-off settings. Shared: the database, the LLM providers, the queue, the workers.

Routing key: the business id on the webhook (WhatsApp phone_number_id, Facebook page
id, Instagram account id). Unknown business ids are dropped and logged; they usually
mean a Meta asset was connected to the app but not configured.

Isolation guarantees:
  * knowledge: one collection per tenant, so a retrieval can never cross stores,
  * conversations, profiles and audit rows carry tenant_id, and API keys can be tenant-scoped,
  * secrets are references ("env:", "file:") resolved at startup, never stored in YAML.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from app.agent.orchestrator import Agent
from app.agent.tenant import TenantConfig, load_tenant_config
from app.channels.gateway import ChannelCredentials
from app.domain import Channel, InboundMessage
from app.logging_config import log
from app.settings import Settings


def resolve_secret(ref: str | None) -> str | None:
    """'env:NAME' -> os.environ['NAME']; 'file:/path' -> file contents; anything else is a literal."""
    if not ref:
        return None
    if ref.startswith("env:"):
        return os.environ.get(ref[4:])
    if ref.startswith("file:"):
        return Path(ref[5:]).read_text().strip()
    return ref


def load_tenant_configs(s: Settings) -> list[TenantConfig]:
    if s.tenants_dir:
        configs = [load_tenant_config(p) for p in sorted(Path(s.tenants_dir).glob("*.yaml"))]
        if not configs:
            raise RuntimeError(f"no tenant configs in {s.tenants_dir}")
        return configs
    return [load_tenant_config(s.tenant_config_path)]


@dataclass
class TenantRuntime:
    config: TenantConfig
    agent: Agent
    credentials: dict[Channel, ChannelCredentials] = field(default_factory=dict)


class TenantRegistry:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.tenants: dict[str, TenantRuntime] = {}
        self._by_business: dict[str, str] = {}

    def add(self, rt: TenantRuntime) -> None:
        cfg = rt.config
        self.tenants[cfg.tenant_id] = rt
        for bid in (cfg.channels.whatsapp_phone_number_id, cfg.channels.page_id, cfg.channels.instagram_account_id):
            if bid:
                self._by_business[bid] = cfg.tenant_id
        rt.credentials = self._credentials(cfg)

    @property
    def default(self) -> TenantRuntime:
        return next(iter(self.tenants.values()))

    def for_tenant(self, tenant_id: str) -> TenantRuntime:
        return self.tenants.get(tenant_id) or self.default

    def resolve(self, msg: InboundMessage) -> TenantRuntime | None:
        tid = self._by_business.get(msg.business_id)
        if tid:
            return self.tenants[tid]
        if len(self.tenants) == 1:  # single-store mode: everything belongs to the one tenant
            return self.default
        log.warning("tenancy.unknown_business_id", business_id=msg.business_id, channel=str(msg.channel))
        return None

    def agent_for(self, msg: InboundMessage) -> Agent:
        rt = self.resolve(msg)
        if rt is None:
            raise LookupError(f"no tenant for business id {msg.business_id}")
        return rt.agent

    # CredentialResolver protocol for the ChannelGateway
    def for_message(self, msg: InboundMessage) -> ChannelCredentials:
        rt = self.resolve(msg) or self.default
        cred = rt.credentials.get(msg.channel)
        if cred is None:
            raise LookupError(f"no {msg.channel} credentials for tenant {rt.config.tenant_id}")
        if msg.channel != Channel.INSTAGRAM and msg.business_id:
            return ChannelCredentials(cred.token, msg.business_id)
        return cred

    def _credentials(self, cfg: TenantConfig) -> dict[Channel, ChannelCredentials]:
        """Tenant secrets if configured, else the global settings (single-store mode)."""
        s, sec, ch = self.settings, cfg.secrets, cfg.channels
        page_token = resolve_secret(sec.page_access_token) or s.page_access_token.get_secret_value()
        ig_token = resolve_secret(sec.instagram_access_token) or (
            s.instagram_access_token.get_secret_value() if s.instagram_access_token else page_token)
        return {
            Channel.WHATSAPP: ChannelCredentials(
                resolve_secret(sec.whatsapp_token) or s.whatsapp_token.get_secret_value(),
                ch.whatsapp_phone_number_id or s.whatsapp_phone_number_id),
            Channel.MESSENGER: ChannelCredentials(page_token, ch.page_id or s.page_id),
            Channel.INSTAGRAM: ChannelCredentials(
                ig_token, ch.instagram_messaging_node or ch.page_id or s.ig_messaging_node),
        }
