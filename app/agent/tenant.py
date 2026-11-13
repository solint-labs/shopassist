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


class BusinessHours(BaseModel):
    timezone: str = "UTC"
    days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])  # Monday = 0
    start: str = "09:00"
    end: str = "18:00"


class EscalationConfig(BaseModel):
    """When to bring in a human (Part 3). Every trigger can be tuned per store."""

    # Extra phrases on top of the built-in English patterns (see escalation/detector.py),
    # e.g. other languages your customers write in.
    human_request_keywords: list[str] = Field(default_factory=list)
    legal_keywords: list[str] = Field(default_factory=lambda: [
        "lawyer", "legal action", "consumer court", "consumer forum", "police", "fraud", "scam", "chargeback", "sue"])
    safety_keywords: list[str] = Field(default_factory=lambda: [
        "allergic", "allergy", "injury", "injured", "burn", "burnt my", "choking", "glass in", "sick after", "food poisoning"])
    frustration_threshold: float = 0.6  # 0..1 from the frustration scorer
    max_failed_answers: int = 2  # consecutive "I don't know"/ungrounded answers before escalating
    high_value_threshold: float = 5000.0  # order total above which refunds/cancellations go to a human
    business_hours: BusinessHours = BusinessHours()
    in_hours_message: str = "I'm bringing in a teammate who can help with this. They'll reply right here shortly."
    out_of_hours_message: str = (
        "I've passed this to our team. They're offline right now but will reply here as soon as they're back.")


class ReturnsPolicy(BaseModel):
    """Machine-readable return rules. The help-center text explains them; code enforces them."""

    # An item is perishable if its Shopify product type matches, or (fallback) its title has a keyword.
    perishable_product_types: list[str] = Field(default_factory=lambda: ["coffee", "tea", "food"])
    perishable_keywords: list[str] = Field(default_factory=lambda: ["coffee", "beans", "ground", "blend"])
    unopened_perishable_days: int = 14
    equipment_days: int = 30
    damaged_report_days: int = 7
    # Optional Shopify ReturnReasonDefinition ids, keyed by our reason codes.
    reason_definition_ids: dict[str, str] = Field(default_factory=dict)


class CancellationPolicy(BaseModel):
    allowed_fulfillment_statuses: list[str] = Field(default_factory=lambda: ["UNFULFILLED"])


class TenantConfig(BaseModel):
    tenant_id: str
    brand: Brand
    persona: Persona = Persona()
    scope: Scope
    comment_policy: CommentPolicy = CommentPolicy()
    extra_instructions: list[str] = Field(default_factory=list)
    escalation: EscalationConfig = EscalationConfig()
    returns: ReturnsPolicy = ReturnsPolicy()
    cancellation: CancellationPolicy = CancellationPolicy()


def load_tenant_config(path: str | Path) -> TenantConfig:
    with open(path, encoding="utf-8") as f:
        return TenantConfig.model_validate(yaml.safe_load(f))
