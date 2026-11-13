"""Deterministic Shopify stand-in for evals (same interface as ShopifyClient).

Orders (all belong to WhatsApp number 919800000001 / asha@example.com):
  #1042  shipped, in transit with Delhivery tracking        -> order status, "can't cancel"
  #1043  unfulfilled (not roasted yet)                      -> cancellation flow
  #1044  coffee, delivered 5 days ago                       -> return flow (unopened / opened)
  #1045  V60 dripper (equipment), delivered 2 days ago      -> damaged-item escalation
  #1046  unfulfilled, high value (9,800 INR)                -> high-value escalation
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

PHONE, EMAIL = "+91 98000 00001", "asha@example.com"


def _order(number: str, fulfillment: str, items: list[tuple[str, int, str]], total: str = "1240.00",
           delivered_days_ago: int | None = None, in_transit: bool = False) -> dict:
    delivered = (datetime.now(timezone.utc) - timedelta(days=delivered_days_ago)).isoformat() \
        if delivered_days_ago is not None else None
    fulfillments = []
    if fulfillment != "UNFULFILLED":
        fulfillments = [{
            "status": "SUCCESS", "displayStatus": "DELIVERED" if delivered else "IN_TRANSIT",
            "inTransitAt": "2026-11-10T08:00:00Z", "deliveredAt": delivered, "createdAt": "2026-11-09T08:00:00Z",
            "estimatedDeliveryAt": None if delivered else "2026-11-16T18:00:00Z",
            "trackingInfo": [{"company": "Delhivery", "number": "DL123456789IN",
                              "url": "https://track.example/DL123456789IN"}],
            "fulfillmentLineItems": {"nodes": [
                {"id": f"gid://shopify/FulfillmentLineItem/{number}{i}", "quantity": q,
                 "lineItem": {"title": t, "product": {"productType": ptype}}}
                for i, (t, q, ptype) in enumerate(items, 1)]},
        }]
    return {
        "id": f"gid://shopify/Order/{number}", "name": f"#{number}", "createdAt": "2026-11-08T10:00:00Z",
        "cancelledAt": None, "closedAt": None, "displayFinancialStatus": "PAID",
        "displayFulfillmentStatus": fulfillment, "email": EMAIL, "phone": PHONE,
        "shippingAddress": {"city": "Pune", "provinceCode": "MH", "countryCodeV2": "IN", "phone": PHONE},
        "totalPriceSet": {"shopMoney": {"amount": total, "currencyCode": "INR"}},
        "lineItems": {"nodes": [{"title": t, "quantity": q} for t, q, _ in items]},
        "fulfillments": fulfillments,
    }


def default_orders() -> dict[str, dict]:
    coffee = [("Ethiopia Yirgacheffe 250g", 2, "Coffee")]
    return {
        "1042": _order("1042", "FULFILLED", coffee, in_transit=True),
        "1043": _order("1043", "UNFULFILLED", coffee),
        "1044": _order("1044", "FULFILLED", coffee, delivered_days_ago=5),
        "1045": _order("1045", "FULFILLED", [("Hario V60 Dripper", 1, "Equipment")], total="1450.00",
                       delivered_days_ago=2),
        "1046": _order("1046", "UNFULFILLED", [("Brewline Barista Grinder", 1, "Equipment")], total="9800.00"),
    }


class FakeShopify:
    def __init__(self, orders: dict[str, dict] | None = None):
        self.orders = orders or default_orders()
        self.mutations: list[tuple[str, object]] = []

    async def find_order(self, number: str):
        o = self.orders.get(str(number).lstrip("#"))
        return copy.deepcopy(o) if o else None

    async def search_products(self, text: str, limit: int = 5):
        return [{"title": "Ethiopia Yirgacheffe 250g", "onlineStoreUrl": "https://brewline.example/products/ethiopia",
                 "description": "Floral, berry-sweet washed Ethiopian coffee.",
                 "priceRangeV2": {"minVariantPrice": {"amount": "620.00", "currencyCode": "INR"},
                                  "maxVariantPrice": {"amount": "620.00", "currencyCode": "INR"}},
                 "variants": {"nodes": [{"title": "Whole bean", "price": "620.00", "availableForSale": True},
                                        {"title": "French press grind", "price": "620.00", "availableForSale": False}]}}]

    async def cancel_order(self, order_gid: str, staff_note: str = ""):
        self.mutations.append(("cancel", order_gid))
        return {"id": "gid://shopify/Job/1", "done": False}

    async def request_return(self, order_gid: str, items):
        self.mutations.append(("return", items))
        return {"id": "gid://shopify/Return/1", "name": "#R1", "status": "REQUESTED"}

    async def shop_policies(self):
        return []

    async def aclose(self) -> None:
        return None
