"""Project code-defined rules and env-defined connectors into the database.

The rule registry is a **projection**, not a second source of truth. `ALL_RULES`
in packages/rules/checks.py already carries every field the PRD asks a
`rule_definition` to hold, so the tables are refreshed from it at startup. The
alternative — a registry the code must be kept in sync with — has a failure mode
where the database claims a rule is at version 2 and the executing code
disagrees, and nothing detects it.

Connectors are the same idea for integrations: the deployment is configured by
environment, and these rows are how the Connections screen sees that
configuration without ever reading a secret.
"""

from __future__ import annotations

import hashlib
import inspect
import os
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from apps.api.db.models import (
    Connector,
    DataHubConnection,
    DemoScenario,
    GitHubConnection,
    RuleDefinition,
    RuleVersion,
    ShopifyConnection,
)
from packages.patch.templates import NON_FILE_TEMPLATES, TEMPLATES
from packages.rules.checks import ALL_RULES

DEMO_SCENARIO_CODE = "northstar-brew-pro-price-inventory"


def _checksum(rule) -> str:
    """Hash the rule's own source so a changed implementation is visible."""
    try:
        src = inspect.getsource(type(rule))
    except (OSError, TypeError):
        src = f"{rule.code}:{rule.version}"
    return hashlib.sha256(src.encode()).hexdigest()


def sync_rule_registry(db: Session) -> int:
    """Upsert rule_definitions and rule_versions from the executing code."""
    touched = 0
    for rule in ALL_RULES:
        definition = db.query(RuleDefinition).filter(RuleDefinition.code == rule.code).first()
        if definition is None:
            definition = RuleDefinition(code=rule.code)
            db.add(definition)

        definition.name = rule.code.replace("_", " ").title()
        definition.description = rule.description
        definition.category = rule.category
        # The floor severity a rule can produce; actual severity is graded per
        # asset from DataHub criticality at check time.
        definition.default_severity = "critical" if rule.code == "inventory_safety" else "high"
        definition.is_system_rule = True
        # Flush only once the row is complete — a NOT NULL column is otherwise
        # written before it has a value.
        db.flush()

        version = (
            db.query(RuleVersion)
            .filter(
                RuleVersion.rule_definition_id == definition.id,
                RuleVersion.version == rule.version,
            )
            .first()
        )
        if version is None:
            version = RuleVersion(rule_definition_id=definition.id, version=rule.version)
            db.add(version)

        templates = [rule.remediation_template] if rule.remediation_template else []
        version.implementation_reference = f"{type(rule).__module__}.{type(rule).__qualname__}"
        version.remediation_templates = [
            t for t in templates if t in TEMPLATES or t in NON_FILE_TEMPLATES
        ]
        version.checksum = _checksum(rule)
        touched += 1

    db.commit()
    return touched


def active_rule_version(db: Session, rule_code: str, version: int) -> RuleVersion | None:
    definition = db.query(RuleDefinition).filter(RuleDefinition.code == rule_code).first()
    if definition is None:
        return None
    return (
        db.query(RuleVersion)
        .filter(RuleVersion.rule_definition_id == definition.id, RuleVersion.version == version)
        .first()
    )


# --- connectors --------------------------------------------------------------


def _upsert_connector(
    db: Session, org_id: str, shop_id: str | None, kind: str, name: str
) -> Connector:
    c = (
        db.query(Connector)
        .filter(Connector.organisation_id == org_id, Connector.connector_type == kind)
        .first()
    )
    if c is None:
        c = Connector(organisation_id=org_id, connector_type=kind, name=name)
        db.add(c)
        db.flush()
    c.name = name
    c.shop_id = shop_id
    return c


