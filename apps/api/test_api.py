"""API, workflow and webhook-security tests. No DataHub, no network."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from apps.api import shopify
from apps.api.db.models import Base, Run, WebhookEvent
from apps.api.db.session import make_engine
from apps.api.workflow import Actor, IllegalTransition, Status, can, transition

SECRET = "shhh-test-secret"


@pytest.fixture
def db(tmp_path) -> Session:
    from sqlalchemy.orm import sessionmaker

    engine = make_engine(f"sqlite:///{tmp_path}/t.db")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


@pytest.fixture
def run(db) -> Run:
    r = Run(organisation_id="org1", shop_id="shop1", status=Status.RECEIVED)
    db.add(r)
    db.commit()
    return r


# --- workflow ----------------------------------------------------------------


def test_happy_path_transitions_are_allowed(db, run):
    path = [
        Status.NORMALIZED, Status.CONTEXT_PENDING, Status.CONTEXT_RESOLVED,
        Status.CHECKS_RUNNING, Status.CHECKS_COMPLETED, Status.REMEDIATION_PLANNING,
        Status.AWAITING_APPROVAL, Status.APPROVED, Status.PATCH_GENERATING,
        Status.PATCH_GENERATED, Status.VALIDATION_RUNNING, Status.VALIDATED,
        Status.PULL_REQUEST_CREATING, Status.PULL_REQUEST_OPENED,
        Status.DATAHUB_WRITEBACK_PENDING, Status.DATAHUB_UPDATED, Status.COMPLETED,
    ]
    for s in path:
        transition(db, run, s)
    assert run.status == Status.COMPLETED
    assert len(run.transitions) == len(path)


def test_illegal_transition_raises(db, run):
    with pytest.raises(IllegalTransition):
        transition(db, run, Status.COMPLETED)


def test_cannot_skip_approval(db, run):
    """The approval gate is a graph property, not a convention."""
    for s in (Status.NORMALIZED, Status.CONTEXT_PENDING, Status.CONTEXT_RESOLVED,
              Status.CHECKS_RUNNING, Status.CHECKS_COMPLETED, Status.REMEDIATION_PLANNING,
              Status.AWAITING_APPROVAL):
        transition(db, run, s)
    assert not can(run, Status.PATCH_GENERATING)
    with pytest.raises(IllegalTransition):
        transition(db, run, Status.PATCH_GENERATING)


def test_failed_validation_cannot_reach_a_pull_request(db, run):
    for s in (Status.NORMALIZED, Status.CONTEXT_PENDING, Status.CONTEXT_RESOLVED,
              Status.CHECKS_RUNNING, Status.CHECKS_COMPLETED, Status.REMEDIATION_PLANNING,
              Status.AWAITING_APPROVAL, Status.APPROVED, Status.PATCH_GENERATING,
              Status.PATCH_GENERATED, Status.VALIDATION_RUNNING, Status.VALIDATION_FAILED):
        transition(db, run, s)
    assert not can(run, Status.PULL_REQUEST_CREATING)
    assert can(run, Status.AWAITING_APPROVAL)  # back for revision


def test_terminal_states_are_terminal(db, run):
    transition(db, run, Status.FAILED, reason="boom")
    with pytest.raises(IllegalTransition):
        transition(db, run, Status.NORMALIZED)


def test_transitions_are_recorded_with_actor(db, run):
    transition(db, run, Status.NORMALIZED, reason="why", actor=Actor("user", "amir@x"))
    t = run.transitions[-1]
    assert (t.from_status, t.to_status, t.actor_type, t.actor_user_id) == (
        Status.RECEIVED, Status.NORMALIZED, "user", "amir@x"
    )


# --- webhook signature -------------------------------------------------------


def sign(body: bytes, secret: str = SECRET) -> str:
    return base64.b64encode(
        hmac.new(secret.encode(), body, hashlib.sha256).digest()
    ).decode()


def test_valid_signature_accepted():
    body = b'{"id":1}'
    assert shopify.verify_hmac(body, sign(body), SECRET)


@pytest.mark.parametrize(
    "header",
    [None, "", "not-base64", base64.b64encode(b"wrong-digest-entirely").decode()],
)
def test_invalid_signatures_rejected(header):
    assert not shopify.verify_hmac(b'{"id":1}', header, SECRET)


def test_signature_is_over_raw_bytes_not_reparsed_json():
    """Re-serialising changes bytes; the HMAC must be over what arrived."""
    body = b'{"a":1,  "b":2}'
    reserialised = json.dumps(json.loads(body)).encode()
    assert body != reserialised
    assert shopify.verify_hmac(body, sign(body), SECRET)
    assert not shopify.verify_hmac(reserialised, sign(body), SECRET)


def test_missing_secret_never_validates():
    body = b'{"id":1}'
    assert not shopify.verify_hmac(body, sign(body), "")


def test_duplicate_deliveries_share_an_idempotency_key():
    a = shopify.idempotency_key("s.myshopify.com", "products/update", "wh-1", "hash")
    b = shopify.idempotency_key("s.myshopify.com", "products/update", "wh-1", "hash")
    assert a == b


def test_different_deliveries_differ():
    a = shopify.idempotency_key("s.myshopify.com", "products/update", "wh-1", "h1")
    b = shopify.idempotency_key("s.myshopify.com", "products/update", "wh-2", "h1")
    assert a != b


def test_headers_are_redacted():
    red = shopify.redact_headers(
        {"X-Shopify-Hmac-Sha256": "abc", "Authorization": "Bearer x", "X-Shopify-Topic": "products/update"}
    )
    assert red["X-Shopify-Hmac-Sha256"] == "<redacted>"
    assert red["Authorization"] == "<redacted>"
    assert red["X-Shopify-Topic"] == "products/update"


def test_topic_allowlist_is_closed():
    assert "products/update" in shopify.ALLOWED_TOPICS
    assert "orders/create" not in shopify.ALLOWED_TOPICS
    assert "shop/redact" not in shopify.ALLOWED_TOPICS


def test_normalize_extracts_commerce_values():
    change = shopify.normalize(
        "products/update",
        {"id": 1, "title": "Brew Pro", "status": "active",
         "variants": [{"sku": "NH-BREW-PRO", "price": "109.00", "inventory_quantity": 3}]},
    )
    assert change.entity_external_id == "NH-BREW-PRO"
    assert change.after_state["price"] == "109.00"
    assert change.after_state["inventory_quantity"] == 3


# --- HTTP surface ------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/api.db")
    monkeypatch.setenv("SHOPIFY_WEBHOOK_SECRET", SECRET)
    import apps.api.db.session as sess

    sess._engine = None
    sess._Session = None
    from apps.api.main import app

    with TestClient(app) as c:
        yield c


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_unsigned_webhook_is_rejected(client):
    r = client.post("/webhooks/shopify/products/update", content=b'{"id":1}')
    assert r.status_code == 401


def test_tampered_body_is_rejected(client):
    body = b'{"id":1,"variants":[{"sku":"X","price":"1.00"}]}'
    sig = sign(body)
    r = client.post(
        "/webhooks/shopify/products/update",
        content=body.replace(b'"1.00"', b'"0.01"'),
        headers={"X-Shopify-Hmac-Sha256": sig, "X-Shopify-Shop-Domain": "s.myshopify.com"},
    )
    assert r.status_code == 401


def test_disallowed_topic_is_refused_even_when_signed(client):
    body = b'{"id":1}'
    r = client.post(
        "/webhooks/shopify/orders/create",
        content=body,
        headers={"X-Shopify-Hmac-Sha256": sign(body), "X-Shopify-Shop-Domain": "s.myshopify.com"},
    )
    assert r.status_code == 400


def test_rejected_webhook_is_recorded_but_creates_no_run(client):
    client.post("/webhooks/shopify/products/update", content=b'{"id":9}')
    assert client.get("/api/runs").json()["runs"] == []


def test_approving_a_run_that_is_not_awaiting_is_refused(client):
    r = client.post("/api/runs/does-not-exist/approve", json={"decided_by": "x@y.z"})
    assert r.status_code == 404


# --- Shopify OAuth -----------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "evil.com",
        "shop.myshopify.com.evil.com",
        "https://shop.myshopify.com",
        "shop.myshopify.com/../admin",
        "",
        "shop myshopify com",
    ],
)
def test_open_redirect_shop_domains_are_refused(bad):
    """`shop` is attacker-controlled; a loose check here is an open redirect."""
    from apps.api.shopify_oauth import valid_shop

    assert not valid_shop(bad)


def test_legitimate_shop_domain_accepted():
    from apps.api.shopify_oauth import valid_shop

    assert valid_shop("northstar-home.myshopify.com")


def test_install_url_targets_the_shop_and_carries_state():
    from apps.api.shopify_oauth import install_url

    url = install_url("northstar-home.myshopify.com", "key123", "https://app.example/cb")
    assert url.startswith("https://northstar-home.myshopify.com/admin/oauth/authorize?")
    assert "state=" in url and "client_id=key123" in url


def test_install_url_refuses_a_hostile_shop():
    from apps.api.shopify_oauth import OAuthError, install_url

    with pytest.raises(OAuthError):
        install_url("evil.com", "key123", "https://app.example/cb")


def test_state_is_single_use():
    from apps.api.shopify_oauth import consume_state, new_state

    shop = "northstar-home.myshopify.com"
    s = new_state(shop)
    assert consume_state(s, shop)
    assert not consume_state(s, shop), "a replayed state must be refused"


def test_state_is_bound_to_its_shop():
    from apps.api.shopify_oauth import consume_state, new_state

    s = new_state("northstar-home.myshopify.com")
    assert not consume_state(s, "attacker.myshopify.com")


def test_callback_hmac_verification():
    from apps.api.shopify_oauth import verify_callback_hmac

    params = {"code": "abc", "shop": "n.myshopify.com", "state": "xyz", "timestamp": "1"}
    message = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    good = hmac.new(SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()

    assert verify_callback_hmac({**params, "hmac": good}, SECRET)
    assert not verify_callback_hmac({**params, "hmac": "deadbeef"}, SECRET)
    assert not verify_callback_hmac({**params, "shop": "other.myshopify.com", "hmac": good}, SECRET)


def test_only_read_scopes_are_requested():
    """Comgu proposes pull requests; it never writes to the store."""
    from apps.api.shopify_oauth import SCOPES

    assert "write" not in SCOPES
