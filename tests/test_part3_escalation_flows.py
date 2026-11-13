"""Part 3: escalation scenarios, hand-off, the human-agent API, and order flows."""

import asyncio
from datetime import datetime, timezone

import pytest

from app.agent.tenant import BusinessHours, EscalationConfig
from app.escalation.detector import EscalationDetector, frustration_score, within_business_hours
from app.llm.base import LLMResponse, ToolCall
from tests import payloads as p
from tests.conftest import sample_order, sent_bodies, signed

AGENT = {"x-agent-key": "agent-key"}


# ── Detector scenarios ───────────────────────────────────────────────────────

@pytest.mark.parametrize("text,reason", [
    ("I think I had an allergic reaction to the decaf", "safety"),
    ("There was glass in the bag!", "safety"),
    ("I'll take this to consumer court", "legal"),
    ("this is fraud, I'm filing a chargeback", "legal"),
    ("can I talk to a real person please", "human_request"),
    ("connect me with someone from your team", "human_request"),
    ("agent", "human_request"),
    ("insaan se baat karni hai", "human_request"),
    ("THIS IS RIDICULOUS!!! STILL NO REFUND", "frustration"),
    ("worst service, totally useless", "frustration"),
])
def test_escalation_scenarios(text, reason):
    d = EscalationDetector(EscalationConfig(human_request_keywords=["insaan se baat"])).evaluate(text, [], {})
    assert d is not None and d.reason == reason


@pytest.mark.parametrize("text", [
    "am I talking to a bot?", "what's the V60 recipe?", "where's my order #1042", "thanks a lot!"])
def test_ordinary_messages_do_not_escalate(text):
    assert EscalationDetector(EscalationConfig()).evaluate(text, [], {}) is None


def test_repeated_failure_and_repetition():
    det = EscalationDetector(EscalationConfig(max_failed_answers=2))
    assert det.evaluate("do you have oat milk?", [], {"failed_answers": 2}).reason == "repeated_failure"
    assert frustration_score("when will my order arrive", repeated=True) > frustration_score("when will my order arrive")


def test_business_hours():
    bh = BusinessHours(timezone="Asia/Kolkata", days=[0, 1, 2, 3, 4, 5], start="09:00", end="19:00")
    assert within_business_hours(bh, datetime(2026, 11, 13, 6, 0, tzinfo=timezone.utc))  # Fri 11:30 IST
    assert not within_business_hours(bh, datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc))  # Fri 20:30 IST
    assert not within_business_hours(bh, datetime(2026, 11, 15, 6, 0, tzinfo=timezone.utc))  # Sunday


# ── Hand-off lifecycle ───────────────────────────────────────────────────────

async def _settle():
    for _ in range(15):
        await asyncio.sleep(0)


async def _send(client, payload):
    body, headers = signed(payload)
    await client.post("/webhooks/meta", content=body, headers=headers)
    await _settle()


async def test_handoff_lifecycle_on_messenger(client, graph_mock):
    await _send(client, p.messenger_text("I want to speak to a human", mid="h1"))
    calls = sent_bodies(graph_mock)
    assert any(path.endswith("/pass_thread_control") and b["target_app_id"] == "263902037430900" for path, b in calls)
    reply = [b for _, b in calls if "message" in b][-1]["message"]["text"]
    assert "teammate" in reply or "team" in reply

    # While a human owns it, the bot stays silent (no reply, no typing bubble).
    n = len(sent_bodies(graph_mock))
    await _send(client, p.messenger_text("hello? my email is asha@example.com", mid="h2"))
    assert len(sent_bodies(graph_mock)) == n

    queue = (await client.get("/agent/conversations", headers=AGENT)).json()
    assert len(queue) == 1 and queue[0]["status"] == "human"
    conv_id = queue[0]["id"]
    convo = (await client.get(f"/agent/conversations/{conv_id}", headers=AGENT)).json()
    assert "asha@example.com" in convo["messages"][-1]["text"]  # the human agent sees real details

    r = await client.post(f"/agent/conversations/{conv_id}/messages", headers=AGENT,
                          json={"text": "Hi Asha, Priya here. Looking into it now!", "agent_name": "Priya"})
    assert r.json() == {"sent": True}
    assert (await client.get("/agent/conversations", headers={"x-agent-key": "wrong"})).status_code == 401

    await client.post(f"/agent/conversations/{conv_id}/release", headers=AGENT)
    assert any(path.endswith("/take_thread_control") for path, _ in sent_bodies(graph_mock))
    n = len(sent_bodies(graph_mock))
    await _send(client, p.messenger_text("thanks! one more question about grind size", mid="h3"))
    assert len(sent_bodies(graph_mock)) > n  # the bot is back


