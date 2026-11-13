"""Webhook authenticity checks.

Meta signs every webhook POST with HMAC-SHA256 of the *raw* request body, keyed with
your App Secret, and sends it as `X-Hub-Signature-256: sha256=<hex>`.

Two rules people get wrong:
  1. Verify against the raw bytes, before any JSON parsing/re-serialisation.
  2. Use a constant-time comparison (hmac.compare_digest) to avoid timing attacks.

If you use "Instagram API with Instagram Login", Instagram webhooks are signed with the
*Instagram* app secret, so we accept a list of secrets.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable


def compute_signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_meta_signature(body: bytes, header_value: str | None, secrets: Iterable[str]) -> bool:
    """Return True if `header_value` is a valid signature of `body` for any secret."""
    if not header_value or not header_value.startswith("sha256="):
        return False
    for secret in secrets:
        if secret and hmac.compare_digest(compute_signature(secret, body), header_value):
            return True
    return False
