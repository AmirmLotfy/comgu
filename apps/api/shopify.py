"""Shopify webhook verification and normalization.

The verification here is the security boundary for the whole ingest path, so it
is deliberately boring:

  * HMAC-SHA256 over the **raw** body — not a re-serialised parse, which would
    change bytes and fail for reasons that look like an attack
  * `hmac.compare_digest`, never `==`
  * a rejected signature is recorded and dropped; it never reaches a run
  * duplicate deliveries resolve to the existing event rather than a second run
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass
from typing import Any

# Topics Comgu accepts. Anything else is refused rather than queued.
ALLOWED_TOPICS = {
    "products/update",
    "products/create",
    "inventory_levels/update",
    "app/uninstalled",
}

MAX_PAYLOAD_BYTES = 1_000_000

REDACT_HEADERS = {"x-shopify-hmac-sha256", "authorization", "cookie", "x-shopify-access-token"}


class WebhookRejected(ValueError):
    """The webhook failed verification and must not be processed."""


def verify_hmac(raw_body: bytes, header_hmac: str | None, secret: str) -> bool:
    """Constant-time comparison of Shopify's base64 HMAC-SHA256."""
    if not header_hmac or not secret:
        return False
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, header_hmac)


def payload_hash(raw_body: bytes) -> str:
    return hashlib.sha256(raw_body).hexdigest()


def idempotency_key(shop_domain: str, topic: str, webhook_id: str | None, body_hash: str) -> str:
    """Prefer Shopify's delivery id; fall back to a content hash.

    Shopify retries with the same X-Shopify-Webhook-Id, so keying on it collapses
    retries. The body hash covers the case where the header is absent.
    """
    return f"{shop_domain}:{topic}:{webhook_id or body_hash}"


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        k: ("<redacted>" if k.lower() in REDACT_HEADERS else v)
        for k, v in headers.items()
    }


@dataclass
class NormalizedChange:
    event_type: str
    entity_type: str
    entity_external_id: str
    after_state: dict[str, Any]
    before_state: dict[str, Any] | None = None


def normalize(topic: str, payload: dict[str, Any]) -> NormalizedChange:
    """Turn a Shopify payload into Comgu's platform-agnostic shape."""
    if topic.startswith("products/"):
        variant = (payload.get("variants") or [{}])[0]
        sku = variant.get("sku") or str(payload.get("id", ""))
        return NormalizedChange(
            event_type="product_price_changed",
            entity_type="product",
            entity_external_id=sku,
            after_state={
                "sku": sku,
                "title": payload.get("title", ""),
                "price": str(variant.get("price", "0")),
                "currency": payload.get("currency", "USD"),
                "inventory_quantity": int(variant.get("inventory_quantity") or 0),
                "status": payload.get("status", "active"),
            },
        )

    if topic.startswith("inventory_levels/"):
        return NormalizedChange(
            event_type="inventory_changed",
            entity_type="inventory_level",
            entity_external_id=str(payload.get("inventory_item_id", "")),
            after_state={
                "inventory_item_id": payload.get("inventory_item_id"),
                "location_id": payload.get("location_id"),
                "available": int(payload.get("available") or 0),
            },
        )

    return NormalizedChange(
        event_type="manual_scan",
        entity_type=topic,
        entity_external_id=str(payload.get("id", "")),
        after_state=payload,
    )


def webhook_secret() -> str:
    return os.environ.get("SHOPIFY_WEBHOOK_SECRET", "")
