"""Part 3: PII detection, the encrypted vault, GDPR webhooks, retention and log redaction."""

import asyncio
import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select, update

from app.domain import Channel, InboundMessage, MessageKind
from app.logging_config import redact_pii
from app.privacy.pii import PIIDetector, mask
from app.privacy.vault import PIIToken, PIIVault
from app.storage.db import Database
from app.storage.models import Conversation, Message
from tests import payloads as p
from tests.conftest import sent_bodies, signed


@pytest.mark.parametrize("text,expected", [
    ("mail ASHA@example.com", "mail [EMAIL]"),
    ("call +91 98765 43210", "call [PHONE]"),
    ("card 4111 1111 1111 1111", "card [CARD]"),
    ("card 4111 1111 1111 1112", "card 4111 1111 1111 1112"),  # fails Luhn: not a card
    ("aadhaar 2345 6789 0123", "aadhaar [GOV_ID]"),
    ("PAN ABCDE1234F", "PAN [GOV_ID]"),
    ("upi asha@okicici", "upi [UPI]"),
    ("my otp is 482913", "my otp is [OTP]"),
    ("order #1042 to pin 411001, tracking DL123456789IN", "order #1042 to pin 411001, tracking DL123456789IN"),
])
def test_detector(text, expected):
    assert mask(text) == expected


def test_log_processor_masks_pii():
    event = redact_pii(None, None, {"event": "x", "body": "reach me at a@b.co", "nested": {"t": "+91 9876543210"}})
    assert event["body"] == "reach me at [EMAIL]" and event["nested"]["t"] == "[PHONE]"