async def test_thread_control_returned_by_inbox_releases_conversation(client, graph_mock):
    await _send(client, p.messenger_text("talk to a person", mid="t1"))
    handover = {"object": "page", "entry": [{"id": p.PAGE_ID, "time": 1, "messaging": [{
        "sender": {"id": "PSID_1"}, "recipient": {"id": p.PAGE_ID}, "timestamp": 1,
        "pass_thread_control": {"previous_owner_app_id": "263902037430900", "new_owner_app_id": "APP_123"}}]}]}
    await _send(client, handover)
    assert (await client.get("/agent/conversations", headers=AGENT)).json() == []


async def test_safety_escalation_on_whatsapp_has_care_message(client, graph_mock):
    await _send(client, p.whatsapp_text("my son had an allergic reaction after drinking it"))
    reply = [b for _, b in sent_bodies(graph_mock) if b.get("type") == "text"][-1]["text"]["body"]
    assert reply.startswith("I'm really sorry") and "medical" in reply


async def test_model_can_escalate_via_tool(client, graph_mock, fake_llm):
    fake_llm.script = [LLMResponse(text="", tool_calls=[ToolCall("e1", "escalate_to_human",
                                                                 {"reason": "compensation_request", "urgency": "high"})])]
    await _send(client, p.instagram_dm("I want compensation for the delay", mid="e_ig"))
    assert (await client.get("/agent/conversations", headers=AGENT)).json()[0]["channel"] == "instagram"


# ── Flows ───────────────────────────────────────────────────────────────────

def _verify_then(flow_tool: str, number="1042", email: str | None = None):
    """LLM script: verify the order, then start a flow (like a real model would)."""
    args = {"order_number": number, **({"email": email} if email else {})}
    return [
        LLMResponse(text="", tool_calls=[ToolCall("c1", "get_order_status", args)]),
        LLMResponse(text="", tool_calls=[ToolCall("c2", flow_tool, {"order_number": number})]),
    ]


async def test_cancel_flow_with_confirmation_buttons(client, graph_mock, shopify_mock, fake_llm):
    shopify_mock.orders["1042"] = sample_order(fulfillment="UNFULFILLED", delivered_days_ago=None)
    fake_llm.script = _verify_then("start_order_cancellation")
    await _send(client, p.whatsapp_text("cancel my order 1042 please", wamid="wamid.c1"))
    confirm = [b for _, b in sent_bodies(graph_mock) if b.get("type") == "interactive"][-1]
    assert "cancel order #1042" in confirm["interactive"]["body"]["text"]
    assert [b["reply"]["id"] for b in confirm["interactive"]["action"]["buttons"]] == ["yes", "no"]
    assert shopify_mock.mutations == []  # nothing happens before confirmation

    button_tap = p.whatsapp_text("Yes, cancel it", wamid="wamid.c2")
    m = button_tap["entry"][0]["changes"][0]["value"]["messages"][0]
    m.update({"type": "interactive", "interactive": {"type": "button_reply",
                                                     "button_reply": {"id": "yes", "title": "Yes, cancel it"}}})
    await _send(client, button_tap)
    assert shopify_mock.mutations[0][0] == "cancel"
    done = [b for _, b in sent_bodies(graph_mock) if b.get("type") == "text"][-1]["text"]["body"]
    assert "is cancelled" in done


async def test_cancel_after_shipping_is_refused_with_policy(client, graph_mock, shopify_mock, fake_llm):
    shopify_mock.orders["1042"] = sample_order(fulfillment="FULFILLED")
    fake_llm.script = _verify_then("start_order_cancellation")
    await _send(client, p.whatsapp_text("cancel 1042", wamid="wamid.c3"))
    reply = [b for _, b in sent_bodies(graph_mock) if b.get("type") == "text"][-1]["text"]["body"]
    assert "can't be cancelled" in reply and "14 days" in reply and not shopify_mock.mutations


