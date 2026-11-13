"""Shared fixtures: an isolated app with temp SQLite, in-memory Qdrant, fake LLM and mocked HTTP."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.channels.signature import compute_signature
from app.llm.fake import FakeLLM
from app.main import create_app
from app.settings import Settings
from tests.payloads import IG_ID, PAGE_ID, WA_PHONE_ID

ROOT = Path(__file__).resolve().parents[1]
APP_SECRET = "test-app-secret"
GRAPH = "https://graph.facebook.com/v27.0"
SHOPIFY_GQL = "https://brewline.myshopify.com/admin/api/2026-10/graphql.json"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        env="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        tenant_config_path=str(ROOT / "config/tenants/brewline.yaml"),
        knowledge_dir=str(ROOT / "knowledge/brewline"),
        llm_provider="fake",
        meta_app_secret=SecretStr(APP_SECRET),
        meta_verify_token=SecretStr("verify-me"),
        whatsapp_phone_number_id=WA_PHONE_ID,
        whatsapp_token=SecretStr("wa-token"),
        page_id=PAGE_ID,
        page_access_token=SecretStr("page-token"),
        instagram_account_id=IG_ID,
        qdrant_path=":memory:",
        rag_encoders="hashing",
        rag_ingest_on_startup=True,
        shopify_shop="brewline",
        shopify_access_token=SecretStr("shpat_test"),
        shopify_webhook_secret=SecretStr("shopify-secret"),
        agent_api_key=SecretStr("agent-key"),
        meta_app_id="APP_123",
        meta_handover=True,
    )


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


class ShopifyMock:
    """Serves canned GraphQL responses keyed by operation name."""

    def __init__(self):
        self.orders: dict[str, dict] = {}
        self.products: list[dict] = []
        self.requests: list[dict] = []
        self.mutations: list[tuple[str, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        q = body["query"]
        if "OrderByName" in q:
            name = body["variables"]["q"].split("#", 1)[-1]
            node = self.orders.get(name)
            return httpx.Response(200, json={"data": {"orders": {"nodes": [node] if node else []}}})
        if "Products" in q:
            return httpx.Response(200, json={"data": {"products": {"nodes": self.products}}})
        if "CancelOrder" in q:
            self.mutations.append(("cancel", body["variables"]))
            return httpx.Response(200, json={"data": {"orderCancel": {
                "job": {"id": "gid://shopify/Job/1", "done": False}, "orderCancelUserErrors": []}}})
        if "RequestReturn" in q:
            self.mutations.append(("return", body["variables"]))
            return httpx.Response(200, json={"data": {"returnRequest": {
                "return": {"id": "gid://shopify/Return/1", "name": "#1042-R1", "status": "REQUESTED"}, "userErrors": []}}})
        return httpx.Response(200, json={"data": {}})


@pytest.fixture
def shopify_mock() -> ShopifyMock:
    return ShopifyMock()


@pytest.fixture
def graph_mock(shopify_mock):
    """Intercept every outbound HTTP call: Graph API (recorded) and Shopify (canned)."""
    with respx.mock(assert_all_called=False) as mock:
        mock.post(SHOPIFY_GQL).mock(side_effect=shopify_mock.handler)
        mock.post(url__startswith=GRAPH).mock(return_value=httpx.Response(200, json={"message_id": "out_1"}))
        yield mock


@pytest.fixture
async def app(settings, fake_llm, graph_mock):
    application = create_app(settings, llm=fake_llm)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def signed(payload: dict) -> tuple[bytes, dict]:
    body = json.dumps(payload).encode()
    return body, {"x-hub-signature-256": compute_signature(APP_SECRET, body), "content-type": "application/json"}


def sent_bodies(graph_mock) -> list[tuple[str, dict]]:
    """(path, json body) for every Graph API call made, in order."""
    return [(c.request.url.path, json.loads(c.request.content or b"{}")) for c in graph_mock.calls
            if c.request.url.host == "graph.facebook.com"]


def sample_order(name="1042", phone="+91 98000 00001", email="asha@example.com", fulfillment="FULFILLED",
                 delivered_days_ago: int | None = 3, total="1240.00",
                 items=(("Ethiopia Yirgacheffe 250g", 2),)):
    from datetime import datetime, timedelta, timezone

    delivered = (datetime.now(timezone.utc) - timedelta(days=delivered_days_ago)).isoformat() \
        if delivered_days_ago is not None else None
    fulfillments = [] if fulfillment == "UNFULFILLED" else [{
        "status": "SUCCESS", "displayStatus": "DELIVERED" if delivered else "IN_TRANSIT",
        "inTransitAt": "2026-11-10T08:00:00Z", "deliveredAt": delivered, "createdAt": "2026-11-09T08:00:00Z",
        "estimatedDeliveryAt": "2026-11-14T18:00:00Z",
        "trackingInfo": [{"company": "Delhivery", "number": "DL123456789IN", "url": "https://track.example/DL123456789IN"}],
        "fulfillmentLineItems": {"nodes": [{"id": f"gid://shopify/FulfillmentLineItem/{i}", "quantity": q,
                                            "lineItem": {"title": t, "product": {"productType": "Equipment" if "Dripper" in t or "Grinder" in t
                                                                         else "Coffee"}}}
                                           for i, (t, q) in enumerate(items, 1)]},
    }]
    return {
        "id": "gid://shopify/Order/1", "name": f"#{name}", "createdAt": "2026-11-08T10:00:00Z",
        "cancelledAt": None, "closedAt": None, "displayFinancialStatus": "PAID",
        "displayFulfillmentStatus": fulfillment, "email": email, "phone": phone,
        "shippingAddress": {"city": "Pune", "provinceCode": "MH", "countryCodeV2": "IN", "phone": phone},
        "totalPriceSet": {"shopMoney": {"amount": total, "currencyCode": "INR"}},
        "lineItems": {"nodes": [{"title": t, "quantity": q} for t, q in items]},
        "fulfillments": fulfillments,
    }
