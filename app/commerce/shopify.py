"""Shopify Admin GraphQL API client.

Authentication: two supported modes
  1. Static Admin API token (`shpat_...`) from a legacy custom app created in the Shopify
     admin before 1 January 2026. Those apps keep working.
  2. Client credentials grant for apps created in the Shopify Dev Dashboard (the only
     option for new custom apps since 1 January 2026, when app and store are in the same
     organization). The token lives ~24h (`expires_in` 86399); we cache it and refresh
     it automatically a few minutes before expiry.

Rate limiting: the Admin GraphQL API is cost-based (a leaky bucket of query points).
A throttled request returns errors[].extensions.code == "THROTTLED"; we back off and
retry. We also keep queries small (first: 1 / first: 5) so they stay cheap.

Protected customer data: reading an order's email/phone/name requires the
read_orders scope *and* protected customer data access configured for the app.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.logging_config import log


class ShopifyError(RuntimeError):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class _Token:
    value: str
    expires_at: float  # monotonic seconds; inf for static tokens


class ShopifyClient:
    def __init__(self, shop: str, api_version: str = "2026-10", access_token: str | None = None,
                 client_id: str | None = None, client_secret: str | None = None,
                 http: httpx.AsyncClient | None = None, max_retries: int = 4):
        if not (access_token or (client_id and client_secret)):
            raise ValueError("Provide SHOPIFY_ACCESS_TOKEN or SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET")
        self.shop = shop.removesuffix("/").removeprefix("https://")
        if not self.shop.endswith(".myshopify.com"):
            self.shop = f"{self.shop}.myshopify.com"
        self.api_version = api_version
        self.client_id, self.client_secret = client_id, client_secret
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))
        self.max_retries = max_retries
        self._token = _Token(access_token, float("inf")) if access_token else None
        self._token_lock = asyncio.Lock()

    @property
    def graphql_url(self) -> str:
        return f"https://{self.shop}/admin/api/{self.api_version}/graphql.json"

    async def _access_token(self) -> str:
        if self._token and time.monotonic() < self._token.expires_at:
            return self._token.value
        async with self._token_lock:  # one refresh even if many requests race here
            if self._token and time.monotonic() < self._token.expires_at:
                return self._token.value
            resp = await self.http.post(
                f"https://{self.shop}/admin/oauth/access_token",
                data={"grant_type": "client_credentials", "client_id": self.client_id,
                      "client_secret": self.client_secret},
                headers={"Accept": "application/json"},
            )
            if resp.status_code != 200:
                raise ShopifyError(f"token request failed: {resp.status_code} {resp.text[:200]}")
            body = resp.json()
            ttl = float(body.get("expires_in", 86399))
            self._token = _Token(body["access_token"], time.monotonic() + max(60.0, ttl - 300))  # refresh 5 min early
            log.info("shopify.token_refreshed", shop=self.shop, scope=body.get("scope"))
            return self._token.value

    async def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        delay = 1.0
        for attempt in range(self.max_retries + 1):
            token = await self._access_token()
            try:
                resp = await self.http.post(self.graphql_url, json={"query": query, "variables": variables or {}},
                                            headers={"X-Shopify-Access-Token": token})
            except httpx.TransportError as e:
                if attempt == self.max_retries:
                    raise ShopifyError(f"network error: {e}", retryable=True) from e
                await asyncio.sleep(delay)
                delay *= 2
                continue
            if resp.status_code == 401 and self.client_id:
                self._token = None  # token revoked/rotated: fetch a fresh one and retry
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == self.max_retries:
                    raise ShopifyError(f"HTTP {resp.status_code}", retryable=True)
                await asyncio.sleep(float(resp.headers.get("Retry-After", delay)))
                delay *= 2
                continue
            if resp.status_code >= 400:
                raise ShopifyError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            body = resp.json()
            errors = body.get("errors") or []
            if any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errors):
                if attempt == self.max_retries:
                    raise ShopifyError("throttled", retryable=True)
                await asyncio.sleep(_throttle_wait(body, delay))
                delay *= 2
                continue
            if errors:
                raise ShopifyError("; ".join(e.get("message", "") for e in errors))
            return body["data"]
        raise ShopifyError("exhausted retries", retryable=True)

    # ── Queries used by the agent ────────────────────────────────────────────

    async def find_order(self, order_number: str) -> dict[str, Any] | None:
        name = order_number.strip().lstrip("#")
        data = await self.graphql(ORDER_QUERY, {"q": f"name:#{name}"})
        nodes = data["orders"]["nodes"]
        return nodes[0] if nodes else None

    async def search_products(self, text: str, limit: int = 5) -> list[dict[str, Any]]:
        safe = text.replace('"', " ").replace("\\", " ").strip()
        data = await self.graphql(PRODUCTS_QUERY, {"q": f"status:active AND ({safe})", "n": limit})
        return data["products"]["nodes"]

    async def shop_policies(self) -> list[dict[str, Any]]:
        data = await self.graphql(POLICIES_QUERY)
        return data["shop"]["shopPolicies"]

    async def pages(self, limit: int = 50) -> list[dict[str, Any]]:
        data = await self.graphql(PAGES_QUERY, {"n": limit})
        return data["pages"]["nodes"]

    async def all_products(self, page_size: int = 50) -> list[dict[str, Any]]:
        out, cursor = [], None
        while True:
            data = await self.graphql(CATALOG_QUERY, {"n": page_size, "after": cursor})
            conn = data["products"]
            out += conn["nodes"]
            if not conn["pageInfo"]["hasNextPage"]:
                return out
            cursor = conn["pageInfo"]["endCursor"]

    # ── Mutations used by flows (Part 3) ─────────────────────────────────────

    async def cancel_order(self, order_gid: str, staff_note: str = "") -> dict[str, Any]:
        """Cancel, refund to the original payment method, restock, and email the customer.

        Irreversible. Only ever called from a flow *after* explicit customer confirmation.
        Needs the write_orders scope. Cancellation runs as an async job on Shopify's side.
        """
        data = await self.graphql(CANCEL_MUTATION, {"orderId": order_gid, "staffNote": staff_note[:255]})
        res = data["orderCancel"]
        if res.get("orderCancelUserErrors"):
            raise ShopifyError("; ".join(e["message"] for e in res["orderCancelUserErrors"]))
        return res.get("job") or {}

    async def request_return(self, order_gid: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        """Create a return in REQUESTED status (merchant approves/declines). Needs write_returns.

        items: [{"fulfillmentLineItemId", "quantity", "customerNote", optional "returnReasonDefinitionId"}]
        """
        data = await self.graphql(RETURN_REQUEST_MUTATION, {"input": {"orderId": order_gid, "returnLineItems": items}})
        res = data["returnRequest"]
        if res.get("userErrors"):
            raise ShopifyError("; ".join(e["message"] for e in res["userErrors"]))
        return res["return"]

    async def aclose(self) -> None:
        await self.http.aclose()


def _throttle_wait(body: dict, fallback: float) -> float:
    """Wait just long enough for the bucket to refill to the requested cost."""
    try:
        cost = body["extensions"]["cost"]
        status = cost["throttleStatus"]
        missing = cost["requestedQueryCost"] - status["currentlyAvailable"]
        return max(0.5, missing / max(status["restoreRate"], 1))
    except (KeyError, TypeError):
        return fallback


ORDER_QUERY = """
query OrderByName($q: String!) {
  orders(first: 1, query: $q) {
    nodes {
      id name createdAt cancelledAt closedAt
      displayFinancialStatus displayFulfillmentStatus
      email phone
      shippingAddress { city provinceCode countryCodeV2 phone }
      totalPriceSet { shopMoney { amount currencyCode } }
      lineItems(first: 20) { nodes { title quantity } }
      fulfillments(first: 5) {
        status displayStatus inTransitAt deliveredAt estimatedDeliveryAt createdAt
        trackingInfo(first: 3) { company number url }
        fulfillmentLineItems(first: 20) { nodes { id quantity lineItem { title product { productType } } } }
      }
    }
  }
}"""

PRODUCTS_QUERY = """
query Products($q: String!, $n: Int!) {
  products(first: $n, query: $q) {
    nodes {
      title handle onlineStoreUrl productType tags totalInventory
      description(truncateAt: 240)
      priceRangeV2 { minVariantPrice { amount currencyCode } maxVariantPrice { amount currencyCode } }
      variants(first: 6) { nodes { title price availableForSale } }
    }
  }
}"""

CATALOG_QUERY = """
query Catalog($n: Int!, $after: String) {
  products(first: $n, after: $after, query: "status:active") {
    pageInfo { hasNextPage endCursor }
    nodes { id title handle onlineStoreUrl productType tags descriptionHtml updatedAt
            variants(first: 10) { nodes { title price } } }
  }
}"""

POLICIES_QUERY = "query { shop { shopPolicies { title body type url } } }"

PAGES_QUERY = "query Pages($n: Int!) { pages(first: $n) { nodes { id title handle body updatedAt } } }"

CANCEL_MUTATION = """
mutation CancelOrder($orderId: ID!, $staffNote: String) {
  orderCancel(orderId: $orderId, reason: CUSTOMER, restock: true, notifyCustomer: true,
              refundMethod: {originalPaymentMethodsRefund: true}, staffNote: $staffNote) {
    job { id done }
    orderCancelUserErrors { field message code }
  }
}"""

RETURN_REQUEST_MUTATION = """
mutation RequestReturn($input: ReturnRequestInput!) {
  returnRequest(input: $input) {
    return { id name status }
    userErrors { field message }
  }
}"""
