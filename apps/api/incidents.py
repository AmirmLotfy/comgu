"""Incident lifecycle (PRD 12.15).

An incident is the merchant-facing container for one run's findings. Runs are the
engineering object — 23 states, checksums, tool traces; an incident is what an
operator is actually asked about, so its status vocabulary is theirs
(`open`, `awaiting_approval`, `fixing`, `resolved`) rather than the machine's.

Status is derived from run transitions rather than set independently, so the two
cannot disagree. Every change appends an `incident_events` row, which is what the
timeline renders.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from apps.api.db.models import Finding, FindingIncident, Incident, IncidentEvent, Run
from apps.api.workflow import Status

# Run status -> incident status. Anything unlisted leaves the incident alone.
RUN_TO_INCIDENT = {
    Status.CHECKS_COMPLETED: "open",
    Status.REMEDIATION_PLANNING: "investigating",
    Status.AWAITING_APPROVAL: "awaiting_approval",
    Status.APPROVED: "fixing",
    Status.PATCH_GENERATING: "fixing",
    Status.PATCH_GENERATED: "fixing",
    Status.VALIDATION_RUNNING: "fixing",
    Status.VALIDATED: "fixing",
    Status.VALIDATION_FAILED: "validation_failed",
    Status.PULL_REQUEST_OPENED: "fixing",
    Status.DATAHUB_UPDATED: "fixing",
    Status.COMPLETED: "resolved",
    Status.REJECTED: "dismissed",
    Status.CANCELLED: "dismissed",
    Status.FAILED: "validation_failed",
}

SEVERITY_ORDER = ["informational", "low", "medium", "high", "critical"]


def _worst(severities: list[str]) -> str:
    ranked = [s for s in SEVERITY_ORDER if s in severities]
    return ranked[-1] if ranked else "informational"


def open_for_run(db: Session, run: Run) -> Incident | None:
    """Create the incident for a run's findings. Idempotent per run."""
    existing = db.query(Incident).filter(Incident.run_id == run.id).first()
    if existing:
        return existing

    findings = db.query(Finding).filter(Finding.run_id == run.id).all()
    if not findings:
        return None

    severity = _worst([f.severity for f in findings])
    critical = [f for f in findings if f.severity in ("critical", "high")]
    unowned = [f for f in findings if not f.owner_reference]

    headline = critical[0].title if critical else findings[0].title
    description = (
        f"{len(findings)} contradiction(s) found after a commerce change. "
        f"Most serious: {headline}."
    )
    if unowned:
        description += (
            f" {len(unowned)} affected surface(s) have no owner recorded in DataHub, "
            "so there is nobody to route the correction to."
        )

    incident = Incident(
        organisation_id=run.organisation_id,
        shop_id=run.shop_id,
        run_id=run.id,
        title=headline,
        description=description,
        severity=severity,
        status="open",
    )
    db.add(incident)
    db.flush()

    for f in findings:
        db.add(FindingIncident(finding_id=f.id, incident_id=incident.id))

    db.add(
        IncidentEvent(
            incident_id=incident.id,
            event_type="opened",
            actor_type="worker",
            content={
                "findings": len(findings),
                "severity": severity,
                "by_severity": {
                    s: sum(1 for f in findings if f.severity == s) for s in SEVERITY_ORDER
                },
            },
        )
    )
    db.commit()
    return incident


def follow_run(
    db: Session,
    run: Run,
    *,
    actor_type: str = "worker",
    actor_user_id: str | None = None,
    detail: dict | None = None,
) -> Incident | None:
    """Move the incident to match the run's current status."""
    incident = db.query(Incident).filter(Incident.run_id == run.id).first()
    if incident is None:
        return None

    target = RUN_TO_INCIDENT.get(run.status)
    if target is None or target == incident.status:
        return incident

    previous, incident.status = incident.status, target

    if target == "resolved":
        incident.resolved_at = datetime.now(timezone.utc)
        incident.resolution_summary = (
            (detail or {}).get("resolution")
            or "Corrections generated, validated and written back to DataHub."
        )

    event_type = {
        "resolved": "resolved",
        "dismissed": "status_changed",
        "awaiting_approval": "approval",
        "validation_failed": "validation",
    }.get(target, "status_changed")

    db.add(
        IncidentEvent(
            incident_id=incident.id,
            event_type=event_type,
            actor_type=actor_type,
            actor_user_id=actor_user_id,
            content={"from": previous, "to": target, "run_status": run.status, **(detail or {})},
        )
    )
    db.commit()
    return incident


def for_run(db: Session, run_id: str) -> Incident | None:
    return db.query(Incident).filter(Incident.run_id == run_id).first()


def to_json(db: Session, incident: Incident, include_events: bool = False) -> dict:
    finding_ids = [
        fi.finding_id
        for fi in db.query(FindingIncident).filter(FindingIncident.incident_id == incident.id).all()
    ]
    payload = {
        "id": incident.id,
        "title": incident.title,
        "description": incident.description,
        "severity": incident.severity,
        "status": incident.status,
        "run_id": incident.run_id,
        "owner_user_id": incident.owner_user_id,
        "opened_at": incident.opened_at.isoformat() if incident.opened_at else None,
        "resolved_at": incident.resolved_at.isoformat() if incident.resolved_at else None,
        "resolution_summary": incident.resolution_summary,
        "finding_count": len(finding_ids),
    }
    if include_events:
        payload["timeline"] = [
            {
                "event_type": e.event_type,
                "actor": e.actor_user_id or e.actor_type,
                "content": e.content,
                "at": e.created_at.isoformat(),
            }
            for e in incident.events
        ]
        payload["finding_ids"] = finding_ids
    return payload
