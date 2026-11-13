"""Part 2: Shopify client auth/throttling, order verification, and the agent using tools end to end."""

import asyncio
import json

import httpx
import respx

from app.commerce.shopify import ShopifyClient
from app.commerce.tools import ToolContext, build_commerce_tools, phones_match
from app.domain import Channel, InboundMessage, MessageKind
from app.llm.base import LLMResponse, ToolCall
from tests import payloads as p
from tests.conftest import sample_order, sent_bodies, signed


# ── Shopify client ────────────────────────────────────────────────────────────

@respx.mock
async def test_client_credentials_token_is_cached_and_used():
    token = respx.post("https://brewline.myshopify.com/admin/oauth/access_token").mock(
        return_value=httpx.Response(200, json={"access_token": "shpat_new", "scope": "read_orders", "expires_in": 86399}))
    gql = respx.post("https://brewline.myshopify.com/admin/api/2026-10/graphql.json").mock(
        return_value=httpx.Response(200, json={"data": {"shop": {"shopPolicies": []}}}))
    c = ShopifyClient("brewline", client_id="cid", client_secret="secret")
    await c.shop_policies()
    await c.shop_policies()
    assert token.call_count == 1  # cached for ~24h
    assert gql.calls[0].request.headers["X-Shopify-Access-Token"] == "shpat_new"
    assert b"grant_type=client_credentials" in token.calls[0].request.content


@respx.mock
async def test_graphql_throttle_is_retried(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    throttled = {"errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
                 "extensions": {"cost": {"requestedQueryCost": 10, "throttleStatus": {"currentlyAvailable": 0, "restoreRate": 50}}}}
    respx.post("https://brewline.myshopify.com/admin/api/2026-10/graphql.json").mock(side_effect=[
        httpx.Response(200, json=throttled), httpx.Response(200, json={"data": {"shop": {"shopPolicies": [1]}}})])
    c = ShopifyClient("brewline", access_token="shpat_x")
    assert await c.shop_policies() == [1]


async def _no_sleep(_):
    return None


# ── Verification ─────────────────────────────────────────────────────────────

def test_phone_matching():
    assert phones_match("919800000001", "+91 98000 00001")
    assert phones_match("09800000001", "+919800000001")
    assert not phones_match("1234", "1234")


class _StubShopify:
    def __init__(self, order):
        self.order = order

    async def find_order(self, number):
        return self.order if self.order and self.order["name"] == f"#{number}" else None


def _ctx(channel, user_id="919800000001"):
    msg = InboundMessage(channel=channel, kind=MessageKind.DIRECT, message_id="m", user_id=user_id,
                         business_id="b", text="")
    return ToolContext(msg=msg, conversation_id="c1", state={})


async def test_whatsapp_phone_verifies_automatically():
    tool = build_commerce_tools(_StubShopify(sample_order()))[0]
    ctx = _ctx(Channel.WHATSAPP)
    res = await tool.handler(ctx, {"order_number": "#1042"})
    assert res["status"] == "ok" and res["order_details"]["shipments"][0]["tracking"][0]["carrier"] == "Delhivery"
    assert "street" not in json.dumps(res).lower()  # minimal data only
    assert ctx.state["verified_orders"] == ["1042"]


async def test_messenger_requires_email_and_never_leaks_existence():
    tool = build_commerce_tools(_StubShopify(sample_order()))[0]
    ctx = _ctx(Channel.MESSENGER, user_id="PSID_1")
    wrong = await tool.handler(ctx, {"order_number": "1042", "email": "nope@example.com"})
    missing = await tool.handler(ctx, {"order_number": "9999", "email": "asha@example.com"})
    assert wrong["status"] == missing["status"] == "unverified"  # identical: no enumeration
    ok = await tool.handler(ctx, {"order_number": "1042", "email": "ASHA@example.com"})
    assert ok["status"] == "ok"


async def test_lockout_after_repeated_failures():
    tool = build_commerce_tools(_StubShopify(sample_order()))[0]
    ctx = _ctx(Channel.MESSENGER, user_id="PSID_1")
    for _ in range(5):
        await tool.handler(ctx, {"order_number": "1042", "email": "guess@example.com"})
    res = await tool.handler(ctx, {"order_number": "1042", "email": "asha@example.com"})
    assert res["status"] == "locked"


# ── End to end: webhook -> tool call -> Shopify -> reply ─────────────────────

async def _settle():
    for _ in range(10):
        await asyncio.sleep(0)


async def test_order_status_on_whatsapp(client, graph_mock, shopify_mock, fake_llm):
    shopify_mock.orders["1042"] = sample_order()
    body, headers = signed(p.whatsapp_text("Where is my order #1042?"))
    await client.post("/webhooks/meta", content=body, headers=headers)
    await _settle()

    reply = [b for _, b in sent_bodies(graph_mock) if b.get("type") == "text"][0]["text"]["body"]
    assert "IN_TRANSIT" in reply or "ok" in reply
    tool_msgs = [m for m in fake_llm.calls[-1]["messages"] if m.role == "tool"]
    assert json.loads(tool_msgs[0].content)["status"] == "ok"


async def test_policy_answer_is_grounded_and_linked(client, graph_mock, fake_llm):
    body, headers = signed(p.messenger_text("Can I return opened coffee bags?", mid="m_ret"))
    await client.post("/webhooks/meta", content=body, headers=headers)
    await _settle()
    system = fake_llm.calls[-1]["system"]
    assert "<sources>" in system and "Returns and refunds" in system
    text = [b for _, b in sent_bodies(graph_mock) if "message" in b][0]["message"]["text"]
    assert "[1]" not in text and "https://brewline.example/policies/refund-policy" in text


async def test_tool_loop_is_capped(client, graph_mock, fake_llm, shopify_mock):
    loop_call = LLMResponse(text="", tool_calls=[ToolCall(id="x", name="search_products", arguments={"query": "v60"})])
    fake_llm.script = [loop_call] * 5 + [LLMResponse(text="Here's what I have so far.")]
    body, headers = signed(p.instagram_dm("do you have v60 drippers", mid="ig_loop"))
    await client.post("/webhooks/meta", content=body, headers=headers)
    await _settle()
    text = [b for _, b in sent_bodies(graph_mock) if "message" in b][0]["message"]["text"]
    assert text == "Here's what I have so far."
