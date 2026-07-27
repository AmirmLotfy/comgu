"""Comgu API.

Serves the operator UI, the run lifecycle, and the Shopify webhook endpoint.
Approval is an HTTP action taken by a person; nothing downstream of it happens
without an Approval row bound to the exact plan and context that were shown.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from apps.api import shopify
from apps.api.db.models import (
    Approval,
    AuditLog,
    CommerceEvent,
    ContextSnapshot,
    DataHubWriteback,
    Finding,
    GeneratedArtifact,
    Organisation,
    PullRequest,
    RemediationPlan,
    Run,
    Shop,
    ValidationRun,
    WebhookEvent,
)
from apps.api.db.session import get_session, init_db, session
from apps.api.runner import advance_after_approval, advance_to_approval, checksum
from apps.api.workflow import Actor, Status, transition
from packages.lab import bridge

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"

DEMO_ORG_SLUG = "northstar-home"

@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    with session() as db:
        ensure_demo_tenant(db)
    yield


app = FastAPI(
    title="Comgu",
    description="Catch commerce changes before customers do.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("COMGU_CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- bootstrap ---------------------------------------------------------------


def ensure_demo_tenant(db: Session) -> tuple[Organisation, Shop]:
    org = db.query(Organisation).filter(Organisation.slug == DEMO_ORG_SLUG).first()
    if org is None:
        org = Organisation(name="Northstar Home", slug=DEMO_ORG_SLUG)
        db.add(org)
        db.flush()
    shop = db.query(Shop).filter(Shop.organisation_id == org.id).first()
    if shop is None:
        shop = Shop(
            organisation_id=org.id,
            shop_domain=os.environ.get("SHOPIFY_SHOP_DOMAIN", "northstar-home.myshopify.com"),
            display_name="Northstar Home",
            is_demo=True,
        )
        db.add(shop)
    db.commit()
    return org, shop


# --- serialisation -----------------------------------------------------------


def run_summary(db: Session, run: Run) -> dict[str, Any]:
    counts = dict(
        db.query(Finding.severity, func.count(Finding.id))
        .filter(Finding.run_id == run.id)
        .group_by(Finding.severity)
        .all()
    )
    return {
        "id": run.id,
        "status": run.status,
        "severity": run.severity,
        "trigger_type": run.trigger_type,
        "finding_count": sum(counts.values()),
        "counts_by_severity": counts,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "failure_message": run.failure_message,
    }


def run_detail(db: Session, run: Run) -> dict[str, Any]:
    snap = (
        db.query(ContextSnapshot).filter(ContextSnapshot.run_id == run.id)
        .order_by(desc(ContextSnapshot.retrieved_at)).first()
    )
    plan = (
        db.query(RemediationPlan).filter(RemediationPlan.run_id == run.id)
        .order_by(desc(RemediationPlan.created_at)).first()
    )
    approval = (
        db.query(Approval).filter(Approval.run_id == run.id)
        .order_by(desc(Approval.decided_at)).first()
    )
    artifact = (
        db.query(GeneratedArtifact).filter(GeneratedArtifact.run_id == run.id)
        .order_by(desc(GeneratedArtifact.created_at)).first()
    )
    validation = (
        db.query(ValidationRun).filter(ValidationRun.run_id == run.id)
        .order_by(desc(ValidationRun.started_at)).first()
    )
    pr = (
        db.query(PullRequest).filter(PullRequest.run_id == run.id)
        .order_by(desc(PullRequest.created_at)).first()
    )
    wb = (
        db.query(DataHubWriteback).filter(DataHubWriteback.run_id == run.id)
        .order_by(desc(DataHubWriteback.started_at)).first()
    )
    findings = (
        db.query(Finding).filter(Finding.run_id == run.id).all()
    )
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "informational": 4}
    findings.sort(key=lambda f: order.get(f.severity, 9))

    return {
        **run_summary(db, run),
        "timeline": [
            {
                "from": t.from_status,
                "to": t.to_status,
                "reason": t.transition_reason,
                "actor": t.actor_type,
                "at": t.created_at.isoformat(),
                "meta": t.meta,
            }
            for t in run.transitions
        ],
        "context": None if not snap else {
            "root_urn": snap.root_urn,
            "lineage_edges": snap.lineage_edges,
            "max_hops": snap.max_hops,
            "datahub_gms_url": snap.datahub_gms_url,
            "checksum": snap.checksum,
            "assets": snap.assets,
            "tool_trace": snap.tool_trace,
            "retrieved_at": snap.retrieved_at.isoformat(),
        },
        "findings": [
            {
                "id": f.id,
                "rule_code": f.rule_code,
                "severity": f.severity,
                "title": f.title,
                "summary": f.summary,
                "expected_value": f.expected_value,
                "observed_value": f.observed_value,
                "source_asset_urn": f.source_asset_urn,
                "downstream_asset_urn": f.downstream_asset_urn,
                "owner_reference": f.owner_reference,
                "customer_impact": f.customer_impact,
                "business_risk": f.business_risk,
                "auto_fix_eligible": f.auto_fix_eligible,
                "remediation_template": f.remediation_template,
                "target_file": f.target_file,
                "evidence": f.evidence,
            }
            for f in findings
        ],
        "plan": None if not plan else {
            "id": plan.id,
            "version": plan.version,
            "summary": plan.summary,
            "business_impact": plan.business_impact,
            "proposed_actions": plan.proposed_actions,
            "validation_plan": plan.validation_plan,
            "rollback_plan": plan.rollback_plan,
            "confidence_explanation": plan.confidence_explanation,
            "source": plan.plan_source,
            "rejected_reason": plan.rejected_reason,
            "provider": plan.model_provider,
            "checksum": plan.checksum,
        },
        "approval": None if not approval else {
            "decision": approval.decision,
            "decided_by": approval.decided_by,
            "role": approval.decided_by_role,
            "reason": approval.decision_reason,
            "at": approval.decided_at.isoformat(),
            "plan_checksum": approval.plan_checksum,
            "context_checksum": approval.context_snapshot_checksum,
        },
        "patch": None if not artifact else {
            "checksum": artifact.checksum,
            "combined_diff": artifact.combined_diff,
            "files": artifact.files,
            "skipped": artifact.skipped,
            "rejected": artifact.rejected,
        },
        "validation": None if not validation else {
            "status": validation.status,
            "summary": validation.summary,
            "steps": validation.steps,
            "duration_ms": validation.duration_ms,
        },
        "pull_request": None if not pr else {
            "status": pr.status,
            "branch": pr.branch_name,
            "url": pr.external_pr_url,
            "number": pr.external_pr_number,
            "repository": pr.repository_full_name,
            "error": pr.error,
        },
        "writeback": None if not wb else {
            "status": wb.status,
            "operations": wb.operations,
            "verification": wb.verification_result,
            "document_urn": wb.document_urn,
            "tool_trace": wb.tool_trace,
        },
    }


# --- background execution ----------------------------------------------------


def _drive_to_approval(run_id: str) -> None:
    with session() as db:
        run = db.get(Run, run_id)
        if run:
            asyncio.run(advance_to_approval(db, run))


def _drive_after_approval(run_id: str) -> None:
    with session() as db:
        run = db.get(Run, run_id)
        if run:
            asyncio.run(advance_after_approval(db, run))


# --- routes ------------------------------------------------------------------


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "datahub_gms_url": os.environ.get("DATAHUB_GMS_URL", "unset"),
        "pr_live": os.environ.get("COMGU_PR_LIVE", "").lower() in ("1", "true"),
        "time": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/runs")
def list_runs(limit: int = 50, db: Session = Depends(get_session)) -> dict[str, Any]:
    runs = db.query(Run).order_by(desc(Run.created_at)).limit(limit).all()
    return {"runs": [run_summary(db, r) for r in runs]}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, db: Session = Depends(get_session)) -> dict[str, Any]:
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(404, "run not found")
    return run_detail(db, run)


class TriggerRequest(BaseModel):
    trigger_type: str = "manual"


@app.post("/api/runs")
def create_run(
    body: TriggerRequest, tasks: BackgroundTasks, db: Session = Depends(get_session)
) -> dict[str, Any]:
    org, shop = ensure_demo_tenant(db)
    change = bridge.load_catalog()

    event = CommerceEvent(
        organisation_id=org.id,
        shop_id=shop.id,
        event_type="product_price_changed",
        source_system="shopify",
        entity_type="product",
        entity_external_id=change.sku,
        after_state={
            "sku": change.sku,
            "price": str(change.price),
            "inventory_quantity": change.inventory_quantity,
            "return_window_days": change.return_window_days,
        },
    )
    db.add(event)
    db.flush()

    run = Run(
        organisation_id=org.id,
        shop_id=shop.id,
        commerce_event_id=event.id,
        trigger_type=body.trigger_type,
        status=Status.RECEIVED,
    )
    db.add(run)
    db.commit()

    tasks.add_task(_drive_to_approval, run.id)
    return {"run_id": run.id, "status": run.status}


class DecisionRequest(BaseModel):
    decided_by: str = "operator@comgu.site"
    role: str = "owner"
    reason: str | None = None


@app.post("/api/runs/{run_id}/approve")
def approve(
    run_id: str, body: DecisionRequest, tasks: BackgroundTasks,
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(404, "run not found")
    if run.status != Status.AWAITING_APPROVAL:
        raise HTTPException(409, f"run is {run.status}, not awaiting approval")

    plan = (
        db.query(RemediationPlan).filter(RemediationPlan.run_id == run.id)
        .order_by(desc(RemediationPlan.created_at)).first()
    )
    snap = (
        db.query(ContextSnapshot).filter(ContextSnapshot.run_id == run.id)
        .order_by(desc(ContextSnapshot.retrieved_at)).first()
    )
    if not plan or not snap:
        raise HTTPException(409, "run has no plan or context to approve")

    # Bind the decision to exactly what was shown.
    db.add(
        Approval(
            run_id=run.id, remediation_plan_id=plan.id, decision="approved",
            decided_by=body.decided_by, decided_by_role=body.role,
            decision_reason=body.reason,
            context_snapshot_checksum=snap.checksum, plan_checksum=plan.checksum,
        )
    )
    plan.status = "approved"
    db.commit()

    transition(
        db, run, Status.APPROVED, reason=f"approved by {body.decided_by}",
        actor=Actor(type="user", id=body.decided_by),
    )
    tasks.add_task(_drive_after_approval, run.id)
    return {"run_id": run.id, "status": run.status}


@app.post("/api/runs/{run_id}/reject")
def reject(
    run_id: str, body: DecisionRequest, db: Session = Depends(get_session)
) -> dict[str, Any]:
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(404, "run not found")
    if run.status != Status.AWAITING_APPROVAL:
        raise HTTPException(409, f"run is {run.status}, not awaiting approval")

    plan = (
        db.query(RemediationPlan).filter(RemediationPlan.run_id == run.id)
        .order_by(desc(RemediationPlan.created_at)).first()
    )
    snap = (
        db.query(ContextSnapshot).filter(ContextSnapshot.run_id == run.id)
        .order_by(desc(ContextSnapshot.retrieved_at)).first()
    )
    db.add(
        Approval(
            run_id=run.id, remediation_plan_id=plan.id if plan else "", decision="rejected",
            decided_by=body.decided_by, decided_by_role=body.role,
            decision_reason=body.reason,
            context_snapshot_checksum=snap.checksum if snap else "",
            plan_checksum=plan.checksum if plan else "",
        )
    )
    if plan:
        plan.status = "rejected"
    db.commit()

    # Rejection preserves the evidence and the proposal.
    transition(
        db, run, Status.REJECTED, reason=body.reason or f"rejected by {body.decided_by}",
        actor=Actor(type="user", id=body.decided_by),
    )
    return {"run_id": run.id, "status": run.status}


# --- webhooks ----------------------------------------------------------------


@app.post("/webhooks/shopify/{topic:path}")
async def shopify_webhook(
    topic: str, request: Request, tasks: BackgroundTasks,
    db: Session = Depends(get_session),
) -> JSONResponse:
    raw = await request.body()
    if len(raw) > shopify.MAX_PAYLOAD_BYTES:
        raise HTTPException(413, "payload too large")

    headers = {k.lower(): v for k, v in request.headers.items()}
    shop_domain = headers.get("x-shopify-shop-domain", "")
    hmac_header = headers.get("x-shopify-hmac-sha256")
    webhook_id = headers.get("x-shopify-webhook-id")

    org, shop = ensure_demo_tenant(db)
    body_hash = shopify.payload_hash(raw)
    key = shopify.idempotency_key(shop_domain or shop.shop_domain, topic, webhook_id, body_hash)

    # A duplicate delivery acknowledges and links to the existing event.
    existing = db.query(WebhookEvent).filter(WebhookEvent.idempotency_key == key).first()
    if existing:
        return JSONResponse(
            {"status": "duplicate", "webhook_event_id": existing.id}, status_code=200
        )

    valid = shopify.verify_hmac(raw, hmac_header, shopify.webhook_secret())
    event = WebhookEvent(
        organisation_id=org.id, shop_id=shop.id, topic=topic,
        external_webhook_id=webhook_id, idempotency_key=key,
        hmac_valid=valid, payload_hash=body_hash,
        raw_payload={} if not valid else (await _safe_json(raw)),
        headers_redacted=shopify.redact_headers(headers),
        processing_status="received" if valid else "rejected",
        error_code=None if valid else "invalid_hmac",
    )
    db.add(event)
    db.commit()

    if not valid:
        # Recorded for audit, but never processed.
        raise HTTPException(401, "invalid webhook signature")

    if topic not in shopify.ALLOWED_TOPICS:
        event.processing_status = "rejected"
        event.error_code = "topic_not_allowed"
        db.commit()
        raise HTTPException(400, f"topic {topic!r} is not accepted")

    payload = event.raw_payload
    change = shopify.normalize(topic, payload)
    commerce = CommerceEvent(
        organisation_id=org.id, shop_id=shop.id, webhook_event_id=event.id,
        event_type=change.event_type, source_system="shopify",
        entity_type=change.entity_type, entity_external_id=change.entity_external_id,
        after_state=change.after_state, before_state=change.before_state,
    )
    db.add(commerce)
    db.flush()

    run = Run(
        organisation_id=org.id, shop_id=shop.id, commerce_event_id=commerce.id,
        trigger_type="webhook", status=Status.SIGNATURE_VERIFIED,
    )
    db.add(run)
    event.processing_status = "processed"
    db.commit()

    tasks.add_task(_drive_to_approval, run.id)
    return JSONResponse({"status": "accepted", "run_id": run.id}, status_code=202)


async def _safe_json(raw: bytes) -> dict:
    import json

    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"payload": parsed}
    except Exception:
        return {}


# --- demo --------------------------------------------------------------------


@app.get("/api/demo/status")
def demo_status(db: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        change = bridge.load_catalog()
        projections = bridge.build_projections()
        lab_ok = True
        lab_error = None
    except Exception as e:
        change = None
        projections = {}
        lab_ok = False
        lab_error = f"{type(e).__name__}: {e}"

    return {
        "lab_available": lab_ok,
        "lab_error": lab_error,
        "lab_path": str(bridge.DEFAULT_LAB_PATH if not lab_ok else bridge.lab_path()),
        "catalog": None if not change else {
            "sku": change.sku,
            "title": change.title,
            "price": str(change.price),
            "sellable_units": change.sellable_units,
            "return_window_days": change.return_window_days,
        },
        "projections": projections,
        "runs": db.query(func.count(Run.id)).scalar(),
        "datahub_gms_url": os.environ.get("DATAHUB_GMS_URL", "unset"),
    }


@app.post("/api/demo/reset")
def demo_reset(db: Session = Depends(get_session)) -> dict[str, Any]:
    """Restore the contradictory starting state.

    Resets the lab checkout to origin/main (where the contradictions live) and
    clears run history. DataHub keeps its graph; the write-back properties are
    overwritten by the next run.
    """
    import subprocess

    lab = bridge.lab_path()
    steps: list[dict[str, Any]] = []

    for args in (
        ["git", "checkout", "--force", "main"],
        ["git", "reset", "--hard", "origin/main"],
        ["git", "clean", "-fd", "feeds", "promotions", "bundles", "ai", "policies"],
    ):
        proc = subprocess.run(args, cwd=lab, capture_output=True, text=True, timeout=60)
        steps.append(
            {"command": " ".join(args), "ok": proc.returncode == 0,
             "output": (proc.stdout + proc.stderr).strip()[-300:]}
        )

    deleted = db.query(Run).count()
    for model in (
        DataHubWriteback, PullRequest, ValidationRun, GeneratedArtifact,
        Approval, RemediationPlan, Finding, ContextSnapshot,
    ):
        db.query(model).delete()
    db.query(Run).delete()
    db.query(CommerceEvent).delete()
    db.query(WebhookEvent).delete()
    db.commit()

    org, _ = ensure_demo_tenant(db)
    db.add(
        AuditLog(
            organisation_id=org.id, actor_type="user", action="demo.reset",
            resource_type="demo", meta={"runs_deleted": deleted},
        )
    )
    db.commit()

    projections = bridge.build_projections()
    return {"status": "reset", "runs_deleted": deleted, "steps": steps,
            "projections": projections}


# --- UI ----------------------------------------------------------------------

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")