async def test_high_value_cancellation_goes_to_human(client, shopify_mock, fake_llm):
    shopify_mock.orders["1042"] = sample_order(fulfillment="UNFULFILLED", delivered_days_ago=None, total="9800.00")
    fake_llm.script = _verify_then("start_order_cancellation")
    await _send(client, p.whatsapp_text("cancel 1042", wamid="wamid.c4"))
    assert not shopify_mock.mutations
    assert (await client.get("/agent/conversations", headers=AGENT)).json()[0]["channel"] == "whatsapp"


async def test_unverified_order_cannot_start_flow(client, graph_mock, shopify_mock, fake_llm):
    shopify_mock.orders["1042"] = sample_order(fulfillment="UNFULFILLED", phone="+91 99999 99999")
    fake_llm.script = _verify_then("start_order_cancellation") + [LLMResponse(text="Could you share your email?")]
    await _send(client, p.whatsapp_text("cancel 1042", wamid="wamid.c5"))
    reply = [b for _, b in sent_bodies(graph_mock) if b.get("type") == "text"][-1]["text"]["body"]
    assert reply == "Could you share your email?" and not shopify_mock.mutations


async def test_return_flow_unopened_coffee(client, graph_mock, shopify_mock, fake_llm):
    shopify_mock.orders["1042"] = sample_order(delivered_days_ago=5)
    # The model only ever saw "<EMAIL_1>"; the vault swaps the real email in before Shopify is called.
    fake_llm.script = _verify_then("start_return_request", email="<EMAIL_1>")
    await _send(client, p.messenger_text("I want to return order 1042, email asha@example.com", mid="r1"))
    texts = [b["message"]["text"] for _, b in sent_bodies(graph_mock) if "message" in b]
    assert "reason for the return" in texts[-1]
    await _send(client, p.messenger_text("it's still sealed, unopened", mid="r2"))
    assert "within the 14-day window" in [b["message"]["text"] for _, b in sent_bodies(graph_mock) if "message" in b][-1]
    await _send(client, p.messenger_text("yes", mid="r3"))
    kind, variables = shopify_mock.mutations[0]
    assert kind == "return" and variables["input"]["returnLineItems"][0]["fulfillmentLineItemId"].endswith("/1")


async def test_return_flow_opened_coffee_is_refused(client, graph_mock, shopify_mock, fake_llm):
    shopify_mock.orders["1042"] = sample_order(delivered_days_ago=2)
    fake_llm.script = _verify_then("start_return_request")
    await _send(client, p.whatsapp_text("return 1042", wamid="wamid.r4"))
    await _send(client, p.whatsapp_text("I opened it but didn't like the taste", wamid="wamid.r5"))
    reply = [b for _, b in sent_bodies(graph_mock) if b.get("type") == "text"][-1]["text"]["body"]
    assert "can't be returned" in reply and "Taste Promise" in reply and not shopify_mock.mutations


async def test_return_flow_damaged_item_escalates_with_photo_request(client, graph_mock, shopify_mock, fake_llm):
    shopify_mock.orders["1042"] = sample_order(delivered_days_ago=1, items=(("Hario V60 Dripper", 1),))
    fake_llm.script = _verify_then("start_return_request")
    await _send(client, p.whatsapp_text("return 1042", wamid="wamid.r6"))
    await _send(client, p.whatsapp_text("it arrived broken", wamid="wamid.r7"))
    reply = [b for _, b in sent_bodies(graph_mock) if b.get("type") == "text"][-1]["text"]["body"]
    assert "send a photo" in reply
    assert (await client.get("/agent/conversations", headers=AGENT)).json()


async def test_customer_can_leave_a_flow(client, graph_mock, shopify_mock, fake_llm):
    shopify_mock.orders["1042"] = sample_order(fulfillment="UNFULFILLED", delivered_days_ago=None)
    fake_llm.script = _verify_then("start_order_cancellation")
    await _send(client, p.whatsapp_text("cancel 1042", wamid="wamid.x1"))
    await _send(client, p.whatsapp_text("never mind", wamid="wamid.x2"))
    reply = [b for _, b in sent_bodies(graph_mock) if b.get("type") == "text"][-1]["text"]["body"]
    assert "stopped" in reply and not shopify_mock.mutations
