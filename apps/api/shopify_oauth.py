"""Shopify OAuth install flow.

Authorization-code grant. The pieces that actually matter for safety:

  * the shop domain is validated against Shopify's hostname rules before it is
    ever put in a URL — this parameter is attacker-controlled and is the usual
    way these flows get turned into an open redirect
  * `state` is random, single-use and expiring, and is compared in constant time
  * the callback HMAC is verified over the sorted query string
  * the access token is never logged, returned, or written to the run record

Registering webhooks after install is what makes the store actually feed Comgu.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
import urllib.parse
from dataclasses import dataclass

import httpx

# Shopify shop domains: <store>.myshopify.com, and nothing else.
SHOP_DOMAIN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,60}\.myshopify\.com$")

# Read-only. Comgu proposes changes as pull requests; it does not write to the
# store, so no write scope is requested.
SCOPES = "read_products,read_inventory,read_locations"

STATE_TTL_SECONDS = 600

API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2026-01")

WEBHOOK_TOPICS = ["products/update", "inventory_levels/update", "app/uninstalled"]


class OAuthError(ValueError):
    """The install or callback could not be trusted."""


def valid_shop(shop: str) -> bool:
    return bool(shop) and bool(SHOP_DOMAIN.match(shop))


def require_shop(shop: str) -> str:
    if not valid_shop(shop):
        raise OAuthError(f"{shop!r} is not a valid myshopify.com domain")
    return shop


@dataclass
class PendingState:
    shop: str
    created_at: float

    @property
    def expired(self) -> bool:
        return (time.time() - self.created_at) > STATE_TTL_SECONDS


# Process-local store. A multi-instance deployment should move this to the
# database or a shared cache — noted rather than pretended otherwise.
_STATES: dict[str, PendingState] = {}


def new_state(shop: str) -> str:
    state = secrets.token_urlsafe(32)
    _STATES[state] = PendingState(shop=shop, created_at=time.time())
    _expire()
    return state


def _expire() -> None:
    for k, v in list(_STATES.items()):
        if v.expired:
            _STATES.pop(k, None)


def consume_state(state: str, shop: str) -> bool:
    """Single-use: a replayed state is rejected because it is already gone."""
    _expire()
    pending = _STATES.pop(state, None)
    if pending is None or pending.expired:
        return False
    return hmac.compare_digest(pending.shop, shop)


def install_url(shop: str, api_key: str, redirect_uri: str) -> str:
    require_shop(shop)
    state = new_state(shop)
    query = urllib.parse.urlencode(
        {
            "client_id": api_key,
            "scope": SCOPES,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"https://{shop}/admin/oauth/authorize?{query}"


def verify_callback_hmac(params: dict[str, str], secret: str) -> bool:
    """HMAC-SHA256 over the sorted query string, excluding `hmac` itself."""
    received = params.get("hmac", "")
    if not received or not secret:
        return False
    message = "&".join(
        f"{k}={v}" for k, v in sorted(params.items()) if k not in ("hmac", "signature")
    )
    expected = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)


async def exchange_code(shop: str, code: str, api_key: str, api_secret: str) -> str:
    """Swap the authorization code for an access token."""
    require_shop(shop)
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            f"https://{shop}/admin/oauth/access_token",
            json={"client_id": api_key, "client_secret": api_secret, "code": code},
        )
    if r.status_code != 200:
        # Deliberately does not echo the body — it can contain the code.
        raise OAuthError(f"token exchange failed with HTTP {r.status_code}")
    token = r.json().get("access_token")
    if not token:
        raise OAuthError("token exchange returned no access_token")
    return token


async def register_webhooks(shop: str, token: str, callback_base: str) -> list[dict]:
    """Subscribe to the topics Comgu acts on. Idempotent — Shopify dedupes."""
    require_shop(shop)
    results = []
    async with httpx.AsyncClient(timeout=20) as client:
        for topic in WEBHOOK_TOPICS:
            r = await client.post(
                f"https://{shop}/admin/api/{API_VERSION}/webhooks.json",
                headers={"X-Shopify-Access-Token": token},
                json={
                    "webhook": {
                        "topic": topic,
                        "address": f"{callback_base.rstrip('/')}/webhooks/shopify/{topic}",
                        "format": "json",
                    }
                },
            )
            results.append(
                {
                    "topic": topic,
                    "status": r.status_code,
                    "ok": r.status_code in (200, 201, 422),  # 422 = already exists
                }
            )
    return results
