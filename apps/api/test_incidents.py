"""Incident lifecycle, rule registry and connector reflection. No network."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker

from apps.api import incidents as lifecycle
from apps.api.db.models import (
    Base,
    Connector,
    DemoScenario,
    Finding,
    FindingIncident,
    Incident,
    Organisation,
    RuleDefinition,
    RuleVersion,
    Run,
    Shop,
)
from apps.api.db.session import make_engine
from apps.api.registry import (
    active_rule_version,
    sync_connectors,
    sync_demo_scenario,
    sync_rule_registry,
)
from apps.api.workflow import Status, transition
from packages.rules.checks import ALL_RULES


@pytest.fixture
def db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/i.db")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


@pytest.fixture
def tenant(db):
    org = Organisation(name="Northstar Home", slug="northstar")
    db.add(org)
    db.flush()
    shop = Shop(organisation_id=org.id, shop_domain="n.myshopify.com", display_name="Northstar")
    db.add(shop)
    db.commit()
    return org, shop


@pytest.fixture
def run_with_findings(db, tenant):
    org, shop = tenant
    run = Run(organisation_id=org.id, shop_id=shop.id, status=Status.RECEIVED)
    db.add(run)
    db.flush()
    for sev, title, owner in [
        ("critical", "Merchant feed advertises a stale price", {"primary": "u:commerce"}),
        ("critical", "Bundle can oversell available inventory", {"primary": "u:commerce"}),
        ("high", "AI shopping manifest is stale", None),
        ("medium", "Storefront return policy contradicts", {"primary": "u:data"}),
    ]:
        db.add(
            Finding(
                organisation_id=org.id, shop_id=shop.id, run_id=run.id,
                rule_code="x", severity=sev, title=title, summary="s",
                expected_value="a", observed_value="b", owner_reference=owner,
            )
        )
    db.commit()
    return run


# --- rule registry -----------------------------------------------------------


def test_registry_projects_every_rule_from_code(db):
    n = sync_rule_registry(db)
    assert n == len(ALL_RULES)
    codes = {d.code for d in db.query(RuleDefinition).all()}
    assert codes == {r.code for r in ALL_RULES}


def test_registry_is_idempotent(db):
    sync_rule_registry(db)
    sync_rule_registry(db)
    assert db.query(RuleDefinition).count() == len(ALL_RULES)
    assert db.query(RuleVersion).count() == len(ALL_RULES)


def test_registry_records_only_registered_templates(db):
    """A rule cannot advertise a remediation the patcher does not implement."""
    from packages.patch.templates import NON_FILE_TEMPLATES, TEMPLATES

    sync_rule_registry(db)
    for v in db.query(RuleVersion).all():
        for t in v.remediation_templates:
            assert t in TEMPLATES or t in NON_FILE_TEMPLATES


def test_registry_version_is_findable(db):
    sync_rule_registry(db)
    rule = ALL_RULES[0]
    assert active_rule_version(db, rule.code, rule.version) is not None
    assert active_rule_version(db, rule.code, 999) is None


def test_registry_checksum_tracks_the_implementation(db):
    sync_rule_registry(db)
    checksums = {v.checksum for v in db.query(RuleVersion).all()}
    assert all(len(c) == 64 for c in checksums)
    assert len(checksums) == len(ALL_RULES), "each rule should hash distinctly"


# --- incidents ---------------------------------------------------------------


def test_incident_takes_the_worst_severity(db, run_with_findings):
    incident = lifecycle.open_for_run(db, run_with_findings)
    assert incident.severity == "critical"
    assert incident.status == "open"


def test_incident_groups_every_finding(db, run_with_findings):
    incident = lifecycle.open_for_run(db, run_with_findings)
    links = db.query(FindingIncident).filter(FindingIncident.incident_id == incident.id).count()
    assert links == 4


def test_incident_mentions_the_ownership_gap(db, run_with_findings):
    incident = lifecycle.open_for_run(db, run_with_findings)
    assert "no owner" in incident.description


def test_opening_is_idempotent_per_run(db, run_with_findings):
    a = lifecycle.open_for_run(db, run_with_findings)
    b = lifecycle.open_for_run(db, run_with_findings)
    assert a.id == b.id
    assert db.query(Incident).count() == 1


def test_no_findings_means_no_incident(db, tenant):
    org, shop = tenant
    run = Run(organisation_id=org.id, shop_id=shop.id, status=Status.RECEIVED)
    db.add(run)
    db.commit()
    assert lifecycle.open_for_run(db, run) is None


def test_status_follows_the_run_rather_than_being_set_apart(db, run_with_findings):
    run = run_with_findings
    incident = lifecycle.open_for_run(db, run)

    for status, expected in [
        (Status.NORMALIZED, "open"),               # unmapped: incident unchanged
        (Status.CONTEXT_PENDING, "open"),
        (Status.CONTEXT_RESOLVED, "open"),
        (Status.CHECKS_RUNNING, "open"),
        (Status.CHECKS_COMPLETED, "open"),
        (Status.REMEDIATION_PLANNING, "investigating"),
        (Status.AWAITING_APPROVAL, "awaiting_approval"),
        (Status.APPROVED, "fixing"),
    ]:
        transition(db, run, status)
        lifecycle.follow_run(db, run)
        db.refresh(incident)
        assert incident.status == expected, f"run {status} -> incident {incident.status}"


def test_resolution_records_a_summary_and_timestamp(db, run_with_findings):
    run = run_with_findings
    incident = lifecycle.open_for_run(db, run)
    for s in (
        Status.NORMALIZED, Status.CONTEXT_PENDING, Status.CONTEXT_RESOLVED,
        Status.CHECKS_RUNNING, Status.CHECKS_COMPLETED, Status.REMEDIATION_PLANNING,
        Status.AWAITING_APPROVAL, Status.APPROVED, Status.PATCH_GENERATING,
        Status.PATCH_GENERATED, Status.VALIDATION_RUNNING, Status.VALIDATED,
        Status.PULL_REQUEST_CREATING, Status.PULL_REQUEST_OPENED,
        Status.DATAHUB_WRITEBACK_PENDING, Status.DATAHUB_UPDATED, Status.COMPLETED,
    ):
        transition(db, run, s)
        lifecycle.follow_run(db, run)

    db.refresh(incident)
    assert incident.status == "resolved"
    assert incident.resolved_at is not None
    assert incident.resolution_summary


def test_rejection_dismisses_without_deleting_evidence(db, run_with_findings):
    run = run_with_findings
    incident = lifecycle.open_for_run(db, run)
    for s in (
        Status.NORMALIZED, Status.CONTEXT_PENDING, Status.CONTEXT_RESOLVED,
        Status.CHECKS_RUNNING, Status.CHECKS_COMPLETED, Status.REMEDIATION_PLANNING,
        Status.AWAITING_APPROVAL, Status.REJECTED,
    ):
        transition(db, run, s)
        lifecycle.follow_run(db, run)

    db.refresh(incident)
    assert incident.status == "dismissed"
    # PRD 12.10: rejection must not delete the evidence.
    assert db.query(Finding).filter(Finding.run_id == run.id).count() == 4


def test_validation_failure_is_visible_to_the_merchant(db, run_with_findings):
    run = run_with_findings
    incident = lifecycle.open_for_run(db, run)
    for s in (
        Status.NORMALIZED, Status.CONTEXT_PENDING, Status.CONTEXT_RESOLVED,
        Status.CHECKS_RUNNING, Status.CHECKS_COMPLETED, Status.REMEDIATION_PLANNING,
        Status.AWAITING_APPROVAL, Status.APPROVED, Status.PATCH_GENERATING,
        Status.PATCH_GENERATED, Status.VALIDATION_RUNNING, Status.VALIDATION_FAILED,
    ):
        transition(db, run, s)
        lifecycle.follow_run(db, run)
    db.refresh(incident)
    assert incident.status == "validation_failed"


def test_every_status_change_appends_a_timeline_event(db, run_with_findings):
    run = run_with_findings
    incident = lifecycle.open_for_run(db, run)
    for s in (Status.NORMALIZED, Status.CONTEXT_PENDING, Status.CONTEXT_RESOLVED,
              Status.CHECKS_RUNNING, Status.CHECKS_COMPLETED, Status.REMEDIATION_PLANNING,
              Status.AWAITING_APPROVAL):
        transition(db, run, s)
        lifecycle.follow_run(db, run)

    payload = lifecycle.to_json(db, incident, include_events=True)
    kinds = [e["event_type"] for e in payload["timeline"]]
    assert kinds[0] == "opened"
    assert "approval" in kinds
    # An unchanged status must not append noise.
    assert len(kinds) == len(set(zip(kinds, [e["content"]["to"] for e in payload["timeline"][1:]] + [None])))


# --- connectors --------------------------------------------------------------


def test_connectors_reflect_the_environment_without_storing_secrets(db, tenant, monkeypatch):
    org, shop = tenant
    monkeypatch.setenv("DATAHUB_GMS_URL", "http://localhost:8080")
    monkeypatch.setenv("GITHUB_LAB_REPO", "owner/repo")
    monkeypatch.setenv("SHOPIFY_API_KEY", "k")
    monkeypatch.setenv("SHOPIFY_API_SECRET", "super-secret-value")
    monkeypatch.setenv("SHOPIFY_WEBHOOK_SECRET", "s")

    sync_connectors(db, org.id, shop.id)
    rows = db.query(Connector).all()
    assert {c.connector_type for c in rows} == {"datahub", "github", "shopify"}
    assert all(c.status == "healthy" for c in rows)

    blob = " ".join(f"{c.configuration} {c.secret_reference}" for c in rows)
    assert "super-secret-value" not in blob, "a secret value reached the database"
    assert "env:" in blob, "secrets should be referenced, not stored"


def test_unconfigured_connector_says_why(db, tenant, monkeypatch):
    org, shop = tenant
    for k in ("SHOPIFY_API_KEY", "SHOPIFY_WEBHOOK_SECRET", "SHOPIFY_SHOP_DOMAIN"):
        monkeypatch.delenv(k, raising=False)
    sync_connectors(db, org.id, shop.id)
    shopify = db.query(Connector).filter(Connector.connector_type == "shopify").first()
    assert shopify.status == "pending"
    assert shopify.last_error_code == "not_configured"
    assert "simulated" in shopify.last_error_message


def test_connector_sync_is_idempotent(db, tenant):
    org, shop = tenant
    sync_connectors(db, org.id, shop.id)
    sync_connectors(db, org.id, shop.id)
    assert db.query(Connector).count() == 3


def test_demo_scenario_is_recorded(db):
    s = sync_demo_scenario(db)
    assert s.configuration["expected_findings"] == 6
    sync_demo_scenario(db)
    assert db.query(DemoScenario).count() == 1
