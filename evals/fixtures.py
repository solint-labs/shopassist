"""Deterministic Shopify stand-in for evals (same interface as ShopifyClient)."""

from __future__ import annotations

ORDER_1042 = {
    "id": "gid://shopify/Order/1042", "name": "#1042", "createdAt": "2026-11-08T10:00:00Z",
    "cancelledAt": None, "closedAt": None, "displayFinancialStatus": "PAID", "displayFulfillmentStatus": "FULFILLED",
    "email": "asha@example.com", "phone": "+91 98000 00001",
    "shippingAddress": {"city": "Pune", "provinceCode": "MH", "countryCodeV2": "IN", "phone": "+91 98000 00001"},
    "totalPriceSet": {"shopMoney": {"amount": "1240.00", "currencyCode": "INR"}},
    "lineItems": {"nodes": [{"title": "Ethiopia Yirgacheffe 250g", "quantity": 2}]},
    "fulfillments": [{
        "status": "SUCCESS", "displayStatus": "IN_TRANSIT", "inTransitAt": "2026-11-10T08:00:00Z", "deliveredAt": None,
        "createdAt": "2026-11-09T08:00:00Z", "estimatedDeliveryAt": "2026-11-14T18:00:00Z",
        "trackingInfo": [{"company": "Delhivery", "number": "DL123456789IN", "url": "https://track.example/DL123456789IN"}],
        "fulfillmentLineItems": {"nodes": [{"id": "gid://shopify/FulfillmentLineItem/1", "quantity": 2,
                                            "lineItem": {"title": "Ethiopia Yirgacheffe 250g",
                                                         "product": {"productType": "Coffee"}}}]},
    }],
}


class FakeShopify:
    def __init__(self, orders: dict[str, dict] | None = None):
        self.orders = orders or {"1042": ORDER_1042}
        self.mutations: list[tuple[str, object]] = []

    async def find_order(self, number: str):
        return self.orders.get(str(number).lstrip("#"))

    async def search_products(self, text: str, limit: int = 5):
        return [{"title": "Ethiopia Yirgacheffe 250g", "onlineStoreUrl": "https://brewline.example/p/ethiopia",
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
        return {"id": "gid://shopify/Return/1", "name": "#1042-R1", "status": "REQUESTED"}

    async def aclose(self) -> None:
        return None