def sync_connectors(db: Session, org_id: str, shop_id: str) -> list[Connector]:
    """Reflect the deployment's environment as connector rows.

    `secret_reference` names where a credential lives; the value never lands in
    the database, and `configuration` holds only non-secret settings.
    """
    now = datetime.now(timezone.utc)
    out: list[Connector] = []

    # --- DataHub ---
    gms = os.environ.get("DATAHUB_GMS_URL", "")
    c = _upsert_connector(db, org_id, shop_id, "datahub", "DataHub Core")
    c.status = "healthy" if gms else "pending"
    c.configuration = {"gms_url": gms, "mcp_transport": "stdio"}
    c.secret_reference = "env:DATAHUB_GMS_TOKEN"
    if gms:
        c.last_verified_at = now
    dh = db.get(DataHubConnection, c.id) or DataHubConnection(connector_id=c.id)
    dh.gms_url = gms
    dh.mcp_transport = "stdio"
    dh.mutation_enabled = True
    dh.secret_reference = "env:DATAHUB_GMS_TOKEN"
    db.merge(dh)
    out.append(c)

    # --- GitHub ---
    repo = os.environ.get("GITHUB_LAB_REPO", "")
    c = _upsert_connector(db, org_id, shop_id, "github", "GitHub")
    c.status = "healthy" if repo else "pending"
    c.configuration = {
        "repository": repo,
        "dry_run": os.environ.get("COMGU_PR_LIVE", "").lower() not in ("1", "true", "yes"),
    }
    c.secret_reference = "env:GITHUB_TOKEN"
    if repo:
        c.last_verified_at = now
    gh = db.get(GitHubConnection, c.id) or GitHubConnection(connector_id=c.id)
    owner, _, repo_name = repo.partition("/")
    gh.repository_owner, gh.repository_name = owner or None, repo_name or None
    gh.repository_allowlist = [repo] if repo else []
    gh.dry_run_enabled = os.environ.get("COMGU_PR_LIVE", "").lower() not in ("1", "true", "yes")
    db.merge(gh)
    out.append(c)

    # --- Shopify ---
    domain = os.environ.get("SHOPIFY_SHOP_DOMAIN", "")
    configured = bool(os.environ.get("SHOPIFY_API_KEY") and os.environ.get("SHOPIFY_WEBHOOK_SECRET"))
    c = _upsert_connector(db, org_id, shop_id, "shopify", "Shopify")
    c.status = "healthy" if configured else "pending"
    c.configuration = {
        "shop_domain": domain,
        "api_version": os.environ.get("SHOPIFY_API_VERSION", "2026-01"),
        "webhooks_configured": configured,
    }
    c.secret_reference = "env:SHOPIFY_API_SECRET"
    c.last_error_code = None if configured else "not_configured"
    c.last_error_message = (
        None if configured else "no Shopify credentials on this deployment; webhooks are simulated"
    )
    if domain:
        sc = db.get(ShopifyConnection, c.id) or ShopifyConnection(connector_id=c.id)
        sc.shopify_shop_domain = domain
        sc.shop_id = shop_id
        sc.api_version = os.environ.get("SHOPIFY_API_VERSION", "2026-01")
        sc.scopes = ["read_products", "read_inventory", "read_locations"]
        sc.token_reference = "env:SHOPIFY_ACCESS_TOKEN"
        db.merge(sc)
    out.append(c)

    db.commit()
    return out


def sync_demo_scenario(db: Session) -> DemoScenario:
    s = db.query(DemoScenario).filter(DemoScenario.code == DEMO_SCENARIO_CODE).first()
    if s is None:
        s = DemoScenario(code=DEMO_SCENARIO_CODE)
        db.add(s)
    s.name = "Northstar Brew Pro price and inventory change"
    s.description = (
        "Price moves $89 -> $109 and sellable inventory 12 -> 3. Five downstream "
        "surfaces fail to follow, one of them unowned."
    )
    s.seed_version = 1
    s.configuration = {
        "sku": "NH-BREW-PRO",
        "price_before": "89.00",
        "price_after": "109.00",
        "inventory_before": 12,
        "inventory_after": 3,
        "expected_findings": 6,
    }
    s.active = True
    db.commit()
    return s
