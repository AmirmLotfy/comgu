"""Drive a run through the workflow, persisting evidence at every step.

Split in two halves at the approval boundary:

  `advance_to_approval` — retrieve context, check, plan. Read-only with respect
  to the outside world; safe to retry.

  `advance_after_approval` — patch, validate, open a PR, write back. Only
  reachable once an Approval row exists.

Nothing here decides anything a human should: the split is the enforcement.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from apps.api.db.models import (
    Approval,
    ContextSnapshot,
    DataHubWriteback,
    Finding as FindingRow,
    GeneratedArtifact,
    PullRequest,
    RemediationPlan,
    Run,
    ValidationRun,
)
from apps.api.workflow import Actor, Status, transition
from packages.datahub.context_builder import build_run_context
from packages.datahub.mcp_client import DataHubUnavailable, datahub_session
from packages.datahub.writeback import write_back
from packages.github.pr import open_pull_request
from packages.lab import bridge
from packages.patch.generator import discard, generate
from packages.patch.validator import run_validation
from packages.planner.planner import plan_remediation
from packages.rules.context import RunContext
from packages.rules.engine import run_rules


def gms_url() -> str:
    return os.environ.get("DATAHUB_GMS_URL", "http://localhost:18080")


def checksum(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, default=str).encode()
    ).hexdigest()


def _rebuild_context(db: Session, run: Run) -> RunContext:
    """Reconstruct the RunContext from the persisted snapshot.

    The approval was given against this snapshot, so remediation must use it
    rather than re-querying DataHub and possibly acting on a different graph.
    """
    from packages.rules.context import AssetContext, BlastRadius

    snap = (
        db.query(ContextSnapshot)
        .filter(ContextSnapshot.run_id == run.id)
        .order_by(ContextSnapshot.retrieved_at.desc())
        .first()
    )
    if snap is None:
        raise RuntimeError("run has no context snapshot; cannot remediate safely")

    assets = [AssetContext(**a) for a in snap.assets]
    change = bridge.load_catalog()
    return RunContext(
        change=change,
        blast_radius=BlastRadius(
            root_urn=snap.root_urn,
            assets=[a for a in assets if a.urn != snap.root_urn],
            max_hops=snap.max_hops,
            lineage_edges=snap.lineage_edges,
        ),
        assets_by_urn={a.urn: a for a in assets},
        projections=bridge.build_projections(),
        tool_trace=snap.tool_trace,
    )


def _asset_dict(a) -> dict:
    return {
        "urn": a.urn,
        "name": a.name,
        "entity_type": a.entity_type,
        "authority": a.authority,
        "customer_facing": a.customer_facing,
        "criticality": a.criticality,
        "channel": a.channel,
        "owners": list(a.owners),
        "lab_file": a.lab_file,
        "comgu_rule": a.comgu_rule,
        "degree": a.degree,
    }


# --- phase 1: up to approval -------------------------------------------------


async def advance_to_approval(db: Session, run: Run) -> Run:
    """Context → checks → plan → AWAITING_APPROVAL."""
    actor = Actor(type="worker", id="comgu-worker")

    if run.status == Status.RECEIVED:
        transition(db, run, Status.NORMALIZED, reason="event normalized", actor=actor)

    if run.status == Status.NORMALIZED:
        transition(db, run, Status.CONTEXT_PENDING, actor=actor)

    # --- DataHub context ---
    if run.status == Status.CONTEXT_PENDING:
        change = bridge.load_catalog()
        source_urn = bridge.catalog_source_urn()
        projections = bridge.build_projections()
        try:
            async with datahub_session(gms_url()) as dh:
                ctx = await build_run_context(dh, change, source_urn, projections)
                trace = dh.trace.to_json()
        except DataHubUnavailable as e:
            # No hardcoded lineage fallback — the run fails visibly instead.
            transition(db, run, Status.FAILED, reason=f"DataHub context failed: {e}", actor=actor)
            return run

        assets = [_asset_dict(a) for a in ctx.assets_by_urn.values()]
        snap = ContextSnapshot(
            run_id=run.id,
            datahub_gms_url=gms_url(),
            root_urn=ctx.blast_radius.root_urn,
            lineage_edges=ctx.blast_radius.lineage_edges,
            max_hops=ctx.blast_radius.max_hops,
            assets=assets,
            tool_trace=trace,
            checksum=checksum(assets),
        )
        db.add(snap)
        db.commit()
        transition(
            db, run, Status.CONTEXT_RESOLVED,
            reason=f"{len(ctx.blast_radius.assets)} downstream assets from "
                   f"{ctx.blast_radius.lineage_edges} lineage results",
            actor=actor, meta={"tool_calls": len(trace)},
        )

    # --- deterministic checks ---
    if run.status == Status.CONTEXT_RESOLVED:
        transition(db, run, Status.CHECKS_RUNNING, actor=actor)
        ctx = _rebuild_context(db, run)
        report = run_rules(ctx)

        if report.context_error:
            transition(db, run, Status.FAILED, reason=report.context_error, actor=actor)
            return run

        for f in report.findings:
            db.add(
                FindingRow(
                    organisation_id=run.organisation_id,
                    shop_id=run.shop_id,
                    run_id=run.id,
                    rule_code=f.rule_code,
                    rule_version=f.rule_version,
                    severity=f.severity.value,
                    title=f.title,
                    summary=f.summary,
                    expected_value=f.expected_value,
                    observed_value=f.observed_value,
                    source_asset_urn=f.source_asset_urn,
                    downstream_asset_urn=f.downstream_asset_urn,
                    owner_reference=f.owner_reference,
                    customer_impact=f.customer_impact,
                    business_risk=f.business_risk,
                    confidence=f.confidence,
                    auto_fix_eligible=f.auto_fix_eligible,
                    remediation_template=f.remediation_template,
                    target_file=f.target_file,
                    evidence=[e.to_json() for e in f.evidence],
                )
            )
        run.severity = report.max_severity.value
        db.commit()
        transition(
            db, run, Status.CHECKS_COMPLETED,
            reason=f"{len(report.findings)} findings, max {report.max_severity.value}",
            actor=actor, meta=report.counts,
        )

        if not report.findings:
            transition(db, run, Status.COMPLETED, reason="no contradictions found", actor=actor)
            return run

    # --- plan ---
    if run.status == Status.CHECKS_COMPLETED:
        transition(db, run, Status.REMEDIATION_PLANNING, actor=actor)
        ctx = _rebuild_context(db, run)
        findings = run_rules(ctx).findings
        result = plan_remediation(ctx, findings)
        plan = result.plan

        payload = plan.model_dump()
        row = RemediationPlan(
            run_id=run.id,
            version=1,
            summary=plan.summary,
            business_impact=plan.business_impact,
            proposed_actions=[a.model_dump() for a in plan.proposed_actions],
            validation_plan=[v.model_dump() for v in plan.validation_plan],
            rollback_plan=plan.rollback_plan,
            confidence_explanation=plan.confidence_explanation,
            plan_source=result.source,
            rejected_reason=result.rejected_reason,
            model_provider=result.provider,
            checksum=checksum(payload),
        )
        db.add(row)
        db.commit()
        transition(
            db, run, Status.AWAITING_APPROVAL,
            reason=f"plan v1 ready ({result.source})", actor=actor,
        )

    return run


# --- phase 2: after approval -------------------------------------------------


async def advance_after_approval(db: Session, run: Run) -> Run:
    """Patch → validate → PR → write-back → COMPLETED."""
    actor = Actor(type="worker", id="comgu-worker")
    lab = bridge.lab_path()
    ctx = _rebuild_context(db, run)
    findings = run_rules(ctx).findings

    if run.status == Status.APPROVED:
        transition(db, run, Status.PATCH_GENERATING, actor=actor)

    patch = None
    try:
        if run.status == Status.PATCH_GENERATING:
            patch = generate(findings, ctx.change, lab)
            db.add(
                GeneratedArtifact(
                    run_id=run.id,
                    workspace_reference=str(patch.workspace),
                    checksum=patch.checksum,
                    combined_diff=patch.combined_diff,
                    files=[f.to_json() for f in patch.files],
                    skipped=patch.skipped,
                    rejected=patch.rejected,
                )
            )
            db.commit()
            transition(
                db, run, Status.PATCH_GENERATED,
                reason=f"{len(patch.files)} files patched", actor=actor,
            )

        # --- validation ---
        if run.status == Status.PATCH_GENERATED:
            transition(db, run, Status.VALIDATION_RUNNING, actor=actor)
            validation = run_validation(patch.workspace, ["pytest"])
            db.add(
                ValidationRun(
                    run_id=run.id,
                    status=validation.status,
                    summary=validation.summary,
                    steps=[s.to_json() for s in validation.steps],
                    duration_ms=validation.duration_ms,
                    completed_at=datetime.now(timezone.utc),
                )
            )
            db.commit()

            if not validation.passed:
                transition(
                    db, run, Status.VALIDATION_FAILED,
                    reason="validation failed; pull request blocked", actor=actor,
                )
                return run

            transition(
                db, run, Status.VALIDATED,
                reason=f"{validation.summary.get('tests_passed', 0)} tests passed", actor=actor,
            )

        # --- pull request ---
        repo = os.environ.get("GITHUB_LAB_REPO", "")
        approval = (
            db.query(Approval).filter(Approval.run_id == run.id)
            .order_by(Approval.decided_at.desc()).first()
        )
        validation_row = (
            db.query(ValidationRun).filter(ValidationRun.run_id == run.id)
            .order_by(ValidationRun.started_at.desc()).first()
        )

        if run.status == Status.VALIDATED and repo and patch:
            transition(db, run, Status.PULL_REQUEST_CREATING, actor=actor)
            from packages.patch.validator import ValidationRun as VR, ValidationStep

            vr = VR(status=validation_row.status, duration_ms=validation_row.duration_ms)
            vr.steps = [ValidationStep(**s) for s in validation_row.steps]

            pr = open_pull_request(
                run_id=run.id, ctx=ctx, findings=findings, patch=patch, validation=vr,
                repo=repo, lab_path=lab,
                approver=approval.decided_by if approval else "unknown",
                approved_at=approval.decided_at.isoformat() if approval else "",
                dry_run=os.environ.get("COMGU_PR_LIVE", "").lower() not in ("1", "true"),
            )
            db.add(
                PullRequest(
                    run_id=run.id, repository_full_name=pr.repository, branch_name=pr.branch,
                    commit_sha=pr.commit_sha, external_pr_number=pr.number,
                    external_pr_url=pr.url, status=pr.status, body=pr.body, error=pr.error,
                )
            )
            db.commit()
            transition(
                db, run, Status.PULL_REQUEST_OPENED,
                reason=pr.url or f"pull request {pr.status}", actor=actor,
            )

        # --- DataHub write-back ---
        if run.status in (Status.VALIDATED, Status.PULL_REQUEST_OPENED):
            transition(db, run, Status.DATAHUB_WRITEBACK_PENDING, actor=actor)
            pr_row = (
                db.query(PullRequest).filter(PullRequest.run_id == run.id)
                .order_by(PullRequest.created_at.desc()).first()
            )
            async with datahub_session(gms_url(), enable_mutations=True) as dhw:
                wb = await write_back(
                    dhw, run_id=run.id, ctx=ctx, findings=findings,
                    validation_summary=validation_row.summary if validation_row else {},
                    approver=approval.decided_by if approval else "unknown",
                    pr_url=pr_row.external_pr_url if pr_row and pr_row.status == "open" else None,
                )
                wtrace = dhw.trace.to_json()

            db.add(
                DataHubWriteback(
                    run_id=run.id, status=wb.status,
                    operations=[o.to_json() for o in wb.operations],
                    verification_result=wb.verification, document_urn=wb.document_urn,
                    tool_trace=wtrace, completed_at=datetime.now(timezone.utc),
                )
            )
            db.commit()
            transition(
                db, run, Status.DATAHUB_UPDATED,
                reason=f"write-back {wb.status}", actor=actor,
            )

        if run.status == Status.DATAHUB_UPDATED:
            transition(db, run, Status.COMPLETED, reason="run complete", actor=actor)

    finally:
        if patch is not None:
            discard(patch)

    return run


def run_sync(db: Session, run: Run) -> Run:
    """Synchronous entry point for the worker."""
    if run.status in (Status.APPROVED,) or run.status.startswith(("PATCH", "VALIDAT", "PULL", "DATAHUB")):
        return asyncio.run(advance_after_approval(db, run))
    return asyncio.run(advance_to_approval(db, run))