@pytest.fixture
async def vault(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/v.db")
    await db.create_all()
    async with db.session() as s:
        s.add(Conversation(id="c1", key="k1", tenant_id="t", channel="messenger", kind="direct", user_id="u"))
        s.add(Conversation(id="c2", key="k2", tenant_id="t", channel="messenger", kind="direct", user_id="u2"))
    return PIIVault(db, [Fernet.generate_key().decode()], "blind-key", PIIDetector())


async def test_pseudonymize_rehydrate_roundtrip(vault):
    r = await vault.pseudonymize("c1", "I'm asha@example.com, phone +91 98000 00001. Again: ASHA@example.com")
    assert r.text == "I'm <EMAIL_1>, phone <PHONE_1>. Again: <EMAIL_1>"  # same value -> same token
    assert await vault.rehydrate("c1", "Sent to <EMAIL_1>") == "Sent to asha@example.com"
    assert await vault.rehydrate("c2", "Sent to <EMAIL_1>") == "Sent to [EMAIL]"  # tokens are per conversation
    async with vault.db.session() as s:
        row = (await s.execute(select(PIIToken))).scalars().first()
    assert "asha" not in row.ciphertext  # encrypted at rest


async def test_irreversible_kinds_are_never_stored(vault):
    r = await vault.pseudonymize("c1", "my card is 4111 1111 1111 1111")
    assert r.text == "my card is [card number removed]" and r.blocked == {"CARD"}
    async with vault.db.session() as s:
        assert (await s.execute(select(PIIToken))).first() is None


async def test_blind_index_lookup_and_key_rotation(vault):
    await vault.pseudonymize("c2", "email: Asha@Example.com")
    assert await vault.conversations_for("EMAIL", "asha@example.com") == {"c2"}
    old = vault.fernet
    from cryptography.fernet import MultiFernet
    new_key = Fernet(Fernet.generate_key())
    vault.fernet = MultiFernet([new_key, *old._fernets])
    assert await vault.rotate() == 1
    vault.fernet = MultiFernet([new_key])  # old key retired
    assert await vault.rehydrate("c2", "<EMAIL_1>") == "Asha@Example.com"


# ── End to end ───────────────────────────────────────────────────────────────

async def _settle():
    for _ in range(15):
        await asyncio.sleep(0)


async def test_llm_and_db_never_see_raw_pii_but_customer_does(app, client, graph_mock, fake_llm):
    body, headers = signed(p.messenger_text("hi, I'm asha@example.com", mid="m_pii"))
    await client.post("/webhooks/meta", content=body, headers=headers)
    await _settle()
    assert "asha@example.com" not in json.dumps([m.content for c in fake_llm.calls for m in c["messages"]])
    async with app.state.db.session() as s:
        texts = [m.text for m in (await s.execute(select(Message))).scalars()]
    assert not any("asha@example.com" in t for t in texts) and any("<EMAIL_1>" in t for t in texts)
    delivered = [b for _, b in sent_bodies(graph_mock) if "message" in b][0]["message"]["text"]
    assert "asha@example.com" in delivered  # FakeLLM echoed <EMAIL_1>; rehydrated on the way out


async def test_card_number_gets_warning(client, graph_mock):
    body, headers = signed(p.messenger_text("my card is 4111 1111 1111 1111", mid="m_card"))
    await client.post("/webhooks/meta", content=body, headers=headers)
    await _settle()
    delivered = [b for _, b in sent_bodies(graph_mock) if "message" in b][0]["message"]["text"]
    assert delivered.startswith("For your security") and "4111" not in delivered


async def test_ig_comment_with_phone_is_hidden_and_moved_to_dm(client, graph_mock):
    body, headers = signed(p.instagram_comment("call me on 9876543210 about my order", comment_id="17900000000000077"))
    await client.post("/webhooks/meta", content=body, headers=headers)
    await _settle()
    paths = [path for path, _ in sent_bodies(graph_mock)]
    assert any(path.endswith("/17900000000000077") for path in paths)  # hide
    assert not any(path.endswith("/replies") for path in paths)  # no public reply
    assert any(b.get("recipient", {}).get("comment_id") for _, b in sent_bodies(graph_mock))  # private reply


async def test_abusive_comment_is_hidden_without_reply(client, graph_mock):
    body, headers = signed(p.instagram_comment("this shop is shit", comment_id="17900000000000088"))
    await client.post("/webhooks/meta", content=body, headers=headers)
    await _settle()
    calls = sent_bodies(graph_mock)
    assert len(calls) == 1 and "hide=true" in str(graph_mock.calls[0].request.url)


async def test_shopify_customers_redact_webhook(app, client):
    body, headers = signed(p.messenger_text("I'm asha@example.com", mid="m_gdpr"))
    await client.post("/webhooks/meta", content=body, headers=headers)
    await _settle()
    raw = json.dumps({"shop_domain": "brewline.myshopify.com", "customer": {"id": 1, "email": "ASHA@example.com"}}).encode()
    sig = base64.b64encode(hmac.new(b"shopify-secret", raw, hashlib.sha256).digest()).decode()
    bad = await client.post("/webhooks/shopify/compliance", content=raw,
                            headers={"x-shopify-hmac-sha256": "nope", "x-shopify-topic": "customers/redact"})
    assert bad.status_code == 401
    ok = await client.post("/webhooks/shopify/compliance", content=raw,
                           headers={"x-shopify-hmac-sha256": sig, "x-shopify-topic": "customers/redact"})
    assert ok.json() == {"erased_conversations": 1}
    async with app.state.db.session() as s:
        assert (await s.execute(select(Conversation))).first() is None


async def test_forget_me_flow_deletes_conversation(app, client, graph_mock):
    for i, text in enumerate(["I'm asha@example.com", "please delete my data", "yes"]):
        body, headers = signed(p.messenger_text(text, mid=f"m_f{i}"))
        await client.post("/webhooks/meta", content=body, headers=headers)
        await _settle()
    texts = [b["message"]["text"] for _, b in sent_bodies(graph_mock) if "message" in b]
    assert "Should I go ahead?" in texts[1] and texts[2].startswith("Done. I've deleted")
    async with app.state.db.session() as s:
        assert (await s.execute(select(Conversation))).first() is None


async def test_retention_deletes_old_messages(app, client):
    body, headers = signed(p.messenger_text("hello there", mid="m_old"))
    await client.post("/webhooks/meta", content=body, headers=headers)
    await _settle()
    old = datetime.now(timezone.utc) - timedelta(days=100)
    async with app.state.db.session() as s:
        await s.execute(update(Message).values(created_at=old))
    stats = await app.state.privacy.apply_retention(90, 30)
    assert stats["messages_deleted"] == 2


def test_inbound_message_fields_for_standby():
    m = InboundMessage(channel=Channel.MESSENGER, kind=MessageKind.DIRECT, message_id="x", user_id="u",
                       business_id="b", text="hi", standby=True)
    assert m.standby
