"""Edge tests: signatures, payload parsing, formatting."""

from app.channels.formatting import format_for_channel, truncate
from app.channels.parser import parse_webhook
from app.channels.signature import compute_signature, verify_meta_signature
from app.domain import Channel, MessageKind
from tests import payloads as p


def test_signature_roundtrip():
    body = b'{"object":"page"}'
    sig = compute_signature("s3cret", body)
    assert verify_meta_signature(body, sig, ["s3cret"])
    assert verify_meta_signature(body, sig, ["other", "s3cret"])  # IG Login uses a second secret
    assert not verify_meta_signature(body + b" ", sig, ["s3cret"])
    assert not verify_meta_signature(body, None, ["s3cret"])
    assert not verify_meta_signature(body, "sha1=abc", ["s3cret"])


def test_parse_whatsapp_text():
    [m] = parse_webhook(p.whatsapp_text("Hello"))
    assert m.channel == Channel.WHATSAPP and m.kind == MessageKind.DIRECT
    assert m.text == "Hello" and m.user_name == "Asha" and m.business_id == p.WA_PHONE_ID
    assert m.conversation_key == f"whatsapp:{p.WA_PHONE_ID}:d:919800000001"


def test_parse_whatsapp_status_is_ignored():
    assert parse_webhook(p.whatsapp_status()) == []


def test_parse_messenger_and_skip_echo():
    [m] = parse_webhook(p.messenger_text())
    assert m.channel == Channel.MESSENGER and m.user_id == "PSID_1"
    assert parse_webhook(p.messenger_echo()) == []


def test_parse_instagram_dm_and_comment():
    [dm] = parse_webhook(p.instagram_dm())
    assert dm.channel == Channel.INSTAGRAM and dm.kind == MessageKind.DIRECT
    [c] = parse_webhook(p.instagram_comment())
    assert c.kind == MessageKind.COMMENT and c.user_name == "coffee.lover" and c.media_id == "MEDIA_1"
    assert ":c:" in c.conversation_key  # comment threads never share DM history


def test_own_comment_is_ignored():
    payload = p.instagram_comment(user_id=p.IG_ID)
    assert parse_webhook(payload) == []


def test_formatting_per_channel():
    md = "## Hi\n**Great** news - see [our policy](https://x.example/p)\n- one\n- two"
    wa = format_for_channel(md, Channel.WHATSAPP)
    assert "*Great*" in wa and "our policy: https://x.example/p" in wa and "• one" in wa and "##" not in wa
    ms = format_for_channel(md, Channel.MESSENGER)
    assert "**" not in ms and "*Great*" not in ms


def test_truncate_prefers_sentence_boundary():
    text = "First sentence is here. " * 20
    out = truncate(text, 100)
    assert len(out) <= 100 and out.endswith(".")
