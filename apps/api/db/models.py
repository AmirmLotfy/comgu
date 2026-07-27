"""Persistence for Comgu runs.

Shaped to the ERD in the PRD, trimmed to what the golden path and the UI
actually need. Tenant columns (`organisation_id`, `shop_id`) are carried on
every owned row from the start, so multi-tenancy is a later feature rather than
a later migration.

Evidence that is only ever read as a whole — tool traces, snapshots, redacted
command output — is stored as JSON rather than shredded into tables.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Index,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def uid() -> str:
    return uuid.uuid4().hex


def now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, onupdate=now
    )


# --- tenancy -----------------------------------------------------------------


class Organisation(Base, TimestampMixin):
    __tablename__ = "organisations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(180))
    slug: Mapped[str] = mapped_column(String(120), unique=True)
    plan_code: Mapped[str] = mapped_column(String(60), default="free")


class User(Base, TimestampMixin):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(160))
    role: Mapped[str] = mapped_column(String(30), default="owner")


class Shop(Base, TimestampMixin):
    __tablename__ = "shops"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"), index=True)
    platform: Mapped[str] = mapped_column(String(30), default="shopify")
    shop_domain: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(180))
    currency_code: Mapped[str] = mapped_column(String(3), default="USD")
    status: Mapped[str] = mapped_column(String(30), default="active")
    is_demo: Mapped[bool] = mapped_column(Boolean, default=True)


# --- ingest ------------------------------------------------------------------


class WebhookEvent(Base):
    """Raw inbound webhook. Kept so a duplicate can be recognised as one."""

    __tablename__ = "webhook_events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    organisation_id: Mapped[str] = mapped_column(String(32), index=True)
    shop_id: Mapped[str] = mapped_column(String(32), index=True)
    provider: Mapped[str] = mapped_column(String(30), default="shopify")
    topic: Mapped[str] = mapped_column(String(255))
    external_webhook_id: Mapped[str | None] = mapped_column(String(255))
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    hmac_valid: Mapped[bool] = mapped_column(Boolean)
    payload_hash: Mapped[str] = mapped_column(String(64))
    raw_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    headers_redacted: Mapped[dict] = mapped_column(JSON, default=dict)
    processing_status: Mapped[str] = mapped_column(String(30), default="received")
    error_code: Mapped[str | None] = mapped_column(String(100))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CommerceEvent(Base):
    """Normalized commerce change — platform-agnostic."""

    __tablename__ = "commerce_events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    organisation_id: Mapped[str] = mapped_column(String(32), index=True)
    shop_id: Mapped[str] = mapped_column(String(32), index=True)
    webhook_event_id: Mapped[str | None] = mapped_column(ForeignKey("webhook_events.id"))
    event_type: Mapped[str] = mapped_column(String(60))
    source_system: Mapped[str] = mapped_column(String(120))
    entity_type: Mapped[str] = mapped_column(String(120))
    entity_external_id: Mapped[str] = mapped_column(String(255))
    before_state: Mapped[dict | None] = mapped_column(JSON)
    after_state: Mapped[dict] = mapped_column(JSON)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    normalized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)


# --- workflow ----------------------------------------------------------------


class Run(Base, TimestampMixin):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    organisation_id: Mapped[str] = mapped_column(String(32), index=True)
    shop_id: Mapped[str] = mapped_column(String(32), index=True)
    commerce_event_id: Mapped[str | None] = mapped_column(ForeignKey("commerce_events.id"))
    trigger_type: Mapped[str] = mapped_column(String(30), default="manual")
    status: Mapped[str] = mapped_column(String(40), default="RECEIVED", index=True)
    severity: Mapped[str | None] = mapped_column(String(20))
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, default=uid)
    correlation_id: Mapped[str] = mapped_column(String(32), default=uid)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(100))
    failure_message: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    # Worker lease, so a restart can reclaim in-flight work.
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(80))

    transitions: Mapped[list["RunTransition"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="RunTransition.created_at"
    )
    findings: Mapped[list["Finding"]] = relationship(cascade="all, delete-orphan")


class RunTransition(Base):
    """Append-only workflow history."""

    __tablename__ = "run_transitions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    from_status: Mapped[str | None] = mapped_column(String(40))
    to_status: Mapped[str] = mapped_column(String(40))
    transition_reason: Mapped[str | None] = mapped_column(Text)
    actor_type: Mapped[str] = mapped_column(String(20), default="worker")
    actor_user_id: Mapped[str | None] = mapped_column(String(32))
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    run: Mapped[Run] = relationship(back_populates="transitions")


class ContextSnapshot(Base):
    """Immutable record of what DataHub said, including the MCP tool trace."""

    __tablename__ = "context_snapshots"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    datahub_gms_url: Mapped[str] = mapped_column(String(255))
    datahub_version: Mapped[str | None] = mapped_column(String(40))
    root_urn: Mapped[str] = mapped_column(Text)
    lineage_edges: Mapped[int] = mapped_column(Integer, default=0)
    max_hops: Mapped[int] = mapped_column(Integer, default=3)
    assets: Mapped[list] = mapped_column(JSON, default=list)
    tool_trace: Mapped[list] = mapped_column(JSON, default=list)
    checksum: Mapped[str] = mapped_column(String(64))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Finding(Base):
    __tablename__ = "findings"
    __table_args__ = (
        Index("ix_findings_run_severity", "run_id", "severity"),
        Index("ix_findings_shop_status_severity", "shop_id", "status", "severity"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    organisation_id: Mapped[str] = mapped_column(String(32), index=True)
    shop_id: Mapped[str] = mapped_column(String(32), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    rule_code: Mapped[str] = mapped_column(String(120), index=True)
    rule_version: Mapped[int] = mapped_column(Integer, default=1)
    rule_execution_id: Mapped[str | None] = mapped_column(
        ForeignKey("rule_executions.id"), index=True
    )
    status: Mapped[str] = mapped_column(String(30), default="open")
    severity: Mapped[str] = mapped_column(String(20), index=True)
    title: Mapped[str] = mapped_column(String(255))
    summary: Mapped[str] = mapped_column(Text)
    expected_value: Mapped[dict | list | str | int | None] = mapped_column(JSON)
    observed_value: Mapped[dict | list | str | int | None] = mapped_column(JSON)
    source_asset_urn: Mapped[str | None] = mapped_column(Text)
    downstream_asset_urn: Mapped[str | None] = mapped_column(Text)
    owner_reference: Mapped[dict | None] = mapped_column(JSON)
    customer_impact: Mapped[str | None] = mapped_column(Text)
    business_risk: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    auto_fix_eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    remediation_template: Mapped[str | None] = mapped_column(String(120))
    target_file: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class RemediationPlan(Base):
    __tablename__ = "remediation_plans"
    __table_args__ = (UniqueConstraint("run_id", "version"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(30), default="awaiting_approval")
    summary: Mapped[str] = mapped_column(Text)
    business_impact: Mapped[str] = mapped_column(Text)
    proposed_actions: Mapped[list] = mapped_column(JSON, default=list)
    validation_plan: Mapped[list] = mapped_column(JSON, default=list)
    rollback_plan: Mapped[str] = mapped_column(Text)
    confidence_explanation: Mapped[str | None] = mapped_column(Text)
    plan_source: Mapped[str] = mapped_column(String(30), default="deterministic")
    rejected_reason: Mapped[str | None] = mapped_column(Text)
    model_provider: Mapped[str | None] = mapped_column(String(80))
    schema_version: Mapped[int] = mapped_column(Integer, default=2)
    checksum: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Approval(Base):
    """A human decision. Bound to the exact plan and context it was shown."""

    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    remediation_plan_id: Mapped[str] = mapped_column(ForeignKey("remediation_plans.id"))
    decision: Mapped[str] = mapped_column(String(20))
    decided_by: Mapped[str] = mapped_column(String(255))
    decided_by_role: Mapped[str] = mapped_column(String(30), default="owner")
    decision_reason: Mapped[str | None] = mapped_column(Text)
    context_snapshot_checksum: Mapped[str] = mapped_column(String(64))
    plan_checksum: Mapped[str] = mapped_column(String(64))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class GeneratedArtifact(Base):
    __tablename__ = "generated_artifacts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    remediation_plan_id: Mapped[str | None] = mapped_column(String(32))
    artifact_type: Mapped[str] = mapped_column(String(30), default="patch")
    status: Mapped[str] = mapped_column(String(30), default="generated")
    workspace_reference: Mapped[str] = mapped_column(Text)
    checksum: Mapped[str] = mapped_column(String(64))
    combined_diff: Mapped[str] = mapped_column(Text, default="")
    files: Mapped[list] = mapped_column(JSON, default=list)
    skipped: Mapped[list] = mapped_column(JSON, default=list)
    rejected: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ValidationRun(Base):
    __tablename__ = "validation_runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    generated_artifact_id: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(30), default="pending")
    environment: Mapped[str] = mapped_column(String(80), default="isolated-workspace")
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    steps: Mapped[list] = mapped_column(JSON, default=list)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PullRequest(Base):
    __tablename__ = "pull_requests"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    repository_full_name: Mapped[str | None] = mapped_column(String(255))
    branch_name: Mapped[str] = mapped_column(String(255))
    commit_sha: Mapped[str | None] = mapped_column(String(64))
    external_pr_number: Mapped[int | None] = mapped_column(Integer)
    external_pr_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="dry_run")
    body: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class DataHubWriteback(Base):
    __tablename__ = "datahub_writebacks"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    operations: Mapped[list] = mapped_column(JSON, default=list)
    verification_result: Mapped[dict] = mapped_column(JSON, default=dict)
    document_urn: Mapped[str | None] = mapped_column(Text)
    tool_trace: Mapped[list] = mapped_column(JSON, default=list)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    """Append-only. Never updated or deleted by application code."""

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_org_created", "organisation_id", "created_at"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    organisation_id: Mapped[str] = mapped_column(String(32), index=True)
    shop_id: Mapped[str | None] = mapped_column(String(32))
    actor_type: Mapped[str] = mapped_column(String(20))
    actor_user_id: Mapped[str | None] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(160), index=True)
    resource_type: Mapped[str] = mapped_column(String(120))
    resource_id: Mapped[str | None] = mapped_column(String(32))
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


# --- membership (PRD 27.3) ---------------------------------------------------


class Membership(Base):
    """Joins a user to an organisation with a role."""

    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("organisation_id", "user_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(30), default="viewer")
    status: Mapped[str] = mapped_column(String(20), default="active")
    invited_by_user_id: Mapped[str | None] = mapped_column(String(32))
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


# --- connectors (PRD 27.5-27.8) ----------------------------------------------


class Connector(Base, TimestampMixin):
    """A configured integration.

    Secrets are never stored here — `secret_reference` names where the value
    lives, and `last_error_message` is redacted before it is written.
    """

    __tablename__ = "connectors"
    __table_args__ = (Index("ix_connectors_org_type_status", "organisation_id", "connector_type", "status"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"), index=True)
    shop_id: Mapped[str | None] = mapped_column(ForeignKey("shops.id"))
    connector_type: Mapped[str] = mapped_column(String(30))  # shopify | datahub | github
    name: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(30), default="pending")
    configuration: Mapped[dict] = mapped_column(JSON, default=dict)
    secret_reference: Mapped[str | None] = mapped_column(Text)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_message: Mapped[str | None] = mapped_column(Text)


class ShopifyConnection(Base):
    __tablename__ = "shopify_connections"
    connector_id: Mapped[str] = mapped_column(ForeignKey("connectors.id"), primary_key=True)
    shop_id: Mapped[str | None] = mapped_column(ForeignKey("shops.id"))
    shopify_shop_domain: Mapped[str] = mapped_column(String(255), unique=True)
    shopify_shop_gid: Mapped[str | None] = mapped_column(String(255))
    api_version: Mapped[str] = mapped_column(String(20), default="2026-01")
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    # The token itself lives in the environment/secret manager, never here.
    token_reference: Mapped[str | None] = mapped_column(Text)
    installed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    uninstalled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_webhook_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DataHubConnection(Base):
    __tablename__ = "datahub_connections"
    connector_id: Mapped[str] = mapped_column(ForeignKey("connectors.id"), primary_key=True)
    gms_url: Mapped[str] = mapped_column(Text)
    mcp_transport: Mapped[str] = mapped_column(String(20), default="stdio")
    mcp_server_version: Mapped[str | None] = mapped_column(String(40))
    datahub_version: Mapped[str | None] = mapped_column(String(40))
    mutation_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    service_account_name: Mapped[str | None] = mapped_column(String(160))
    secret_reference: Mapped[str | None] = mapped_column(Text)
    last_read_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_write_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GitHubConnection(Base):
    __tablename__ = "github_connections"
    connector_id: Mapped[str] = mapped_column(ForeignKey("connectors.id"), primary_key=True)
    installation_id: Mapped[int | None] = mapped_column(Integer)
    account_login: Mapped[str | None] = mapped_column(String(255))
    repository_owner: Mapped[str | None] = mapped_column(String(255))
    repository_name: Mapped[str | None] = mapped_column(String(255))
    default_branch: Mapped[str] = mapped_column(String(255), default="main")
    repository_allowlist: Mapped[list] = mapped_column(JSON, default=list)
    dry_run_enabled: Mapped[bool] = mapped_column(Boolean, default=True)


# --- rule registry (PRD 27.15-27.18) -----------------------------------------


class RuleDefinition(Base):
    """A logical rule. Projected from ALL_RULES so code stays authoritative."""

    __tablename__ = "rule_definitions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    code: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(180))
    description: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(40))
    default_severity: Mapped[str] = mapped_column(String(20))
    is_system_rule: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class RuleVersion(Base):
    __tablename__ = "rule_versions"
    __table_args__ = (UniqueConstraint("rule_definition_id", "version"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    rule_definition_id: Mapped[str] = mapped_column(ForeignKey("rule_definitions.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    implementation_reference: Mapped[str] = mapped_column(Text)
    remediation_templates: Mapped[list] = mapped_column(JSON, default=list)
    checksum: Mapped[str] = mapped_column(String(64), default="")
    active_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RuleExecution(Base):
    """One rule, one run. Required by PRD 12.8 to record what actually ran."""

    __tablename__ = "rule_executions"
    __table_args__ = (Index("ix_rule_executions_run_status", "run_id", "status"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    rule_version_id: Mapped[str | None] = mapped_column(ForeignKey("rule_versions.id"))
    rule_code: Mapped[str] = mapped_column(String(120), index=True)
    status: Mapped[str] = mapped_column(String(20))
    finding_count: Mapped[int] = mapped_column(Integer, default=0)
    skip_reason: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --- incidents (PRD 27.31-27.33) ---------------------------------------------


class Incident(Base, TimestampMixin):
    """A merchant-facing container for the findings of one run."""

    __tablename__ = "incidents"
    __table_args__ = (Index("ix_incidents_org_status_severity", "organisation_id", "status", "severity"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"), index=True)
    shop_id: Mapped[str] = mapped_column(ForeignKey("shops.id"), index=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(20), index=True)
    status: Mapped[str] = mapped_column(String(30), default="open", index=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(255))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_summary: Mapped[str | None] = mapped_column(Text)

    events: Mapped[list["IncidentEvent"]] = relationship(
        cascade="all, delete-orphan", order_by="IncidentEvent.created_at"
    )


class FindingIncident(Base):
    __tablename__ = "finding_incidents"
    finding_id: Mapped[str] = mapped_column(ForeignKey("findings.id"), primary_key=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class IncidentEvent(Base):
    __tablename__ = "incident_events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(30))
    actor_type: Mapped[str] = mapped_column(String(20), default="worker")
    actor_user_id: Mapped[str | None] = mapped_column(String(255))
    content: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


# --- demo (PRD 27.35-27.36) --------------------------------------------------


class DemoScenario(Base):
    __tablename__ = "demo_scenarios"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    code: Mapped[str] = mapped_column(String(120), unique=True)
    name: Mapped[str] = mapped_column(String(180))
    description: Mapped[str] = mapped_column(Text)
    seed_version: Mapped[int] = mapped_column(Integer, default=1)
    configuration: Mapped[dict] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class DemoReset(Base):
    __tablename__ = "demo_resets"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    demo_scenario_id: Mapped[str | None] = mapped_column(ForeignKey("demo_scenarios.id"))
    organisation_id: Mapped[str] = mapped_column(String(32), index=True)
    shop_id: Mapped[str | None] = mapped_column(String(32))
    requested_by_user_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict] = mapped_column(JSON, default=dict)
