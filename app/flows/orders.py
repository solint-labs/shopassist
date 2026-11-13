"""Order flows: cancel an order, request a return.

Both flows only start for orders the customer has already verified (see
`get_order_status`), re-read the order from Shopify (never trusting the chat
history), apply the store's policy in code, ask for explicit confirmation, and only
then call the mutation.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from app.commerce.shopify import ShopifyError
from app.domain import AgentReply, QuickReply
from app.escalation.detector import EscalationDecision
from app.flows.engine import YES_NO, Flow, FlowContext, yes_no


def _total(order: dict) -> float:
    try:
        return float(order["totalPriceSet"]["shopMoney"]["amount"])
    except (KeyError, TypeError, ValueError):
        return 0.0


def _money(order: dict) -> str:
    m = (order.get("totalPriceSet") or {}).get("shopMoney") or {}
    return f"{m.get('amount')} {m.get('currencyCode')}" if m else ""


def _items_text(order: dict) -> str:
    return ", ".join(f"{li['quantity']} × {li['title']}" for li in (order.get("lineItems") or {}).get("nodes", []))


async def _load_verified_order(ctx: FlowContext, number: str) -> tuple[dict | None, AgentReply | None]:
    number = str(number).strip().lstrip("#")
    if number not in ctx.state.get("verified_orders", []):
        return None, AgentReply("I'll need to verify that order first. Could you share the order number and the "
                                "email you used at checkout?")
    if ctx.shopify is None:
        return None, await ctx.escalate(EscalationDecision("integration_unavailable", "normal"))
    order = await ctx.shopify.find_order(number)
    if order is None:
        return None, AgentReply(f"I couldn't find order #{number}. Could you double-check the number?")
    return order, None


class CancelOrderFlow(Flow):
    name = "cancel_order"

    async def start(self, ctx: FlowContext, args: dict[str, Any]) -> AgentReply:
        order, err = await _load_verified_order(ctx, args.get("order_number", ""))
        if err:
            self.finish(ctx)
            return err
        policy, esc = ctx.tenant.cancellation, ctx.tenant.escalation
        if order.get("cancelledAt"):
            self.finish(ctx)
            return AgentReply(f"Order {order['name']} is already cancelled. Refunds take 5-7 business days to show up.")
        if order.get("displayFulfillmentStatus") not in policy.allowed_fulfillment_statuses:
            self.finish(ctx)
            days = ctx.tenant.returns.unopened_perishable_days
            return AgentReply(
                f"Order {order['name']} has already been prepared or shipped "
                f"(status: {str(order.get('displayFulfillmentStatus', '')).replace('_', ' ').lower()}), "
                f"so it can't be cancelled anymore. Once it arrives, unopened items can be returned within {days} days. "
                "I can help you start a return then.")
        if _total(order) > esc.high_value_threshold:
            self.finish(ctx)
            return await ctx.escalate(EscalationDecision(
                "high_value", "normal", f"cancel {order['name']} worth {_money(order)}"))
        self.goto(ctx, "confirm", order_gid=order["id"], order_name=order["name"])
        return AgentReply(f"Just to confirm: cancel order {order['name']} ({_items_text(order)}, {_money(order)})? "
                          "You'll get a full refund to your original payment method.",
                          quick_replies=[QuickReply("yes", "Yes, cancel it"), QuickReply("no", "No, keep it")])

    async def handle(self, ctx: FlowContext) -> AgentReply:
        data = ctx.flow["data"]
        answer = yes_no(ctx.msg)
        if answer is None:
            return self.reask(ctx, f"Should I cancel order {data['order_name']}? Please tap Yes or No.",
                              [QuickReply("yes", "Yes, cancel it"), QuickReply("no", "No, keep it")])
        self.finish(ctx)
        if not answer:
            return AgentReply(f"No problem, order {data['order_name']} stays as it is.")
        try:
            await ctx.shopify.cancel_order(data["order_gid"], staff_note=f"Cancelled by customer via {ctx.msg.channel}")
        except ShopifyError as e:
            return await ctx.escalate(EscalationDecision("action_failed", "high", f"orderCancel failed: {e}"))
        return AgentReply(f"Done. Order {data['order_name']} is cancelled ✅ Your refund goes back to the original "
                          "payment method within 5-7 business days, and you'll get a confirmation email.",
                          meta={"action": "order_cancelled"})


REASONS = [QuickReply("damaged", "Damaged/wrong item"), QuickReply("unopened", "Unopened coffee"),
           QuickReply("unused_gear", "Unused equipment")]


def classify_reason(text: str, payload: str | None) -> str | None:
    if payload in {"damaged", "unopened", "unused_gear", "opened"}:
        return payload
    t = text.lower()
    if re.search(r"damag|broken|crack|leak|wrong|torn|missing", t):
        return "damaged"
    if re.search(r"\bunopened|sealed|not opened|never opened", t):
        return "unopened"
    if re.search(r"\bopened|didn'?t like|taste|bitter|sour|don'?t like", t):
        return "opened"
    if re.search(r"unused|never used|not used|don'?t need|changed my mind", t):
        return "unused_gear"
    return None


class ReturnRequestFlow(Flow):
    name = "return_request"

    async def start(self, ctx: FlowContext, args: dict[str, Any]) -> AgentReply:
        order, err = await _load_verified_order(ctx, args.get("order_number", ""))
        if err:
            self.finish(ctx)
            return err
        items, delivered = [], None
        for f in order.get("fulfillments") or []:
            ts = f.get("deliveredAt") or f.get("createdAt")
            if ts:
                d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                delivered = max(delivered, d) if delivered else d
            for fli in (f.get("fulfillmentLineItems") or {}).get("nodes", []):
                li = fli["lineItem"]
                items.append({"id": fli["id"], "title": li["title"], "qty": fli["quantity"],
                              "type": ((li.get("product") or {}).get("productType") or "")})
        if not items:
            self.finish(ctx)
            return AgentReply(f"Order {order['name']} hasn't shipped yet, so there's nothing to return. "
                              "Would you like to cancel it instead?")
        self.goto(ctx, "item", order_gid=order["id"], order_name=order["name"], items=items,
                  delivered_at=delivered.isoformat() if delivered else None)
        if len(items) == 1:
            ctx.flow["data"]["item"] = items[0]
            ctx.flow["step"] = "reason"
            return self._ask_reason(items[0])
        lines = "\n".join(f"{i}. {it['title']} (×{it['qty']})" for i, it in enumerate(items, 1))
        quick = [QuickReply(f"item:{i}", it["title"][:20]) for i, it in enumerate(items[:3], 1)]
        return AgentReply(f"Which item would you like to return from {order['name']}?\n{lines}", quick_replies=quick)

    @staticmethod
    def _ask_reason(item: dict) -> AgentReply:
        return AgentReply(f"Got it: {item['title']}. What's the reason for the return? "
                          "You can also describe it in your own words.", quick_replies=REASONS)

    async def handle(self, ctx: FlowContext) -> AgentReply:
        step, data = ctx.flow["step"], ctx.flow["data"]
        if step == "item":
            m = re.search(r"(\d+)", ctx.msg.payload or ctx.msg.text)
            idx = int(m.group(1)) if m else 0
            if not 1 <= idx <= len(data["items"]):
                return self.reask(ctx, "Please reply with the item number from the list.", [])
            data["item"] = data["items"][idx - 1]
            ctx.flow["step"] = "reason"
            return self._ask_reason(data["item"])

        if step == "reason":
            reason = classify_reason(ctx.msg.text, ctx.msg.payload)
            if reason is None:
                return self.reask(ctx, "Could you tell me a bit more: is it damaged or wrong, unopened coffee, "
                                       "or unused equipment?", REASONS)
            return await self._apply_policy(ctx, reason)

        if step == "confirm":
            answer = yes_no(ctx.msg)
            if answer is None:
                return self.reask(ctx, "Shall I submit the return request?", YES_NO)
            self.finish(ctx)
            if not answer:
                return AgentReply("Okay, I haven't submitted anything. Let me know if you change your mind.")
            item = {"fulfillmentLineItemId": data["item"]["id"], "quantity": data["item"]["qty"],
                    "customerNote": f"Reason: {data['reason']} (via {ctx.msg.channel})"}
            if rid := ctx.tenant.returns.reason_definition_ids.get(data["reason"]):
                item["returnReasonDefinitionId"] = rid
            try:
                ret = await ctx.shopify.request_return(data["order_gid"], [item])
            except ShopifyError as e:
                return await ctx.escalate(EscalationDecision("action_failed", "high", f"returnRequest failed: {e}"))
            return AgentReply(f"Your return request {ret.get('name') or ''} for {data['item']['title']} is in ✅ "
                              "We'll review it within 1 business day and email you the return instructions.",
                              meta={"action": "return_requested"})
        self.finish(ctx)
        return AgentReply("Let's start over. What would you like to do?")

    async def _apply_policy(self, ctx: FlowContext, reason: str) -> AgentReply:
        data, policy = ctx.flow["data"], ctx.tenant.returns
        title = data["item"]["title"]
        ptype = data["item"].get("type", "").lower()
        perishable = ptype in {t.lower() for t in policy.perishable_product_types} or \
            any(k in title.lower() for k in policy.perishable_keywords)
        delivered = datetime.fromisoformat(data["delivered_at"]) if data.get("delivered_at") else None
        days = (datetime.now(timezone.utc) - delivered).days if delivered else 0

        if reason == "damaged":
            self.finish(ctx)
            reply = await ctx.escalate(EscalationDecision(
                "damaged_item", "normal", f"{title} in {data['order_name']}, reported {days} days after delivery"))
            reply.text = ("I'm sorry it arrived like that. Please send a photo of the item and the packaging here. "
                          + reply.text)
            return reply
        if perishable and reason in ("opened", "unused_gear"):
            self.finish(ctx)
            return AgentReply("Opened coffee can't be returned because it's a food product. If you didn't enjoy it, "
                              "tell me what you didn't like. Our Taste Promise may cover a different coffee for you.")
        window = policy.unopened_perishable_days if perishable else policy.equipment_days
        if days > window:
            self.finish(ctx)
            extra = "" if perishable else " Equipment is still covered by the 1-year warranty against defects."
            return AgentReply(f"I'm sorry, {title} was delivered {days} days ago and the return window is "
                              f"{window} days.{extra}")
        data["reason"] = reason
        ctx.flow["step"] = "confirm"
        return AgentReply(f"You're within the {window}-day window. Shall I submit a return request for "
                          f"{data['item']['qty']} × {title} from {data['order_name']}?", quick_replies=YES_NO)


class ForgetMeFlow(Flow):
    """Customer-initiated erasure of this conversation and the PII they shared in it."""

    name = "forget_me"

    async def start(self, ctx: FlowContext, args: dict[str, Any]) -> AgentReply:
        self.goto(ctx, "confirm")
        return AgentReply("I can delete this conversation and every personal detail you've shared in it. "
                          "This can't be undone. Should I go ahead?",
                          quick_replies=[QuickReply("yes", "Yes, delete it"), QuickReply("no", "No, keep it")])

    async def handle(self, ctx: FlowContext) -> AgentReply:
        answer = yes_no(ctx.msg)
        if answer is None:
            return self.reask(ctx, "Should I delete this conversation?", YES_NO)
        self.finish(ctx)
        if not answer:
            return AgentReply("Okay, nothing has been deleted.")
        await ctx.forget()
        email = ctx.tenant.brand.support_email
        return AgentReply("Done. I've deleted this conversation and the personal details you shared here. "
                          f"Your order records in our store are kept as the law requires. Email {email} to request "
                          "full account deletion.", meta={"forgotten": True})
