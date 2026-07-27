"""The run state machine.

Transitions are guarded and recorded. A run only advances along a declared
edge, so an unexpected jump is a bug that raises rather than a state nobody can
explain later.

Two properties matter operationally:

  * every transition writes a RunTransition row, so the timeline is derived
    from history rather than reconstructed
  * the terminal states are explicit, so a worker restart can tell the
    difference between "still working" and "stopped here"
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from apps.api.db.models import AuditLog, Run, RunTransition


class Status:
    RECEIVED = "RECEIVED"
    SIGNATURE_VERIFIED = "SIGNATURE_VERIFIED"
    NORMALIZED = "NORMALIZED"
    CONTEXT_PENDING = "CONTEXT_PENDING"
    CONTEXT_RESOLVED = "CONTEXT_RESOLVED"
    CHECKS_RUNNING = "CHECKS_RUNNING"
    CHECKS_COMPLETED = "CHECKS_COMPLETED"
    REMEDIATION_PLANNING = "REMEDIATION_PLANNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    PATCH_GENERATING = "PATCH_GENERATING"
    PATCH_GENERATED = "PATCH_GENERATED"
    VALIDATION_RUNNING = "VALIDATION_RUNNING"
    VALIDATED = "VALIDATED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    PULL_REQUEST_CREATING = "PULL_REQUEST_CREATING"
    PULL_REQUEST_OPENED = "PULL_REQUEST_OPENED"
    DATAHUB_WRITEBACK_PENDING = "DATAHUB_WRITEBACK_PENDING"
    DATAHUB_UPDATED = "DATAHUB_UPDATED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL = {Status.COMPLETED, Status.FAILED, Status.CANCELLED, Status.REJECTED}

# A run that is waiting on a human, not on a worker.
WAITING_ON_HUMAN = {Status.AWAITING_APPROVAL}

ALLOWED: dict[str, set[str]] = {
    Status.RECEIVED: {Status.SIGNATURE_VERIFIED, Status.NORMALIZED, Status.FAILED, Status.CANCELLED},
    Status.SIGNATURE_VERIFIED: {Status.NORMALIZED, Status.FAILED, Status.CANCELLED},
    Status.NORMALIZED: {Status.CONTEXT_PENDING, Status.FAILED, Status.CANCELLED},
    Status.CONTEXT_PENDING: {Status.CONTEXT_RESOLVED, Status.FAILED, Status.CANCELLED},
    Status.CONTEXT_RESOLVED: {Status.CHECKS_RUNNING, Status.FAILED, Status.CANCELLED},
    Status.CHECKS_RUNNING: {Status.CHECKS_COMPLETED, Status.FAILED, Status.CANCELLED},
    Status.CHECKS_COMPLETED: {Status.REMEDIATION_PLANNING, Status.COMPLETED, Status.FAILED, Status.CANCELLED},
    Status.REMEDIATION_PLANNING: {Status.AWAITING_APPROVAL, Status.FAILED, Status.CANCELLED},
    Status.AWAITING_APPROVAL: {Status.APPROVED, Status.REJECTED, Status.CANCELLED},
    Status.APPROVED: {Status.PATCH_GENERATING, Status.FAILED, Status.CANCELLED},
    Status.PATCH_GENERATING: {Status.PATCH_GENERATED, Status.FAILED, Status.CANCELLED},
    Status.PATCH_GENERATED: {Status.VALIDATION_RUNNING, Status.FAILED, Status.CANCELLED},
    Status.VALIDATION_RUNNING: {Status.VALIDATED, Status.VALIDATION_FAILED, Status.FAILED},
    # A failed validation goes back for revision; it never proceeds to a PR.
    Status.VALIDATION_FAILED: {Status.AWAITING_APPROVAL, Status.FAILED, Status.CANCELLED},
    Status.VALIDATED: {Status.PULL_REQUEST_CREATING, Status.DATAHUB_WRITEBACK_PENDING, Status.FAILED},
    Status.PULL_REQUEST_CREATING: {Status.PULL_REQUEST_OPENED, Status.FAILED, Status.CANCELLED},
    Status.PULL_REQUEST_OPENED: {Status.DATAHUB_WRITEBACK_PENDING, Status.FAILED},
    Status.DATAHUB_WRITEBACK_PENDING: {Status.DATAHUB_UPDATED, Status.FAILED},
    Status.DATAHUB_UPDATED: {Status.COMPLETED, Status.FAILED},
    Status.REJECTED: set(),
    Status.COMPLETED: set(),
    Status.FAILED: set(),
    Status.CANCELLED: set(),
}


class IllegalTransition(RuntimeError):
    """A run was asked to move along an edge that does not exist."""


@dataclass
class Actor:
    type: str = "worker"  # user | system | worker | connector
    id: str | None = None


def transition(
    db: Session,
    run: Run,
    to_status: str,
    *,
    reason: str | None = None,
    actor: Actor | None = None,
    meta: dict | None = None,
) -> Run:
    """Move a run, recording history. Raises on an undeclared edge."""
    actor = actor or Actor()
    frm = run.status

    if to_status not in ALLOWED.get(frm, set()):
        raise IllegalTransition(
            f"run {run.id[:8]} cannot move {frm} -> {to_status}; "
            f"allowed: {sorted(ALLOWED.get(frm, set())) or 'none (terminal)'}"
        )

    run.status = to_status
    now = datetime.now(timezone.utc)
    if run.started_at is None:
        run.started_at = now
    if to_status in TERMINAL:
        run.completed_at = now
        run.locked_at = None
        run.locked_by = None
    if to_status == Status.FAILED:
        run.failed_at = now
        run.failure_message = reason

    db.add(
        RunTransition(
            run_id=run.id,
            from_status=frm,
            to_status=to_status,
            transition_reason=reason,
            actor_type=actor.type,
            actor_user_id=actor.id,
            meta=meta or {},
        )
    )
    db.add(
        AuditLog(
            organisation_id=run.organisation_id,
            shop_id=run.shop_id,
            actor_type=actor.type,
            actor_user_id=actor.id,
            action=f"run.{to_status.lower()}",
            resource_type="run",
            resource_id=run.id,
            meta={"from": frm, "to": to_status, "reason": reason},
        )
    )
    db.commit()
    return run


def can(run: Run, to_status: str) -> bool:
    return to_status in ALLOWED.get(run.status, set())
