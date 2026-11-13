"""Tools the LLM can call, and the registry that runs them safely.

Design rules for support tools:
  * The model chooses *which* tool to call; the tool decides *what may be revealed*.
    Identity checks live in code, never in the prompt, because prompts can be talked
    around and code can't.
  * Tool results are minimal. The model only gets the fields it needs to answer
    (no full address, no payment details), which also limits what can leak.
  * Tools never raise into the agent loop: failures become {"error": ...} results the
    model can explain ("I can't reach our order system right now").
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.commerce.shopify import ShopifyClient, ShopifyError
from app.domain import Channel, InboundMessage
from app.llm.base import ToolCall, ToolSpec
from app.logging_config import log

MAX_VERIFY_ATTEMPTS = 5


@dataclass
class ToolContext:
    msg: InboundMessage
    conversation_id: str
    state: dict[str, Any] = field(default_factory=dict)  # persisted per conversation
    # Side-channel for tools that change control flow (escalate, start a flow). The
    # orchestrator acts on these after the LLM turn, so a tool never sends messages itself.
    signals: dict[str, Any] = field(default_factory=dict)


Handler = Callable[[ToolContext, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class Tool:
    spec: ToolSpec
    handler: Handler


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None):
        self._tools: dict[str, Tool] = {}
        for t in tools or []:
            self.register(t)

    def register(self, tool: Tool) -> None:
        self._tools[tool.spec.name] = tool

    def specs(self) -> list[ToolSpec]:
        return [t.spec for t in self._tools.values()]

    def __bool__(self) -> bool:
        return bool(self._tools)

    async def execute(self, call: ToolCall, ctx: ToolContext) -> dict[str, Any]:
        tool = self._tools.get(call.name)
        if tool is None:
            return {"error": f"unknown tool {call.name}"}
        missing = [k for k in tool.spec.parameters.get("required", []) if not call.arguments.get(k)]
        if missing:
            return {"error": f"missing required arguments: {', '.join(missing)}"}
        try:
            result = await tool.handler(ctx, call.arguments)
        except ShopifyError as e:
            log.warning("tool.shopify_error", tool=call.name, error=str(e))
            return {"error": "The store system is temporarily unavailable. Apologise and offer to follow up."}
        except Exception as e:  # noqa: BLE001
            log.exception("tool.failed", tool=call.name, error=str(e))
            return {"error": "Tool failed unexpectedly."}
        log.info("tool.executed", tool=call.name)
        return result


# ── Identity verification ─────────────────────────────────────────────────────


def _digits(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")


def phones_match(a: str | None, b: str | None) -> bool:
    """Compare the last 10 digits, so +91 98xxx and 098xxx match; too-short numbers never do."""
    da, db = _digits(a), _digits(b)
    return len(da) >= 8 and len(db) >= 8 and da[-10:] == db[-10:]


def is_verified_for_order(order: dict[str, Any], msg: InboundMessage, email: str | None, phone: str | None) -> bool:
    order_phones = [order.get("phone"), (order.get("shippingAddress") or {}).get("phone")]
    # WhatsApp gives us the customer's verified phone number for free.
    if msg.channel == Channel.WHATSAPP and any(phones_match(msg.user_id, p) for p in order_phones):
        return True
    if email and order.get("email") and email.strip().lower() == order["email"].strip().lower():
        return True
    if phone and any(phones_match(phone, p) for p in order_phones):
        return True
    return False


def summarize_order(o: dict[str, Any]) -> dict[str, Any]:
    """Only what a support answer needs. No street address, no payment details."""
    fulfillments = []
    for f in o.get("fulfillments") or []:
        fulfillments.append({
            "status": f.get("displayStatus") or f.get("status"),
            "in_transit_at": f.get("inTransitAt"),
            "estimated_delivery": f.get("estimatedDeliveryAt"),
            "delivered_at": f.get("deliveredAt"),
            "tracking": [{"carrier": t.get("company"), "number": t.get("number"), "url": t.get("url")}
                         for t in f.get("trackingInfo") or []],
        })
    ship = o.get("shippingAddress") or {}
    total = (o.get("totalPriceSet") or {}).get("shopMoney") or {}
    return {
        "order": o.get("name"),
        "placed_at": o.get("createdAt"),
        "cancelled": bool(o.get("cancelledAt")),
        "payment_status": o.get("displayFinancialStatus"),
        "fulfillment_status": o.get("displayFulfillmentStatus"),
        "items": [f"{li['quantity']} × {li['title']}" for li in (o.get("lineItems") or {}).get("nodes", [])],
        "total": f"{total.get('amount')} {total.get('currencyCode')}" if total else None,
        "ship_to_city": ", ".join(x for x in [ship.get("city"), ship.get("countryCodeV2")] if x) or None,
        "shipments": fulfillments,
    }


# ── Tool definitions ──────────────────────────────────────────────────────────


def build_commerce_tools(shopify: ShopifyClient) -> list[Tool]:
    async def get_order_status(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        number = str(args["order_number"]).strip().lstrip("#")
        verified = ctx.state.setdefault("verified_orders", [])
        attempts = ctx.state.get("verify_failures", 0)
        if number not in verified and attempts >= MAX_VERIFY_ATTEMPTS:
            return {"status": "locked", "instruction": "Too many failed attempts. Offer to connect them with the team."}

        order = await shopify.find_order(number)
        if order and (number in verified or is_verified_for_order(order, ctx.msg, args.get("email"), args.get("phone"))):
            if number not in verified:
                verified.append(number)
            ctx.state["last_order"] = number
            return {"status": "ok", "order_details": summarize_order(order)}

        # Same answer whether the order doesn't exist or the details don't match,
        # so the tool can't be used to discover which order numbers are valid.
        ctx.state["verify_failures"] = attempts + 1
        need = "the email address used at checkout" if ctx.msg.channel != Channel.WHATSAPP else \
            "the email address used at checkout (the WhatsApp number didn't match the order)"
        return {"status": "unverified",
                "instruction": f"Couldn't match that order to this customer. Ask them to double-check the order number "
                               f"and share {need}. Do not say whether the order exists."}

    async def search_products(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        nodes = await shopify.search_products(str(args["query"]))
        products = []
        for p in nodes:
            pr = p.get("priceRangeV2") or {}
            lo, hi = (pr.get("minVariantPrice") or {}), (pr.get("maxVariantPrice") or {})
            variants = (p.get("variants") or {}).get("nodes", [])
            products.append({
                "title": p["title"],
                "url": p.get("onlineStoreUrl"),
                "price": f"{lo.get('amount')}" + (f"-{hi.get('amount')}" if hi.get("amount") != lo.get("amount") else "")
                         + f" {lo.get('currencyCode', '')}".rstrip(),
                "in_stock_variants": [v["title"] for v in variants if v.get("availableForSale")],
                "out_of_stock_variants": [v["title"] for v in variants if not v.get("availableForSale")],
                "summary": p.get("description"),
            })
        return {"products": products} if products else {"products": [], "note": "No matching active products."}

    return [
        Tool(ToolSpec(
            name="get_order_status",
            description=("Look up a customer's order: payment and fulfillment status, items, shipment tracking and "
                         "delivery estimates. Requires the order number. On Messenger/Instagram also pass the email "
                         "used at checkout; on WhatsApp the customer's phone is checked automatically."),
            parameters={"type": "object", "properties": {
                "order_number": {"type": "string", "description": "Order number, e.g. 1042 or #1042"},
                "email": {"type": "string", "description": "Email used at checkout, if the customer gave it"},
                "phone": {"type": "string", "description": "Phone used at checkout, if the customer gave it"},
            }, "required": ["order_number"]},
        ), get_order_status),
        Tool(ToolSpec(
            name="search_products",
            description=("Search the live catalog for products: current prices, variants and stock. Use for "
                         "availability or price questions. Product descriptions/brewing info are in the knowledge base."),
            parameters={"type": "object", "properties": {
                "query": {"type": "string", "description": "Product keywords, e.g. 'ethiopia decaf' or 'burr grinder'"},
            }, "required": ["query"]},
        ), search_products),
    ]


TOOLS_GUIDANCE = """Live store data is available through tools:
- Order status, shipping or tracking questions -> call get_order_status. You need the order number. On Messenger
  and Instagram also ask for the email used at checkout before calling. Never reveal order details that a tool
  didn't return.
- Price or stock questions -> call search_products.
Only state live facts (status, stock, prices, tracking) that a tool returned in this conversation."""
