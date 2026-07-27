"""Authentication and the role model (PRD 12.2, 16.1).

Comgu has no identity provider. Access is a signed, expiring token carrying a
role and an organisation, which is enough for a demo instance that must not be
anonymously mutable and keeps the six roles the PRD specifies real rather than
decorative.

Two properties are load-bearing:

  * **Capabilities, not role checks at call sites.** Endpoints declare what they
    need (`run:approve`); the matrix decides who has it. Adding a role does not
    mean auditing every route.

  * **`live:mutate` is a capability the judge role does not have.** The PRD says
    judges get "no live mutations". Hiding a button is not a control — the server
    refuses, so a judge's approval can open a dry-run pull request and never a
    real one, whatever the client sends.

Tokens are HMAC-SHA256 over a compact JSON payload. No dependency, and the
verification path is small enough to read in one sitting.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field

from fastapi import Depends, HTTPException, Request

# --- capabilities ------------------------------------------------------------

RUN_READ = "run:read"
RUN_TRIGGER = "run:trigger"
RUN_APPROVE = "run:approve"
RUN_RETRY = "run:retry"
RUN_CANCEL = "run:cancel"
DEMO_RESET = "demo:reset"
INCIDENT_MANAGE = "incident:manage"
CONNECTION_READ = "connection:read"
CONNECTION_MANAGE = "connection:manage"
AUDIT_READ = "audit:read"
SETTINGS_MANAGE = "settings:manage"
# Permission to take an action visible outside the sandbox — a real pull
# request. Deliberately withheld from `judge`.
LIVE_MUTATE = "live:mutate"

ALL_CAPABILITIES = {
    RUN_READ, RUN_TRIGGER, RUN_APPROVE, RUN_RETRY, RUN_CANCEL, DEMO_RESET,
    INCIDENT_MANAGE, CONNECTION_READ, CONNECTION_MANAGE, AUDIT_READ,
    SETTINGS_MANAGE, LIVE_MUTATE,
}

ROLES = ("owner", "admin", "operator", "developer", "viewer", "judge")

ROLE_CAPABILITIES: dict[str, set[str]] = {
    # Full organisation control including billing and deletion.
    "owner": set(ALL_CAPABILITIES),
    # Stores, connections, rules, approval, audit — but not org settings.
    "admin": ALL_CAPABILITIES - {SETTINGS_MANAGE},
    # Runs the store day to day.
    "operator": {
        RUN_READ, RUN_TRIGGER, RUN_APPROVE, RUN_RETRY, RUN_CANCEL,
        INCIDENT_MANAGE, CONNECTION_READ, AUDIT_READ, LIVE_MUTATE,
    },
    # Technical context and integrations; retries validations, does not approve.
    "developer": {
        RUN_READ, RUN_RETRY, CONNECTION_READ, CONNECTION_MANAGE, AUDIT_READ,
    },
    "viewer": {RUN_READ, CONNECTION_READ, AUDIT_READ},
    # Demo-only. Can drive and approve the sandbox flow and reset it; cannot
    # cause anything to happen outside it.
    "judge": {
        RUN_READ, RUN_TRIGGER, RUN_APPROVE, DEMO_RESET, CONNECTION_READ, AUDIT_READ,
    },
}


def capabilities_for(role: str) -> set[str]:
    return set(ROLE_CAPABILITIES.get(role, set()))


# --- principal ---------------------------------------------------------------


@dataclass(frozen=True)
class Principal:
    role: str
    organisation_id: str
    subject: str
    expires_at: int
    capabilities: frozenset[str] = field(default_factory=frozenset)

    def can(self, capability: str) -> bool:
        return capability in self.capabilities

    @property
    def is_judge(self) -> bool:
        return self.role == "judge"

    def to_json(self) -> dict:
        return {
            "role": self.role,
            "organisation_id": self.organisation_id,
            "subject": self.subject,
            "expires_at": self.expires_at,
            "capabilities": sorted(self.capabilities),
        }


class AuthError(Exception):
    """Token missing, malformed, expired or wrongly signed."""


# --- token -------------------------------------------------------------------

DEFAULT_TTL_SECONDS = 60 * 60 * 24 * 90  # long-lived: judging runs for weeks


def _secret() -> str:
    """Signing key.

    A generated key is fine for a local run but means tokens die on restart, so
    say so loudly rather than letting a judge's link silently stop working.
    """
    key = os.environ.get("COMGU_AUTH_SECRET")
    if key:
        return key
    global _EPHEMERAL
    if _EPHEMERAL is None:
        _EPHEMERAL = secrets.token_urlsafe(32)
        print(
            "[auth] COMGU_AUTH_SECRET is unset; using an ephemeral key. "
            "Issued tokens will not survive a restart.",
            flush=True,
        )
    return _EPHEMERAL


_EPHEMERAL: str | None = None


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue_token(
    role: str,
    organisation_id: str,
    subject: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> str:
    if role not in ROLES:
        raise AuthError(f"{role!r} is not a known role (expected one of {ROLES})")
    payload = {
        "role": role,
        "org": organisation_id,
        "sub": subject,
        "exp": int(time.time()) + ttl_seconds,
    }
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64(hmac.new(_secret().encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_token(token: str) -> Principal:
    if not token or "." not in token:
        raise AuthError("malformed token")

    body, _, sig = token.rpartition(".")
    expected = _b64(hmac.new(_secret().encode(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, sig):
        raise AuthError("bad signature")

    try:
        payload = json.loads(_unb64(body))
    except Exception as e:
        raise AuthError(f"undecodable payload: {type(e).__name__}") from e

    if int(payload.get("exp", 0)) < int(time.time()):
        raise AuthError("token expired")

    role = payload.get("role")
    if role not in ROLES:
        raise AuthError(f"unknown role {role!r}")

    return Principal(
        role=role,
        organisation_id=payload.get("org", ""),
        subject=payload.get("sub", ""),
        expires_at=int(payload["exp"]),
        capabilities=frozenset(capabilities_for(role)),
    )


# --- FastAPI plumbing --------------------------------------------------------


def _extract(request: Request) -> str | None:
    """Bearer header, then `?token=`, then cookie.

    The query parameter exists so a judge can be handed one clickable link; the
    UI immediately stores it and strips it from the address bar.
    """
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    if token := request.query_params.get("token"):
        return token
    return request.cookies.get("comgu_token")


def principal(request: Request) -> Principal:
    """Require a valid token. 401 when absent or unusable."""
    token = _extract(request)
    if not token:
        raise HTTPException(
            401,
            "authentication required — get a demo token from POST /api/auth/demo-token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return verify_token(token)
    except AuthError as e:
        raise HTTPException(401, f"invalid token: {e}", headers={"WWW-Authenticate": "Bearer"})


def optional_principal(request: Request) -> Principal | None:
    """For endpoints that vary by identity but do not demand one."""
    token = _extract(request)
    if not token:
        return None
    try:
        return verify_token(token)
    except AuthError:
        return None


def require(*capabilities: str):
    """Dependency asserting the caller holds every listed capability."""
    for c in capabilities:
        if c not in ALL_CAPABILITIES:
            raise ValueError(f"unknown capability {c!r}")

    def dependency(who: Principal = Depends(principal)) -> Principal:
        missing = [c for c in capabilities if not who.can(c)]
        if missing:
            raise HTTPException(
                403,
                f"role {who.role!r} lacks {', '.join(missing)}",
            )
        return who

    return dependency


def pr_live_allowed(who: Principal) -> bool:
    """Whether this caller's approval may open a *real* pull request.

    Both conditions must hold: the deployment opted in, and the caller holds
    `live:mutate`. A judge never does, so a judge-driven run stays a dry run
    even on an instance configured for live pull requests.
    """
    configured = os.environ.get("COMGU_PR_LIVE", "").lower() in ("1", "true", "yes")
    return configured and who.can(LIVE_MUTATE)
