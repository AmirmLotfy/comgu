"""Endpoints backing the operator screens (PRD 13.2, 20).

Kept apart from main.py, which owns the run lifecycle. Everything here is a
read over data the golden path already produced, plus the two run controls the
state machine could reach but nothing could invoke (`retry`, `cancel`).

Connection tests deliberately use a cheap HTTP probe rather than opening an MCP
session: an MCP handshake costs ~17s of DataHub SDK import, which is fine once
per run and far too slow for a button on a settings screen.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from apps.api import auth
from apps.api import incidents as lifecycle
from apps.api.db.models import (
    AuditLog,
    Connector,
    DataHubWriteback,
    Finding,
    GeneratedArtifact,
    Incident,
    Organisation,
    PullRequest,
    RuleDefinition,
    RuleExecution,
    RuleVersion,
    Run,
    Shop,
    ValidationRun,
)
from apps.api.db.session import get_session
from apps.api.workflow import Actor, Status, can, transition

router = APIRouter(prefix="/api")

SEVERITY_ORDER = ["critical", "high", "medium", "low", "informational"]


# --- overview ----------------------------------------------------------------


@router.get("/overview")
def overview(
    db: Session = Depends(get_session),
    who: auth.Principal = Depends(auth.require(auth.RUN_READ)),
) -> dict[str, Any]:
    """Commerce health at a glance (PRD 13.2)."""
    runs = db.query(Run).order_by(desc(Run.created_at)).limit(25).all()
    latest = runs[0] if runs else None

    open_incidents = (
        db.query(Incident)
        .filter(Incident.status.notin_(["resolved", "dismissed"]))
        .order_by(desc(Incident.opened_at))
        .all()
    )
    # A run stopped at approval is the merchant's cue to act — surface it first.
    blocked = [r for r in runs if r.status == Status.AWAITING_APPROVAL]

    unresolved_findings = (
        db.query(Finding.severity, func.count(Finding.id))
        .filter(Finding.status == "open")
        .group_by(Finding.severity)
        .all()
    )
    counts = dict(unresolved_findings)
    critical_open = counts.get("critical", 0) + counts.get("high", 0)

    completed = [r for r in runs if r.status == Status.COMPLETED]
    failed = [r for r in runs if r.status in (Status.FAILED, Status.VALIDATION_FAILED)]

    if blocked or critical_open:
        health, reason = "at_risk", "customer-visible contradictions are unresolved"
    elif failed:
        health, reason = "degraded", "a recent run did not complete"
    elif completed:
        health, reason = "healthy", "every checked surface agrees with the catalog"
    else:
        health, reason = "unknown", "no runs yet"

    connectors = db.query(Connector).all()
    return {
        "commerce_health": {"state": health, "reason": reason},
        "blocked_change": None if not blocked else {
            "run_id": blocked[0].id,
            "severity": blocked[0].severity,
            "since": blocked[0].updated_at.isoformat() if blocked[0].updated_at else None,
        },
        "latest_run": None if not latest else {
            "id": latest.id, "status": latest.status, "severity": latest.severity,
            "created_at": latest.created_at.isoformat() if latest.created_at else None,
        },
        "findings_by_severity": {s: counts.get(s, 0) for s in SEVERITY_ORDER},
        "open_incidents": [lifecycle.to_json(db, i) for i in open_incidents[:5]],
        "counts": {
            "runs": db.query(func.count(Run.id)).scalar(),
            "completed": len(completed),
            "open_incidents": len(open_incidents),
        },
        "connections": [
            {"type": c.connector_type, "status": c.status, "name": c.name} for c in connectors
        ],
    }


# --- incidents ---------------------------------------------------------------


@router.get("/incidents")
def list_incidents(
    status: str | None = None,
    severity: str | None = None,
    db: Session = Depends(get_session),
    who: auth.Principal = Depends(auth.require(auth.RUN_READ)),
) -> dict[str, Any]:
    q = db.query(Incident)
    if status:
        q = q.filter(Incident.status == status)
    if severity:
        q = q.filter(Incident.severity == severity)
    rows = q.order_by(desc(Incident.opened_at)).limit(100).all()
    return {"incidents": [lifecycle.to_json(db, i) for i in rows]}


@router.get("/incidents/{incident_id}")
def get_incident(
    incident_id: str,
    db: Session = Depends(get_session),
    who: auth.Principal = Depends(auth.require(auth.RUN_READ)),
) -> dict[str, Any]:
    incident = db.get(Incident, incident_id)
    if not incident:
        raise HTTPException(404, "incident not found")
    payload = lifecycle.to_json(db, incident, include_events=True)
    payload["findings"] = [
        {
            "id": f.id, "rule_code": f.rule_code, "severity": f.severity,
            "title": f.title, "summary": f.summary, "customer_impact": f.customer_impact,
        }
        for f in db.query(Finding).filter(Finding.id.in_(payload["finding_ids"])).all()
    ]
    return payload


class IncidentPatch(BaseModel):
    status: str | None = None
    owner_user_id: str | None = None
    resolution_summary: str | None = None


ASSIGNABLE_STATUSES = {"open", "investigating", "dismissed"}


@router.patch("/incidents/{incident_id}")
def patch_incident(
    incident_id: str,
    body: IncidentPatch,
    db: Session = Depends(get_session),
    who: auth.Principal = Depends(auth.require(auth.INCIDENT_MANAGE)),
) -> dict[str, Any]:
    incident = db.get(Incident, incident_id)
    if not incident:
        raise HTTPException(404, "incident not found")

    changes: dict[str, Any] = {}
    if body.status is not None:
        # Statuses that mirror the run are set by the workflow, not by hand —
        # letting a human type "resolved" would make the incident disagree with
        # a run that is still mid-flight.
        if body.status not in ASSIGNABLE_STATUSES:
            raise HTTPException(
                409,
                f"{body.status!r} is derived from the run; assignable statuses are "
                f"{sorted(ASSIGNABLE_STATUSES)}",
            )
        changes["status"] = {"from": incident.status, "to": body.status}
        incident.status = body.status
    if body.owner_user_id is not None:
        changes["owner"] = {"from": incident.owner_user_id, "to": body.owner_user_id}
        incident.owner_user_id = body.owner_user_id
    if body.resolution_summary is not None:
        incident.resolution_summary = body.resolution_summary
        changes["resolution_summary"] = True

    if changes:
        from apps.api.db.models import IncidentEvent

        db.add(
            IncidentEvent(
                incident_id=incident.id,
                event_type="assigned" if "owner" in changes else "status_changed",
                actor_type="user", actor_user_id=who.subject, content=changes,
            )
        )
        db.add(
            AuditLog(
                organisation_id=incident.organisation_id, shop_id=incident.shop_id,
                actor_type="user", actor_user_id=who.subject,
                action="incident.updated", resource_type="incident",
                resource_id=incident.id, meta=changes,
            )
        )
        db.commit()
    return lifecycle.to_json(db, incident, include_events=True)


# --- rules -------------------------------------------------------------------


@router.get("/rules")
def list_rules(
    db: Session = Depends(get_session),
    who: auth.Principal = Depends(auth.require(auth.RUN_READ)),
) -> dict[str, Any]:
    out = []
    for d in db.query(RuleDefinition).order_by(RuleDefinition.code).all():
        versions = (
            db.query(RuleVersion)
            .filter(RuleVersion.rule_definition_id == d.id)
            .order_by(desc(RuleVersion.version))
            .all()
        )
        stats = dict(
            db.query(RuleExecution.status, func.count(RuleExecution.id))
            .filter(RuleExecution.rule_code == d.code)
            .group_by(RuleExecution.status)
            .all()
        )
        findings = (
            db.query(func.count(Finding.id)).filter(Finding.rule_code == d.code).scalar() or 0
        )
        out.append(
            {
                "code": d.code, "name": d.name, "description": d.description,
                "category": d.category, "default_severity": d.default_severity,
                "enabled": True,  # tenant overrides are out of scope for now
                "versions": [
                    {
                        "version": v.version,
                        "implementation": v.implementation_reference,
                        "remediation_templates": v.remediation_templates,
                        "checksum": v.checksum[:12],
                    }
                    for v in versions
                ],
                "executions": stats,
                "findings_produced": findings,
            }
        )
    return {"rules": out}


# --- connections -------------------------------------------------------------


def _probe(url: str, timeout: int = 8) -> tuple[bool, str, int]:
    started = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            r.read(2048)
            return True, f"HTTP {r.status}", int((time.monotonic() - started) * 1000)
    except urllib.error.HTTPError as e:
        return e.code < 500, f"HTTP {e.code}", int((time.monotonic() - started) * 1000)
    except Exception as e:
        return False, f"{type(e).__name__}", int((time.monotonic() - started) * 1000)


@router.get("/connections")
def list_connections(
    db: Session = Depends(get_session),
    who: auth.Principal = Depends(auth.require(auth.CONNECTION_READ)),
) -> dict[str, Any]:
    return {
        "connections": [
            {
                "id": c.id, "type": c.connector_type, "name": c.name, "status": c.status,
                "configuration": c.configuration,
                # The reference, never the value.
                "secret_reference": c.secret_reference,
                "last_verified_at": c.last_verified_at.isoformat() if c.last_verified_at else None,
                "last_error_code": c.last_error_code,
                "last_error_message": c.last_error_message,
            }
            for c in db.query(Connector).order_by(Connector.connector_type).all()
        ]
    }


@router.post("/integrations/datahub/test")
def test_datahub(
    db: Session = Depends(get_session),
    who: auth.Principal = Depends(auth.require(auth.CONNECTION_READ)),
) -> dict[str, Any]:
    gms = os.environ.get("DATAHUB_GMS_URL", "")
    if not gms:
        raise HTTPException(503, "DATAHUB_GMS_URL is not configured")
    ok, detail, ms = _probe(gms.rstrip("/") + "/config")

    version = None
    if ok:
        try:
            with urllib.request.urlopen(gms.rstrip("/") + "/config", timeout=8) as r:
                version = (json.load(r).get("versions", {}).get("acryldata/datahub", {})).get("version")
        except Exception:
            pass

    c = db.query(Connector).filter(Connector.connector_type == "datahub").first()
    if c:
        c.status = "healthy" if ok else "failed"
        c.last_verified_at = datetime.now(timezone.utc)
        c.last_error_code = None if ok else "unreachable"
        c.last_error_message = None if ok else f"GMS did not respond: {detail}"
        db.commit()
    return {"ok": ok, "detail": detail, "duration_ms": ms, "datahub_version": version, "gms_url": gms}


@router.get("/integrations/datahub/status")
def datahub_status(
    db: Session = Depends(get_session),
    who: auth.Principal = Depends(auth.require(auth.CONNECTION_READ)),
) -> dict[str, Any]:
    c = db.query(Connector).filter(Connector.connector_type == "datahub").first()
    if not c:
        raise HTTPException(404, "no DataHub connector configured")
    return {
        "status": c.status,
        "configuration": c.configuration,
        "last_verified_at": c.last_verified_at.isoformat() if c.last_verified_at else None,
        "last_error_message": c.last_error_message,
    }


@router.post("/integrations/github/test")
def test_github(
    db: Session = Depends(get_session),
    who: auth.Principal = Depends(auth.require(auth.CONNECTION_READ)),
) -> dict[str, Any]:
    repo = os.environ.get("GITHUB_LAB_REPO", "")
    if not repo:
        raise HTTPException(503, "GITHUB_LAB_REPO is not configured")
    ok, detail, ms = _probe(f"https://api.github.com/repos/{repo}")
    c = db.query(Connector).filter(Connector.connector_type == "github").first()
    if c:
        c.status = "healthy" if ok else "degraded"
        c.last_verified_at = datetime.now(timezone.utc)
        c.last_error_message = None if ok else f"repository not reachable: {detail}"
        db.commit()
    return {"ok": ok, "detail": detail, "duration_ms": ms, "repository": repo}


# --- audit -------------------------------------------------------------------


@router.get("/audit")
def list_audit(
    action: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_session),
    who: auth.Principal = Depends(auth.require(auth.AUDIT_READ)),
) -> dict[str, Any]:
    q = db.query(AuditLog)
    if action:
        q = q.filter(AuditLog.action.like(f"{action}%"))
    rows = q.order_by(desc(AuditLog.created_at)).limit(min(limit, 500)).all()
    return {
        "entries": [
            {
                "id": e.id, "action": e.action, "actor": e.actor_user_id or e.actor_type,
                "actor_type": e.actor_type, "resource_type": e.resource_type,
                "resource_id": e.resource_id, "meta": e.meta,
                "at": e.created_at.isoformat(),
            }
            for e in rows
        ],
        "total": db.query(func.count(AuditLog.id)).scalar(),
    }


# --- settings / shops --------------------------------------------------------


@router.get("/settings")
def settings(
    db: Session = Depends(get_session),
    who: auth.Principal = Depends(auth.require(auth.RUN_READ)),
) -> dict[str, Any]:
    org = db.query(Organisation).first()
    shops = db.query(Shop).all()
    return {
        "organisation": None if not org else {
            "id": org.id, "name": org.name, "slug": org.slug, "plan_code": org.plan_code,
        },
        "you": who.to_json(),
        "roles": {
            role: sorted(caps) for role, caps in auth.ROLE_CAPABILITIES.items()
        },
        "shops": [
            {"id": s.id, "domain": s.shop_domain, "name": s.display_name,
             "platform": s.platform, "status": s.status, "is_demo": s.is_demo}
            for s in shops
        ],
        "demo": {
            "pr_live_configured": os.environ.get("COMGU_PR_LIVE", "").lower() in ("1", "true"),
            "pr_live_for_you": auth.pr_live_allowed(who),
            "datahub_gms_url": os.environ.get("DATAHUB_GMS_URL", ""),
        },
    }


@router.get("/shops")
def list_shops(
    db: Session = Depends(get_session),
    who: auth.Principal = Depends(auth.require(auth.RUN_READ)),
) -> dict[str, Any]:
    return {
        "shops": [
            {"id": s.id, "domain": s.shop_domain, "name": s.display_name,
             "platform": s.platform, "status": s.status, "is_demo": s.is_demo}
            for s in db.query(Shop).all()
        ]
    }


# --- run controls and artifacts ---------------------------------------------


@router.post("/runs/{run_id}/cancel")
def cancel_run(
    run_id: str,
    db: Session = Depends(get_session),
    who: auth.Principal = Depends(auth.require(auth.RUN_CANCEL)),
) -> dict[str, Any]:
    """Reachable at last — CANCELLED existed in the state machine with no route."""
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(404, "run not found")
    if not can(run, Status.CANCELLED):
        raise HTTPException(409, f"a run in {run.status} cannot be cancelled")
    transition(
        db, run, Status.CANCELLED, reason=f"cancelled by {who.subject}",
        actor=Actor(type="user", id=who.subject),
    )
    lifecycle.follow_run(db, run, actor_type="user", actor_user_id=who.subject)
    return {"run_id": run.id, "status": run.status}


@router.post("/runs/{run_id}/retry")
def retry_run(
    run_id: str,
    db: Session = Depends(get_session),
    who: auth.Principal = Depends(auth.require(auth.RUN_RETRY)),
) -> dict[str, Any]:
    """Send a failed validation back for revision (PRD 17)."""
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(404, "run not found")
    if run.status != Status.VALIDATION_FAILED:
        raise HTTPException(
            409, f"only a run in VALIDATION_FAILED can be retried; this one is {run.status}"
        )
    run.retry_count += 1
    transition(
        db, run, Status.AWAITING_APPROVAL,
        reason=f"returned for revision by {who.subject} (retry {run.retry_count})",
        actor=Actor(type="user", id=who.subject),
    )
    lifecycle.follow_run(db, run, actor_type="user", actor_user_id=who.subject)
    return {"run_id": run.id, "status": run.status, "retry_count": run.retry_count}


def _latest(db: Session, model, run_id: str, order_col):
    return db.query(model).filter(model.run_id == run_id).order_by(desc(order_col)).first()


@router.get("/runs/{run_id}/findings")
def run_findings(
    run_id: str,
    db: Session = Depends(get_session),
    who: auth.Principal = Depends(auth.require(auth.RUN_READ)),
) -> dict[str, Any]:
    rows = db.query(Finding).filter(Finding.run_id == run_id).all()
    rows.sort(key=lambda f: SEVERITY_ORDER.index(f.severity) if f.severity in SEVERITY_ORDER else 9)
    return {"findings": [
        {"id": f.id, "rule_code": f.rule_code, "severity": f.severity, "title": f.title,
         "summary": f.summary, "expected_value": f.expected_value,
         "observed_value": f.observed_value, "customer_impact": f.customer_impact,
         "business_risk": f.business_risk, "target_file": f.target_file,
         "auto_fix_eligible": f.auto_fix_eligible}
        for f in rows
    ]}


@router.get("/findings/{finding_id}")
def get_finding(
    finding_id: str,
    db: Session = Depends(get_session),
    who: auth.Principal = Depends(auth.require(auth.RUN_READ)),
) -> dict[str, Any]:
    f = db.get(Finding, finding_id)
    if not f:
        raise HTTPException(404, "finding not found")
    return {
        "id": f.id, "run_id": f.run_id, "rule_code": f.rule_code,
        "rule_version": f.rule_version, "severity": f.severity, "status": f.status,
        "title": f.title, "summary": f.summary,
        "expected_value": f.expected_value, "observed_value": f.observed_value,
        "source_asset_urn": f.source_asset_urn, "downstream_asset_urn": f.downstream_asset_urn,
        "owner_reference": f.owner_reference, "customer_impact": f.customer_impact,
        "business_risk": f.business_risk, "confidence": f.confidence,
        "auto_fix_eligible": f.auto_fix_eligible,
        "remediation_template": f.remediation_template, "target_file": f.target_file,
        "evidence": f.evidence, "detected_at": f.detected_at.isoformat(),
    }


@router.get("/runs/{run_id}/diff")
def run_diff(
    run_id: str,
    db: Session = Depends(get_session),
    who: auth.Principal = Depends(auth.require(auth.RUN_READ)),
) -> dict[str, Any]:
    a = _latest(db, GeneratedArtifact, run_id, GeneratedArtifact.created_at)
    if not a:
        raise HTTPException(404, "no patch generated for this run")
    return {"checksum": a.checksum, "combined_diff": a.combined_diff,
            "files": a.files, "skipped": a.skipped, "rejected": a.rejected}


@router.get("/runs/{run_id}/validation")
def run_validation_result(
    run_id: str,
    db: Session = Depends(get_session),
    who: auth.Principal = Depends(auth.require(auth.RUN_READ)),
) -> dict[str, Any]:
    v = _latest(db, ValidationRun, run_id, ValidationRun.started_at)
    if not v:
        raise HTTPException(404, "no validation run for this run")
    return {"status": v.status, "summary": v.summary, "steps": v.steps,
            "duration_ms": v.duration_ms, "environment": v.environment}


@router.get("/runs/{run_id}/pull-request")
def run_pull_request(
    run_id: str,
    db: Session = Depends(get_session),
    who: auth.Principal = Depends(auth.require(auth.RUN_READ)),
) -> dict[str, Any]:
    pr = _latest(db, PullRequest, run_id, PullRequest.created_at)
    if not pr:
        raise HTTPException(404, "no pull request for this run")
    return {"status": pr.status, "branch": pr.branch_name, "url": pr.external_pr_url,
            "number": pr.external_pr_number, "repository": pr.repository_full_name,
            "commit_sha": pr.commit_sha, "error": pr.error, "body": pr.body}


@router.get("/runs/{run_id}/datahub-writeback")
def run_writeback(
    run_id: str,
    db: Session = Depends(get_session),
    who: auth.Principal = Depends(auth.require(auth.RUN_READ)),
) -> dict[str, Any]:
    w = _latest(db, DataHubWriteback, run_id, DataHubWriteback.started_at)
    if not w:
        raise HTTPException(404, "no write-back for this run")
    return {"status": w.status, "operations": w.operations,
            "verification": w.verification_result, "document_urn": w.document_urn,
            "tool_trace": w.tool_trace}
