"""Post a correctly signed sample webhook to a running ShopAssist instance.

    python -m scripts.send_test_webhook whatsapp "Do you ship to Pune?"
    python -m scripts.send_test_webhook ig-comment "my order is late"

Run the server with DRY_RUN_OUTBOUND=true to see the outbound Graph calls in the logs
without real Meta credentials.
"""

from __future__ import annotations

import argparse
import json
import uuid

import httpx

from app.channels.signature import compute_signature
from app.settings import get_settings
from tests import payloads as p

BUILDERS = {
    "whatsapp": lambda t: p.whatsapp_text(t, wamid=f"wamid.{uuid.uuid4().hex[:10]}"),
    "messenger": lambda t: p.messenger_text(t, mid=f"m_{uuid.uuid4().hex[:10]}"),
    "ig-dm": lambda t: p.instagram_dm(t, mid=f"ig_{uuid.uuid4().hex[:10]}"),
    "ig-comment": lambda t: p.instagram_comment(t, comment_id=str(uuid.uuid4().int)[:17]),
}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=BUILDERS)
    ap.add_argument("text")
    ap.add_argument("--url", default="http://localhost:8000/webhooks/meta")
    a = ap.parse_args()
    body = json.dumps(BUILDERS[a.kind](a.text)).encode()
    sig = compute_signature(get_settings().meta_app_secret.get_secret_value(), body)
    r = httpx.post(a.url, content=body, headers={"x-hub-signature-256": sig, "content-type": "application/json"})
    print(r.status_code, r.text)
