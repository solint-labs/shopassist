"""Part 1: webhook -> agent -> Graph API, end to end with a fake LLM."""

import asyncio

from app.llm.base import LLMResponse
from tests import payloads as p
from tests.conftest import sent_bodies, signed


async def _settle():
    # Background tasks run after the response; give the event loop a tick.
    for _ in range(5):
        await asyncio.sleep(0)


async def test_webhook_verification(client):
    ok = await client.get("/webhooks/meta", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me",
                                                     "hub.challenge": "1158201444"})
    assert ok.status_code == 200 and ok.text == "1158201444"
    bad = await client.get("/webhooks/meta", params={"hub.mode": "subscribe", "hub.verify_token": "nope",
                                                      "hub.challenge": "x"})
    assert bad.status_code == 403


async def test_rejects_unsigned_payload(client):
    r = await client.post("/webhooks/meta", json=p.whatsapp_text())
    assert r.status_code == 401


async def test_whatsapp_roundtrip(client, graph_mock, fake_llm):
    body, headers = signed(p.whatsapp_text("Do you ship to Pune?"))
    r = await client.post("/webhooks/meta", content=body, headers=headers)
    assert r.status_code == 200 and r.json() == {"received": 1}
    await _settle()

    calls = sent_bodies(graph_mock)
    # 1) read receipt + typing indicator, 2) the actual reply
    assert calls[0][1]["status"] == "read" and calls[0][1]["typing_indicator"] == {"type": "text"}
    path, reply = calls[1]
    assert path.endswith(f"/{p.WA_PHONE_ID}/messages")
    assert reply["to"] == "919800000001" and reply["context"]["message_id"] == "wamid.HBgM001"
    assert reply["text"]["body"].strip()  # content depends on the stage (echo in Part 1, grounded later)

    system = fake_llm.calls[0]["system"]
    assert "You are Bree" in system and "WhatsApp" in system and "ONLY help with" in system


async def test_duplicate_webhook_is_processed_once(client, graph_mock):
    body, headers = signed(p.messenger_text(mid="m_dup"))
    await client.post("/webhooks/meta", content=body, headers=headers)
    await client.post("/webhooks/meta", content=body, headers=headers)
    await _settle()
    replies = [b for path, b in sent_bodies(graph_mock) if "message" in b]
    assert len(replies) == 1


async def test_history_is_sent_to_llm(client, fake_llm):
    for i, text in enumerate(["Hi there", "Do you sell grinders?"]):
        body, headers = signed(p.messenger_text(text, mid=f"m_{i}"))
        await client.post("/webhooks/meta", content=body, headers=headers)
        await _settle()
    msgs = fake_llm.calls[-1]["messages"]
    assert [m.role for m in msgs] == ["user", "assistant", "user"]
    assert msgs[-1].content == "Do you sell grinders?"


async def test_llm_failure_sends_fallback(client, graph_mock, fake_llm):
    def boom(*_):
        raise RuntimeError("provider down")
    fake_llm.script = [boom]
    body, headers = signed(p.instagram_dm(mid="ig_fail"))
    await client.post("/webhooks/meta", content=body, headers=headers)
    await _settle()
    texts = [b["message"]["text"] for _, b in sent_bodies(graph_mock) if "message" in b]
    assert texts and "help@brewline.example" in texts[0]


async def test_instagram_compliment_gets_public_reply_only(client, graph_mock, fake_llm):
    fake_llm.script = [LLMResponse(text='{"public_reply": "So glad you love it!", "send_dm": false, "dm_text": ""}')]
    body, headers = signed(p.instagram_comment("Love this roast!"))
    await client.post("/webhooks/meta", content=body, headers=headers)
    await _settle()
    calls = sent_bodies(graph_mock)
    assert len(calls) == 1
    assert calls[0][0].endswith("/17900000000000001/replies") and calls[0][1] == {"message": "So glad you love it!"}


async def test_instagram_order_comment_moves_to_dm(client, graph_mock, fake_llm):
    # Model "forgets" to move to DM; the keyword guard still does it.
    fake_llm.script = [LLMResponse(text='{"public_reply": "", "send_dm": false}')]
    body, headers = signed(p.instagram_comment("my order never arrived!!", comment_id="17900000000000002"))
    await client.post("/webhooks/meta", content=body, headers=headers)
    await _settle()
    calls = sent_bodies(graph_mock)
    public = [b for path, b in calls if path.endswith("/replies")]
    private = [b for path, b in calls if b.get("recipient", {}).get("comment_id")]
    assert public and "DM" in public[0]["message"]
    assert private and private[0]["recipient"] == {"comment_id": "17900000000000002"}
