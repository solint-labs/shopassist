"""Tenant (store) configuration, loaded from YAML.

Everything a support lead might want to change without a deploy lives here: brand facts,
the agent's persona and tone dials, scope rules. Engineers own `settings.py`; the support
team owns `config/tenants/<store>.yaml`.

Pydantic validates the file at startup, so a typo fails the deploy, not a customer chat.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class Brand(BaseModel):
    name: str
    website: str
    support_email: str
    support_hours: str
    description: str = ""


class Persona(BaseModel):
    """The knobs that shape *how* the agent speaks (not *what* it knows)."""

    agent_name: str = "Assistant"
    voice: str = "friendly, clear and helpful"
    formality: Literal["casual", "neutral", "formal"] = "neutral"
    emoji: Literal["none", "light", "expressive"] = "light"
    verbosity: Literal["brief", "balanced", "detailed"] = "brief"
    language: str = "auto"  # "auto" = mirror the customer's language; or an ISO code like "en"
    signature_phrases: list[str] = Field(default_factory=list)  # optional brand phrases, used sparingly
    avoid_phrases: list[str] = Field(default_factory=list)  # e.g. "As an AI language model"


class Scope(BaseModel):
    allowed_topics: list[str]
    refuse_message: str = "I can only help with questions about our store, products and orders."


class CommentPolicy(BaseModel):
    """How to handle public Instagram comments."""

    reply_publicly: bool = True
    # Anything that needs personal details (orders, refunds, complaints) moves to DM.
    move_to_dm_topics: list[str] = Field(default_factory=lambda: ["order", "refund", "delivery", "complaint", "account"])


class TenantConfig(BaseModel):
    tenant_id: str
    brand: Brand
    persona: Persona = Persona()
    scope: Scope
    comment_policy: CommentPolicy = CommentPolicy()
    extra_instructions: list[str] = Field(default_factory=list)


def load_tenant_config(path: str | Path) -> TenantConfig:
    with open(path, encoding="utf-8") as f:
        return TenantConfig.model_validate(yaml.safe_load(f))
