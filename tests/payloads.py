"""Realistic Meta webhook payloads (trimmed to the fields we use).

Shapes follow Meta's webhook reference for WhatsApp Cloud API, Messenger Platform and
the Instagram API (messaging + comments).
"""

WA_PHONE_ID = "109876543210"
PAGE_ID = "222333444"
IG_ID = "17841400000000000"


def whatsapp_text(text: str = "Hi! Do you ship to Pune?", wamid: str = "wamid.HBgM001", wa_id: str = "919800000001"):
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "WABA_ID",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"display_phone_number": "15550001111", "phone_number_id": WA_PHONE_ID},
                    "contacts": [{"profile": {"name": "Asha"}, "wa_id": wa_id}],
                    "messages": [{"from": wa_id, "id": wamid, "timestamp": "1762990000", "type": "text",
                                  "text": {"body": text}}],
                },
            }],
        }],
    }


def whatsapp_status():
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA_ID", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp", "metadata": {"phone_number_id": WA_PHONE_ID},
            "statuses": [{"id": "wamid.X", "status": "delivered", "timestamp": "1762990001", "recipient_id": "9198"}],
        }}]}],
    }


def messenger_text(text: str = "Is the Ethiopia Yirgacheffe in stock?", mid: str = "m_001", psid: str = "PSID_1"):
    return {
        "object": "page",
        "entry": [{"id": PAGE_ID, "time": 1762990000000, "messaging": [{
            "sender": {"id": psid}, "recipient": {"id": PAGE_ID}, "timestamp": 1762990000000,
            "message": {"mid": mid, "text": text},
        }]}],
    }


def messenger_echo():
    return {
        "object": "page",
        "entry": [{"id": PAGE_ID, "time": 1762990000000, "messaging": [{
            "sender": {"id": PAGE_ID}, "recipient": {"id": "PSID_1"}, "timestamp": 1762990000000,
            "message": {"mid": "m_echo", "text": "hello from us", "is_echo": True},
        }]}],
    }


def instagram_dm(text: str = "hey, where's my order?", mid: str = "ig_m_001", igsid: str = "IGSID_1"):
    return {
        "object": "instagram",
        "entry": [{"id": IG_ID, "time": 1762990000000, "messaging": [{
            "sender": {"id": igsid}, "recipient": {"id": IG_ID}, "timestamp": 1762990000000,
            "message": {"mid": mid, "text": text},
        }]}],
    }


def instagram_comment(text: str = "Love this roast! 😍", comment_id: str = "17900000000000001",
                      user_id: str = "IGUSER_9", username: str = "coffee.lover"):
    return {
        "object": "instagram",
        "entry": [{"id": IG_ID, "time": 1762990000, "changes": [{
            "field": "comments",
            "value": {"from": {"id": user_id, "username": username},
                      "media": {"id": "MEDIA_1", "media_product_type": "FEED"},
                      "id": comment_id, "text": text},
        }]}],
    }
