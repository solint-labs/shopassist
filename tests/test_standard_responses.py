"""Standard customer journeys, end to end through the real webhook, verifying the message the
customer actually receives (body, formatting, links, buttons, limits) on each channel."""

import asyncio

import pytest

from app.escalation import handoff as handoff_module
from app.llm.base import LLMResponse
from tests import payloads as p
from tests.conftest import sample_order, sent_bodies, signed


async def _send(client, payload):
    body, headers = signed(payload)
    r = await client.post("/webhooks/meta", content=body, headers=headers)
    assert r.status_code == 200
    for _ in range(15):
        await asyncio.sleep(0)


def _last_text(graph_mock) -> str:
    """Text of the last customer-facing message, whatever the channel."""
    for _, b in reversed(sent_bodies(graph_mock)):
        if b.get("type") == "text":
            return b["text"]["body"]
        if b.get("type") == "interactive":
            return b["interactive"]["body"]["text"]
        if "message" in b and isinstance(b["message"], dict):
            return b["message"].get("text", "")
    raise AssertionError("no message sent")


# ── FAQ answers: right article, policy link, per channel ─────────────────────

FAQ = [
    ("Is shipping free on orders above 999?", "Domestic shipping", "https://brewline.example/policies/shipping-policy"),
    ("Can I return an opened bag of coffee?", "Coffee beans", "https://brewline.example/policies/refund-policy"),
    ("How do I cancel my coffee subscription?", "Cancelling a subscription", "https://brewline.example/pages/subscriptions"),
    ("What grind size for a French press?", "grind size", "https://brewline.example/blogs/guides"),
    ("Do your gift cards expire?", "Gift cards", "https://brewline.example/pages/faq"),
]


@pytest.mark.parametrize("question,section,url", FAQ)
@pytest.mark.parametrize("channel", ["whatsapp", "messenger", "instagram"])
async def test_faq_reply_is_grounded_and_linked(client, graph_mock, fake_llm, question, section, url, channel):
    payload = {"whatsapp": p.whatsapp_text(question, wamid=f"wamid.{hash(question + channel)}"),
               "messenger": p.messenger_text(question, mid=f"m.{hash(question + channel)}"),
               "instagram": p.instagram_dm(question, mid=f"ig.{hash(question + channel)}")}[channel]
    await _send(client, payload)
    text = _last_text(graph_mock)
    assert section.lower() in fake_llm.calls[-1]["system"].lower()  # the right article reached the model
    assert url in text  # the customer gets the policy link
    assert "[1]" not in text and "<source" not in text  # citations and markup never leak


async def test_unknown_question_gets_no_sources_and_honest_prompt(client, graph_mock, fake_llm):
    await _send(client, p.messenger_text("Do you sell oat milk?", mid="m.oat"))
    system = fake_llm.calls[-1]["system"]
    assert "<sources>" not in system and "Do NOT guess" in system


# ── Formatting and limits per channel ────────────────────────────────────────

MARKDOWN = "## Steps\n**Grind coarse** and steep 4 min. See [the guide](https://brewline.example/blogs/guides).\n- stir\n- press"


async def test_whatsapp_formatting(client, graph_mock, fake_llm):
    fake_llm.script = [LLMResponse(text=MARKDOWN)]
    await _send(client, p.whatsapp_text("how do I brew french press?", wamid="wamid.fmt"))
    text = _last_text(graph_mock)
    assert "*Grind coarse*" in text and "##" not in text and "• stir" in text
    assert "the guide: https://brewline.example/blogs/guides" in text


async def test_instagram_dm_plain_text_and_length_limit(client, graph_mock, fake_llm):
    fake_llm.script = [LLMResponse(text="**Bold** start. " + "This sentence is long enough. " * 80)]
    await _send(client, p.instagram_dm("tell me everything about coffee", mid="ig.long"))
    text = _last_text(graph_mock)
    assert "**" not in text and len(text) <= 1000


async def test_comment_public_reply_is_short(client, graph_mock, fake_llm):
    fake_llm.script = [LLMResponse(text='{"public_reply": "' + "Thanks so much for the love! " * 30 +
                                        '", "send_dm": false, "moderation": "ok"}')]
    await _send(client, p.instagram_comment("this roast is amazing", comment_id="17900000000000099"))
    public = [b for path, b in sent_bodies(graph_mock) if path.endswith("/replies")][0]["message"]
    assert len(public) <= 300


# ── Orders ───────────────────────────────────────────────────────────────────

async def test_order_status_reply_includes_tracking(client, graph_mock, shopify_mock):
    shopify_mock.orders["1042"] = sample_order(delivered_days_ago=None)
    await _send(client, p.whatsapp_text("where is my order #1042?", wamid="wamid.track"))
    text = _last_text(graph_mock)
    assert "DL123456789IN" in text and "Delhivery" in text


async def test_order_status_wrong_customer_gets_nothing(client, graph_mock, shopify_mock):
    shopify_mock.orders["1042"] = sample_order(phone="+91 90000 11111")
    await _send(client, p.whatsapp_text("where is my order #1042?", wamid="wamid.other", wa_id="919811122233"))
    text = _last_text(graph_mock)
    assert "DL123456789IN" not in text and "Yirgacheffe" not in text


async def test_customer_sees_own_email_back(client, graph_mock, fake_llm):
    fake_llm.script = [LLMResponse(text="Got it, I'll use <EMAIL_1> to look up your order.")]
    await _send(client, p.messenger_text("my email is asha@example.com", mid="m.mail"))
    assert _last_text(graph_mock) == "Got it, I'll use asha@example.com to look up your order."


# ── Buttons ──────────────────────────────────────────────────────────────────

async def test_cancel_confirmation_renders_quick_replies_on_messenger(client, graph_mock, shopify_mock, fake_llm):
    from app.llm.base import ToolCall
    shopify_mock.orders["1042"] = sample_order(fulfillment="UNFULFILLED", delivered_days_ago=None)
    fake_llm.script = [
        LLMResponse(text="", tool_calls=[ToolCall("a", "get_order_status", {"order_number": "1042", "email": "<EMAIL_1>"})]),
        LLMResponse(text="", tool_calls=[ToolCall("b", "start_order_cancellation", {"order_number": "1042"})]),
    ]
    await _send(client, p.messenger_text("cancel order 1042, email asha@example.com", mid="m.qr"))
    msg = [b for _, b in sent_bodies(graph_mock) if "message" in b][-1]["message"]
    assert [q["payload"] for q in msg["quick_replies"]] == ["yes", "no"]
    assert all(len(q["title"]) <= 20 for q in msg["quick_replies"])


# ── Escalation messages: business hours ──────────────────────────────────────

@pytest.mark.parametrize("in_hours,expected", [(True, "few minutes"), (False, "offline right now")])
async def test_handoff_message_depends_on_business_hours(client, graph_mock, monkeypatch, in_hours, expected):
    monkeypatch.setattr(handoff_module, "within_business_hours", lambda *_a, **_k: in_hours)
    await _send(client, p.messenger_text("I want to talk to a human", mid=f"m.bh{in_hours}"))
    assert expected in _last_text(graph_mock)


async def test_fallback_when_llm_down_includes_support_email(client, graph_mock, fake_llm):
    def down(*_):
        raise RuntimeError("provider down")
    fake_llm.script = [down]
    await _send(client, p.whatsapp_text("hello?", wamid="wamid.down"))
    assert "help@brewline.example" in _last_text(graph_mock)
